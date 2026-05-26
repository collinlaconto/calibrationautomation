"""
probe_sync.py  —  Probe + multi-brand data logger synchronisation and visualiser.

Run with no arguments to open the graphical interface:
    python probe_sync.py

Or pass arguments directly for scripting / automation:
    python probe_sync.py --probe probe.csv --devices a.csv b.csv \\
                         --start "2024-06-01 14:00:00" --output chart.html

Adding support for a new logger brand
--------------------------------------
  Open logger_formats.json (in the same folder as this script) and add an entry.
  No changes to this file are required.  The GUI shows a "Format Detected" column
  and the log prints which format was matched for each file so you can verify.

Dependencies:
    pip install pandas plotly
    tkinter is part of Python's standard library (install python3-tk on Linux if needed)
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

# ── Third-party ───────────────────────────────────────────────────────────────
import pandas as pd
import plotly.graph_objects as go

# ── Optional GUI (tkinter ships with Python; absent on some headless servers) ─
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
    HAS_TK = True
except ImportError:
    HAS_TK = False

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — FORMAT REGISTRY
# Formats live in logger_formats.json next to this script.  The registry is
# loaded fresh on every run so users can add entries without restarting.
# ══════════════════════════════════════════════════════════════════════════════

FORMATS_FILE = Path(__file__).parent / "logger_formats.json"

# Built-in defaults — written to disk if logger_formats.json is missing.
_DEFAULT_FORMATS = {
    "easylog": {
        "description": "Lascar EasyLog / Omega OM-EL-USB (same software, OEM)",
        "signature":   ["Logger Name:"],
        "date_col":    "Date (DD/MM/YYYY)",
        "time_col":    "Time (HH:mm:ss)",
        "value_col":   "Temperature (\u00b0C)",
        "dayfirst":    True,
    },
    "madgetech": {
        "description": "MadgeTech 4 Data Logger Software",
        "signature":   ["Reading #,Date,Time", "Device Name,Model,Serial Number"],
        "date_col":    "Date",
        "time_col":    "Time",
        "value_col":   "Temperature, \u00b0C",
        "dayfirst":    False,
    },
    "reed": {
        "description": "Reed Instruments data loggers",
        "signature":   ["Device: Reed"],
        "time_col":    "Date/Time",
        "value_col":   "Temperature (\u00b0C)",
        "dayfirst":    False,
    },
    "vaisala": {
        "description": "Vaisala vLog software",
        "signature":   ["Vaisala vLog"],
        "time_col":    "Date/Time",
        "value_col":   "Temperature (\u00b0C)",
        "dayfirst":    False,
    },
}

def load_registry() -> dict:
    """
    Load the format registry from logger_formats.json.
    If the file does not exist it is created with the built-in defaults.
    The '_comment' key (documentation) is stripped before returning.
    """
    if not FORMATS_FILE.exists():
        FORMATS_FILE.write_text(
            json.dumps(_DEFAULT_FORMATS, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Created default format registry: {FORMATS_FILE}")

    with open(FORMATS_FILE, encoding="utf-8") as fh:
        data = json.load(fh)

    # Remove documentation keys so only real format entries remain
    return {k: v for k, v in data.items() if not k.startswith("_")}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — FORMAT AUTO-DETECTION
# Three-stage pipeline:
#   Stage 1 — Signature matching   (preamble text → most reliable)
#   Stage 2 — Column name matching (header columns → reliable for known brands)
#   Stage 3 — Value pattern analysis (date shapes → fallback for unknown files)
# ══════════════════════════════════════════════════════════════════════════════

def _read_preamble(path: str, n: int = 15) -> str:
    """Return the first n lines of a file as a single lowercased string."""
    lines = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(n), fh):
                lines.append(line.lower())
    except OSError:
        pass
    return "\n".join(lines)


def _find_header_row(path: str, search_names: list) -> int:
    """
    Scan line-by-line and return the index of the first line where any of
    search_names appears as a standalone CSV field (not merely as a substring
    of a metadata value like "Start Time" or "Device Name").

    Matching rule: the name must be surrounded by comma delimiters or
    start/end-of-line, so "Time" matches a column called "Time" but not
    a metadata cell called "Start Time".

    Returns 0 if no match is found.
    """
    patterns = [
        re.compile(
            r'(?:^|,)\s*' + re.escape(s.lower()) + r'\s*(?:,|$)',
            re.IGNORECASE,
        )
        for s in search_names if s
    ]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                stripped = line.strip()
                if any(p.search(stripped) for p in patterns):
                    return i
    except OSError:
        pass
    return 0


# Keywords used for value-pattern fallback detection
_TEMP_KEYWORDS      = ["temperature", "temp", "\u00b0c", "\u00b0f",
                        "degc", "degf", "celsius", "fahrenheit"]
_COMBINED_TS_KEYS   = ["date/time", "datetime", "timestamp", "date & time", "date-time"]
_DATE_ONLY_KEYS     = ["date"]   # matched only when "time" is absent from the name
_TIME_ONLY_KEYS     = ["time"]   # matched only when "date" is absent from the name


def _infer_dayfirst(date_values) -> bool:
    """
    Examine up to 20 date strings to determine whether the day comes first.

    Logic:
      - If the FIRST numeric component of any value is > 12, it must be the
        day (DD/MM/…) → return True.
      - If the SECOND component is > 12 and the first is ≤ 12, the first must
        be the month (MM/DD/…) → return False.
      - If all values are ambiguous (both components ≤ 12), return False
        (month-first / US convention is the safer default).
    """
    for val in list(date_values)[:20]:
        parts = re.split(r"[/\-.]", str(val).strip().split()[0])
        if len(parts) >= 2:
            try:
                first, second = int(parts[0]), int(parts[1])
                if first > 12:
                    return True   # unambiguously day-first
                if second > 12:
                    return False  # unambiguously month-first
            except (ValueError, IndexError):
                continue
    return False  # cannot determine → default month-first


def detect_file_format(path: str, registry: dict) -> tuple:
    """
    Identify the brand/format of a device CSV file.

    Returns (format_name: str, format_dict: dict).
    format_name is a registry key (e.g. "easylog") or "auto-detected" / "unknown".

    Stage 1 — Signature matching
    ─────────────────────────────
    Each registry entry can define a list of 'signature' strings.  The first
    ~15 lines of the file are searched for any signature (case-insensitive).
    This is the most reliable method because header text is brand-specific.

    Stage 2 — Column name matching
    ────────────────────────────────
    If no signature matches, the file's header columns are compared against
    each registry entry.  Entries that define a 'date_col' score highest when
    that column is present — split-column formats have the most distinctive
    names and are therefore the clearest signal.  The highest-scoring entry
    wins.  Entries that define a 'date_col' but whose column is absent are
    immediately disqualified.

    Stage 3 — Value pattern analysis
    ──────────────────────────────────
    If no registry entry matches, a synthetic format dict is built by:
      • Finding a combined timestamp column (keywords: date/time, datetime, …)
        OR a separate date + time column pair.
      • Calling _infer_dayfirst() on the date values.
      • Finding the temperature column by scanning for temp/°C/°F keywords.
    """

    # ── Stage 1: Signature ──────────────────────────────────────────────────
    preamble = _read_preamble(path)
    for fmt_name, fmt in registry.items():
        for sig in fmt.get("signature", []):
            if sig.lower() in preamble:
                return fmt_name, fmt

    # ── Read header columns for stages 2 & 3 ───────────────────────────────
    # Gather all column names mentioned across all registry entries as hints
    hints = []
    for fmt in registry.values():
        hints.extend(filter(None, [
            fmt.get("date_col"), fmt.get("time_col"), fmt.get("value_col"),
        ]))
    hints += _COMBINED_TS_KEYS + ["time", "date"]

    header_row = _find_header_row(path, hints)
    try:
        sample = pd.read_csv(path, skiprows=header_row, nrows=5, dtype=str)
        sample.columns = sample.columns.str.strip()
        columns = set(sample.columns)
    except Exception:
        return "unknown", {}

    # ── Stage 2: Column name matching ───────────────────────────────────────
    best_score, best_name, best_fmt = 0, None, {}
    for fmt_name, fmt in registry.items():
        date_col  = fmt.get("date_col")
        time_col  = fmt.get("time_col", "")
        value_col = fmt.get("value_col", "")
        score = 0

        # A defined date_col that is absent → this format cannot match
        if date_col and date_col not in columns:
            continue

        if date_col and date_col in columns:
            score += 3   # split date column — very distinctive
        if time_col in columns:
            score += 2
        if value_col in columns:
            score += 1

        if score > best_score:
            best_score, best_name, best_fmt = score, fmt_name, fmt

    if best_score > 0:
        return best_name, best_fmt

    # ── Stage 3: Value pattern analysis (unknown brand) ─────────────────────
    inferred = {}

    # Find timestamp column
    ts_col = next(
        (c for c in sample.columns if any(k in c.lower() for k in _COMBINED_TS_KEYS)),
        None,
    )
    date_col = next(
        (c for c in sample.columns
         if "date" in c.lower() and "time" not in c.lower()),
        None,
    )
    time_col = next(
        (c for c in sample.columns
         if c.lower() == "time" or
         ("time" in c.lower() and "date" not in c.lower())),
        None,
    )

    if ts_col:
        inferred["time_col"] = ts_col
    elif date_col and time_col:
        inferred["date_col"]  = date_col
        inferred["time_col"]  = time_col
        inferred["dayfirst"]  = _infer_dayfirst(sample[date_col].dropna())
    elif sample.columns.size > 0:
        inferred["time_col"] = sample.columns[0]  # last resort

    # Find temperature column
    val_col = next(
        (c for c in sample.columns if any(k in c.lower() for k in _TEMP_KEYWORDS)),
        None,
    )
    if val_col is None and sample.columns.size > 1:
        val_col = sample.columns[-1]   # last column as final fallback

    inferred["value_col"] = val_col
    return "auto-detected", inferred


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — FILE LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_probe(path: str, time_col: str, value_col: str,
               start: datetime) -> pd.DataFrame:
    """
    Load the Additel 286 DAQ probe CSV.

    The probe records elapsed time in MM:SS format that resets to 00:00 every
    hour.  This function:
      1. Skips the metadata preamble to find the column header row.
      2. Parses each elapsed-time string into seconds.
      3. Detects and stitches clock resets (adds +3600 s per reset).
      4. Converts elapsed seconds to absolute datetimes using `start`.
    """
    def _parse_elapsed(val: str) -> float:
        val = str(val).strip()
        m = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", val)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m = re.fullmatch(r"(\d+):(\d{2}(?:\.\d+)?)", val)
        if m:
            return int(m.group(1)) * 60 + float(m.group(2))
        try:
            return float(val)
        except ValueError:
            raise ValueError(f"Cannot parse elapsed time: '{val}'")

    header_row = _find_header_row(path, [time_col, value_col])
    df = pd.read_csv(path, skiprows=header_row, dtype=str)
    df.columns = df.columns.str.strip()

    for col in [time_col, value_col]:
        if col not in df.columns:
            raise KeyError(
                f"Probe file: column '{col}' not found.\n"
                f"  Available: {list(df.columns)}"
            )

    raw_s    = df[time_col].apply(_parse_elapsed)
    offsets  = (raw_s.diff() < 0).cumsum() * 3600   # stitch each reset
    elapsed  = raw_s + offsets
    df["_abs_time"] = [start + timedelta(seconds=float(s)) for s in elapsed]
    df["_value"]    = pd.to_numeric(df[value_col], errors="coerce")
    return df[["_abs_time", "_value"]].dropna()


def load_device(path: str, fmt: dict) -> pd.DataFrame:
    """
    Load a device logger CSV using a pre-detected format dictionary.

    The format dict (from the registry or from auto-detection) specifies:
      time_col        — the timestamp (or time-only) column name
      date_col        — the date column name, if split from time (optional)
      value_col       — the temperature column name
      dayfirst        — True for DD/MM/YYYY, False for MM/DD/YYYY (default False)
      datetime_format — explicit strptime string (optional, overrides dayfirst)
    """
    time_col        = fmt.get("time_col", "")
    date_col        = fmt.get("date_col")
    value_col       = fmt.get("value_col", "")
    dayfirst        = fmt.get("dayfirst", False)
    datetime_format = fmt.get("datetime_format")

    search = [s for s in [time_col, date_col, value_col] if s]
    header_row = _find_header_row(path, search)
    df = pd.read_csv(path, skiprows=header_row, dtype=str)
    df.columns = df.columns.str.strip()

    for col in [c for c in [time_col, value_col] if c]:
        if col not in df.columns:
            raise KeyError(
                f"'{os.path.basename(path)}': column '{col}' not found.\n"
                f"  Available: {list(df.columns)}\n"
                f"  Tip: check logger_formats.json for this brand's column names."
            )

    # Build combined timestamp string
    if date_col and date_col in df.columns:
        combined = df[date_col].str.strip() + " " + df[time_col].str.strip()
    else:
        combined = df[time_col].str.strip()

    # Parse timestamps
    if datetime_format:
        df["_abs_time"] = pd.to_datetime(combined, format=datetime_format)
    else:
        df["_abs_time"] = pd.to_datetime(
            combined, dayfirst=dayfirst
        )

    df["_value"] = pd.to_numeric(df[value_col], errors="coerce")
    return df[["_abs_time", "_value"]].dropna()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — TIME WINDOW ALIGNMENT
# ══════════════════════════════════════════════════════════════════════════════

def clip_to_probe_window(device_df: pd.DataFrame, name: str,
                         probe_start: datetime, probe_end: datetime,
                         log_fn=print) -> pd.DataFrame:
    """
    Remove device rows outside [probe_start, probe_end] and report what was trimmed.
    Devices that started recording before the probe simply have their early rows
    dropped here so the chart's x-axis is bounded to the test window.
    """
    total = len(device_df)
    mask  = (
        (device_df["_abs_time"] >= probe_start) &
        (device_df["_abs_time"] <= probe_end)
    )
    clipped = device_df[mask].copy()
    dropped  = total - len(clipped)

    if dropped:
        dev_start = device_df["_abs_time"].min()
        dev_end   = device_df["_abs_time"].max()
        early_s   = max(0.0, (probe_start - dev_start).total_seconds())
        late_s    = max(0.0, (dev_end     - probe_end).total_seconds())
        detail    = []
        if early_s:
            detail.append(f"{early_s:.0f} s before probe start")
        if late_s:
            detail.append(f"{late_s:.0f} s after probe end")
        log_fn(f"  \u26a0  '{name}': trimmed {dropped} row(s) — {', '.join(detail)}")
    else:
        log_fn(f"  \u2713  '{name}': fully within probe window")

    return clipped


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — CHART BUILDING
# ══════════════════════════════════════════════════════════════════════════════

def build_figure(probe_df: pd.DataFrame,
                 device_dfs: list,
                 device_names: list) -> go.Figure:
    """
    Assemble a Plotly figure.

    Probe  → thin line, no dot markers (4-second data — too dense for dots)
    Devices → thicker line + dot markers (1-minute data — dots show each sample)
    hovermode='x unified' → one tooltip spanning all traces on hover
    """
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=probe_df["_abs_time"], y=probe_df["_value"],
        mode="lines", name="Probe (ADT286)",
        line=dict(width=1.5, color="#00b4d8"),
    ))

    colors = [
        "#ef233c", "#f77f00", "#2dc653", "#9b5de5",
        "#f15bb5", "#fee440", "#00bbf9", "#fb5607",
        "#e63946", "#2a9d8f", "#e9c46a", "#264653",
    ]

    for i, (df, name) in enumerate(zip(device_dfs, device_names)):
        fig.add_trace(go.Scatter(
            x=df["_abs_time"], y=df["_value"],
            mode="lines+markers", name=name,
            line=dict(width=2, color=colors[i % len(colors)]),
            marker=dict(size=5),
        ))

    fig.update_layout(
        title="Temperature \u2014 Probe + Data Loggers (Synchronized)",
        xaxis_title="Time",
        yaxis_title="Temperature (\u00b0C)",
        template="plotly_dark",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(family="monospace", size=12),
    )
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — CORE PIPELINE
# Separated from all UI code so it can be called identically by the GUI,
# the CLI, or any future automation script.
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(probe_path: str,
                 probe_time_col: str,
                 probe_value_col: str,
                 start_dt: datetime,
                 device_paths: list,
                 output_path: str,
                 registry: dict,
                 log_fn=print) -> bool:
    """
    Full end-to-end pipeline:
      1. Load probe → convert elapsed time → absolute datetimes
      2. For each device: auto-detect format → load → clip to probe window
      3. Build Plotly figure → write self-contained HTML

    log_fn receives each status string; defaults to print() but can be any
    callable (e.g. a GUI text-append function).

    Returns True on success, False if an exception was caught.
    """
    try:
        # ── Probe ─────────────────────────────────────────────────────────────
        log_fn(f"Loading probe: {os.path.basename(probe_path)}")
        probe_df = load_probe(probe_path, probe_time_col, probe_value_col, start_dt)
        log_fn(
            f"  {len(probe_df):,} rows  |  "
            f"{probe_df['_abs_time'].min():%Y-%m-%d %H:%M:%S} \u2192 "
            f"{probe_df['_abs_time'].max():%Y-%m-%d %H:%M:%S}"
        )

        # ── Devices ───────────────────────────────────────────────────────────
        device_dfs, device_names = [], []
        for path in device_paths:
            name = os.path.splitext(os.path.basename(path))[0]
            fmt_name, fmt = detect_file_format(path, registry)
            desc = registry.get(fmt_name, {}).get("description", fmt_name)
            log_fn(f"Loading: {os.path.basename(path)}  [{desc}]")
            df = load_device(path, fmt)
            log_fn(
                f"  {len(df):,} rows  |  "
                f"{df['_abs_time'].min():%Y-%m-%d %H:%M:%S} \u2192 "
                f"{df['_abs_time'].max():%Y-%m-%d %H:%M:%S}"
            )
            device_dfs.append(df)
            device_names.append(name)

        # ── Align to probe window ─────────────────────────────────────────────
        probe_start = probe_df["_abs_time"].min()
        probe_end   = probe_df["_abs_time"].max()
        log_fn(
            f"\nAligning to probe window: "
            f"{probe_start:%Y-%m-%d %H:%M:%S} \u2192 {probe_end:%Y-%m-%d %H:%M:%S}"
        )
        device_dfs = [
            clip_to_probe_window(df, name, probe_start, probe_end, log_fn)
            for df, name in zip(device_dfs, device_names)
        ]

        for df, name in zip(device_dfs, device_names):
            if df.empty:
                log_fn(f"  \u2717  WARNING: '{name}' has no data in the probe window.")

        # ── Chart ─────────────────────────────────────────────────────────────
        log_fn("\nBuilding chart\u2026")
        fig = build_figure(probe_df, device_dfs, device_names)
        fig.write_html(output_path)
        log_fn(f"\u2713 Done!  Chart saved to: {output_path}")
        return True

    except Exception as exc:
        log_fn(f"\n\u2717 Error: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — GRAPHICAL INTERFACE (tkinter)
# ══════════════════════════════════════════════════════════════════════════════

if HAS_TK:
    class SyncApp(tk.Tk):
        """
        Main application window.

        Layout
        ──────
        ┌── Probe ─────────────────────────────────────────────────────────┐
        │  CSV file  [________________________]  [Browse]                   │
        │  Start     [2024-06-01 14:00:00     ]  (YYYY-MM-DD HH:MM:SS)     │
        │  Value col [Temperature (°C)        ]                             │
        ├── Device Loggers ────────────────────────────────────────────────┤
        │  ┌──────────────────────────────┬──────────────────────────────┐ │
        │  │ File                         │ Format Detected               │ │
        │  │ PRO-Unit-01.csv              │ Lascar EasyLog / Omega        │ │
        │  │ MadgeTech-01.csv             │ MadgeTech 4                   │ │
        │  └──────────────────────────────┴──────────────────────────────┘ │
        │  [Add Files]  [Remove Selected]  [Clear All]                      │
        ├── Output ────────────────────────────────────────────────────────┤
        │  Save to  [chart.html            ]  [Browse]                      │
        ├──────────────────────────────────────────────────────────────────┤
        │                    [▶  Generate Chart]                            │
        ├── Log ───────────────────────────────────────────────────────────┤
        │  (scrollable console output)                                      │
        └──────────────────────────────────────────────────────────────────┘
        """

        def __init__(self, registry: dict):
            super().__init__()
            self.registry      = registry
            self.device_paths  = []   # parallel lists kept in tree order
            self.device_formats = []

            self.title("Probe + Logger Sync Tool")
            self.minsize(640, 580)
            self.resizable(True, True)
            self._build_ui()

        # ── UI construction ──────────────────────────────────────────────────

        def _build_ui(self):
            PAD = dict(padx=8, pady=4)

            # ── Probe section ─────────────────────────────────────────────────
            pf = ttk.LabelFrame(self, text=" Probe (Additel 286 DAQ) ")
            pf.pack(fill="x", padx=10, pady=(10, 4))
            pf.columnconfigure(1, weight=1)

            ttk.Label(pf, text="CSV file:").grid(
                row=0, column=0, sticky="w", **PAD)
            self.probe_var = tk.StringVar()
            ttk.Entry(pf, textvariable=self.probe_var).grid(
                row=0, column=1, sticky="ew", **PAD)
            ttk.Button(pf, text="Browse\u2026", command=self._browse_probe).grid(
                row=0, column=2, **PAD)

            ttk.Label(pf, text="Start time:").grid(
                row=1, column=0, sticky="w", **PAD)
            self.start_var = tk.StringVar(value="2024-06-01 14:00:00")
            ttk.Entry(pf, textvariable=self.start_var, width=22).grid(
                row=1, column=1, sticky="w", **PAD)
            ttk.Label(pf, text="YYYY-MM-DD HH:MM:SS",
                      foreground="gray").grid(row=1, column=2, sticky="w", **PAD)

            ttk.Label(pf, text="Value column:").grid(
                row=2, column=0, sticky="w", **PAD)
            self.probe_val_var = tk.StringVar(value="Temperature (\u00b0C)")
            ttk.Entry(pf, textvariable=self.probe_val_var, width=28).grid(
                row=2, column=1, sticky="w", **PAD)
            ttk.Label(pf, text="(match exactly to your file's header)",
                      foreground="gray").grid(row=2, column=2, sticky="w", **PAD)

            # ── Device loggers section ────────────────────────────────────────
            df_ = ttk.LabelFrame(self, text=" Device Loggers — all brands mixed ")
            df_.pack(fill="both", expand=True, padx=10, pady=4)

            cols = ("file", "format")
            self.tree = ttk.Treeview(df_, columns=cols, show="headings", height=7)
            self.tree.heading("file",   text="File")
            self.tree.heading("format", text="Format Detected")
            self.tree.column("file",   width=280, stretch=True)
            self.tree.column("format", width=240, stretch=True)

            sb = ttk.Scrollbar(df_, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=sb.set)
            self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
            sb.pack(side="right", fill="y", pady=6, padx=(0, 8))

            bf = ttk.Frame(self)
            bf.pack(fill="x", padx=10, pady=2)
            ttk.Button(bf, text="Add Device Files\u2026",
                       command=self._add_devices).pack(side="left", padx=4)
            ttk.Button(bf, text="Remove Selected",
                       command=self._remove_selected).pack(side="left", padx=4)
            ttk.Button(bf, text="Clear All",
                       command=self._clear_devices).pack(side="left", padx=4)

            # ── Output section ────────────────────────────────────────────────
            of = ttk.LabelFrame(self, text=" Output ")
            of.pack(fill="x", padx=10, pady=4)
            of.columnconfigure(1, weight=1)

            ttk.Label(of, text="Save chart to:").grid(
                row=0, column=0, sticky="w", **PAD)
            self.output_var = tk.StringVar(value="chart.html")
            ttk.Entry(of, textvariable=self.output_var).grid(
                row=0, column=1, sticky="ew", **PAD)
            ttk.Button(of, text="Browse\u2026",
                       command=self._browse_output).grid(row=0, column=2, **PAD)

            # ── Run button ────────────────────────────────────────────────────
            ttk.Button(self, text="\u25b6  Generate Chart",
                       command=self._run).pack(pady=8)

            # ── Log area ──────────────────────────────────────────────────────
            lf = ttk.LabelFrame(self, text=" Log ")
            lf.pack(fill="both", expand=True, padx=10, pady=(0, 10))

            self.log_box = scrolledtext.ScrolledText(
                lf, height=9,
                font=("Courier", 9),
                state="disabled",
                background="#1e1e1e",
                foreground="#d4d4d4",
                insertbackground="#d4d4d4",
            )
            self.log_box.pack(fill="both", expand=True, padx=4, pady=4)
            self._log("Ready.  Add files above and click \u25b6 Generate Chart.")

        # ── Button callbacks ─────────────────────────────────────────────────

        def _browse_probe(self):
            path = filedialog.askopenfilename(
                title="Select Additel 286 Probe CSV",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            )
            if path:
                self.probe_var.set(path)
                # Default output to same directory as probe file
                default_out = str(Path(path).parent / "chart.html")
                if self.output_var.get() in ("", "chart.html"):
                    self.output_var.set(default_out)

        def _add_devices(self):
            paths = filedialog.askopenfilenames(
                title="Select Device Logger CSVs (all brands, any mix)",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            )
            for path in paths:
                if path in self.device_paths:
                    continue   # skip duplicates
                fmt_name, fmt = detect_file_format(path, self.registry)
                desc = self.registry.get(fmt_name, {}).get("description", fmt_name)
                self.device_paths.append(path)
                self.device_formats.append(fmt)
                # Use full path as the tree item ID so Remove can find it
                self.tree.insert(
                    "", "end", iid=path,
                    values=(os.path.basename(path), desc),
                )

        def _remove_selected(self):
            for iid in self.tree.selection():
                self.tree.delete(iid)
                if iid in self.device_paths:
                    idx = self.device_paths.index(iid)
                    self.device_paths.pop(idx)
                    self.device_formats.pop(idx)

        def _clear_devices(self):
            self.tree.delete(*self.tree.get_children())
            self.device_paths.clear()
            self.device_formats.clear()

        def _browse_output(self):
            path = filedialog.asksaveasfilename(
                title="Save Chart As",
                defaultextension=".html",
                filetypes=[("HTML files", "*.html"), ("All files", "*.*")],
            )
            if path:
                self.output_var.set(path)

        # ── Log helpers ──────────────────────────────────────────────────────

        def _log(self, msg: str):
            """Append a line to the log box.  Safe to call from any thread."""
            self.after(0, self._log_main, msg)

        def _log_main(self, msg: str):
            """Must only be called from the main thread (via after())."""
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        # ── Run ──────────────────────────────────────────────────────────────

        def _run(self):
            probe_path  = self.probe_var.get().strip()
            start_str   = self.start_var.get().strip()
            output_path = self.output_var.get().strip()

            if not probe_path:
                messagebox.showwarning("Missing Input",
                                       "Please select a probe CSV file.")
                return
            if not self.device_paths:
                messagebox.showwarning("Missing Input",
                                       "Please add at least one device CSV file.")
                return
            if not start_str:
                messagebox.showwarning("Missing Input",
                                       "Please enter the probe start time.")
                return
            try:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                messagebox.showerror(
                    "Invalid Input",
                    "Start time must be in the format YYYY-MM-DD HH:MM:SS\n"
                    f"Got: {start_str!r}",
                )
                return

            # Clear the log and run the pipeline in a background thread so
            # the UI stays responsive during processing.
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.configure(state="disabled")

            registry = self.registry

            def worker():
                success = run_pipeline(
                    probe_path        = probe_path,
                    probe_time_col    = "Time",
                    probe_value_col   = self.probe_val_var.get().strip(),
                    start_dt          = start_dt,
                    device_paths      = list(self.device_paths),
                    output_path       = output_path,
                    registry          = registry,
                    log_fn            = self._log,
                )
                if success:
                    self.after(0, lambda: messagebox.showinfo(
                        "Done",
                        f"Chart saved to:\n{output_path}",
                    ))

            threading.Thread(target=worker, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — CLI FALLBACK
# Used when tkinter is unavailable OR when arguments are passed directly
# (e.g. from a script or CI pipeline).
# ══════════════════════════════════════════════════════════════════════════════

def cli_main(registry: dict):
    import argparse

    parser = argparse.ArgumentParser(
        description="Sync Additel 286 probe + multi-brand device loggers → chart.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Format detection is automatic.  Add new brands to logger_formats.json\n"
            "without modifying this script."
        ),
    )
    parser.add_argument("--probe",   required=True)
    parser.add_argument("--devices", required=True, nargs="+")
    parser.add_argument("--start",   required=True,
                        help='e.g. "2024-06-01 14:00:00"')
    parser.add_argument("--probe-time-col",  default="Time")
    parser.add_argument("--probe-value-col", default="Temperature (\u00b0C)")
    parser.add_argument("--output", default="chart.html")
    args = parser.parse_args()

    try:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        sys.exit(f"ERROR: --start must be YYYY-MM-DD HH:MM:SS, got: {args.start!r}")

    success = run_pipeline(
        probe_path      = args.probe,
        probe_time_col  = args.probe_time_col,
        probe_value_col = args.probe_value_col,
        start_dt        = start_dt,
        device_paths    = args.devices,
        output_path     = args.output,
        registry        = registry,
    )
    sys.exit(0 if success else 1)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    registry = load_registry()

    # GUI mode: no command-line arguments AND tkinter is available
    if HAS_TK and len(sys.argv) == 1:
        app = SyncApp(registry)
        app.mainloop()
    # CLI mode: arguments supplied, or tkinter is missing
    else:
        if not HAS_TK and len(sys.argv) == 1:
            print(
                "tkinter is not available on this system.\n"
                "Install it with:  sudo apt-get install python3-tk  (Linux)\n"
                "Then re-run without arguments for the GUI, or pass arguments "
                "directly for CLI mode (run with --help)."
            )
            sys.exit(1)
        cli_main(registry)
