"""
Collin Laconto + Claude


probe_sync.py — Sync probe (elapsed-time) + device (absolute-time) CSVs and plot them.

Usage:
    python probe_sync.py \
        --probe probe.csv \
        --devices dev1.csv dev2.csv dev3.csv \
        --start "2024-06-01 09:00:00" \
        [--probe-time-col "Time"] \
        [--probe-value-col "Value"] \
        [--device-time-col "Timestamp"] \
        [--device-value-col "Value"] \
        [--output chart.html]

Dependencies:
    pip install pandas plotly
"""

import argparse
import sys
import os
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import re


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — ELAPSED TIME PARSING
# The probe records time as elapsed duration ("03:42" or "01:03:42")
# rather than a timestamp. These functions convert that to seconds.
# ══════════════════════════════════════════════════════════════════════════════

def parse_elapsed(value: str) -> float:
    """
    Convert one elapsed-time string into a plain float of total seconds.

    Handles three formats:
      HH:MM:SS  e.g. "01:03:42"  →  3822.0 s
      MM:SS     e.g. "03:42"     →   222.0 s
      plain     e.g. "222"       →   222.0 s

    Decimal seconds are supported in all formats, e.g. "03:42.5" → 222.5 s.
    Raises ValueError if the string can't be parsed.
    """
    value = str(value).strip()

    # Try HH:MM:SS (or H:MM:SS — the hours group can be any width)
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", value)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    # Try MM:SS (minutes can be any width)
    m = re.fullmatch(r"(\d+):(\d{2}(?:\.\d+)?)", value)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))

    # Fall back to interpreting the value as a plain number of seconds
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"Cannot parse elapsed time: '{value}'")


def stitch_elapsed(raw_seconds: pd.Series) -> pd.Series:
    """
    Fix the probe's 1-hour clock reset so time becomes monotonically increasing.

    The probe clock counts up to 3600 s (00:00 → 59:59) then resets to 00:00.
    For a 90-minute test that means one reset; a 3-hour test would have two, etc.

    Strategy:
      1. Compute the difference between each row and the one before it (.diff()).
      2. Anywhere that difference is negative, the clock has reset — flag it True.
      3. .cumsum() converts those flags into a running count of resets seen so far
         (0 before any reset, 1 after the first, 2 after the second, …).
      4. Multiply by 3600 to get the cumulative seconds to add back.
      5. Add to the raw values → a smooth, ever-increasing timeline.

    Example (simplified):
      raw:      [0, 10, 20, 2, 12]   ← reset between 20 and 2
      diff:     [NaN, 10, 10, -18, 10]
      diff < 0: [False, False, False, True, False]
      cumsum:   [0, 0, 0, 1, 1]
      offset:   [0, 0, 0, 3600, 3600]
      result:   [0, 10, 20, 3602, 3612]  ← continuous again
    """
    offsets = (raw_seconds.diff() < 0).cumsum() * 3600
    return raw_seconds + offsets


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — FILE LOADING
# One function per file type. Both return a tidy two-column DataFrame:
#   _abs_time  (datetime)  — wall-clock time, ready to plot on a shared axis
#   _value     (float)     — the measurement
# ══════════════════════════════════════════════════════════════════════════════

def load_probe(path: str, time_col: str, value_col: str,
               start: datetime) -> pd.DataFrame:
    """
    Load the probe CSV and convert its elapsed timestamps to absolute datetimes.

    Steps:
      1. Read the CSV into a DataFrame.
      2. Strip any accidental whitespace from column names.
      3. Verify the expected columns exist (gives a clear error if not).
      4. Parse every value in the time column into seconds via parse_elapsed().
      5. Stitch any clock resets into a continuous timeline via stitch_elapsed().
      6. Add the elapsed seconds to the user-supplied start datetime to get
         real wall-clock timestamps.
      7. Convert the value column to numbers (bad rows become NaN, then drop).
      8. Return only the two columns we need.
    """
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()  # remove leading/trailing spaces from headers

    # Guard against mistyped column names — surface a helpful message
    if time_col not in df.columns:
        raise KeyError(
            f"Probe file: column '{time_col}' not found. "
            f"Available columns: {list(df.columns)}"
        )
    if value_col not in df.columns:
        raise KeyError(
            f"Probe file: column '{value_col}' not found. "
            f"Available columns: {list(df.columns)}"
        )

    # Parse every elapsed-time string into a float of seconds
    raw_s = df[time_col].apply(parse_elapsed)

    # Repair clock resets so time is monotonically increasing
    stitched_s = stitch_elapsed(raw_s)

    # Anchor to the real start time: start + elapsed seconds = wall-clock time
    df["_abs_time"] = [start + timedelta(seconds=s) for s in stitched_s]

    # Coerce the value column to numeric; anything unparseable becomes NaN
    df["_value"] = pd.to_numeric(df[value_col], errors="coerce")

    # Keep only the two output columns and drop any rows with NaN in either
    return df[["_abs_time", "_value"]].dropna()


def load_device(path: str, time_col: str, value_col: str) -> pd.DataFrame:
    """
    Load a device CSV whose timestamps are already absolute (e.g. "2024-06-01 09:05:00").

    Steps:
      1. Read the CSV and strip column-name whitespace.
      2. Verify the expected columns exist.
      3. Parse the timestamp column with pandas' flexible datetime parser —
         it handles most common formats automatically.
      4. Coerce the value column to numeric, drop bad rows.
      5. Return the tidy two-column DataFrame.
    """
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()

    if time_col not in df.columns:
        raise KeyError(
            f"Device file '{path}': column '{time_col}' not found. "
            f"Available columns: {list(df.columns)}"
        )
    if value_col not in df.columns:
        raise KeyError(
            f"Device file '{path}': column '{value_col}' not found. "
            f"Available columns: {list(df.columns)}"
        )

    # infer_datetime_format lets pandas auto-detect the format (ISO, US, etc.)
    df["_abs_time"] = pd.to_datetime(df[time_col], infer_datetime_format=True)
    df["_value"] = pd.to_numeric(df[value_col], errors="coerce")

    return df[["_abs_time", "_value"]].dropna()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — CHART BUILDING
# Takes the cleaned DataFrames and assembles an interactive Plotly figure.
# Each dataset becomes one "trace" (a line on the chart).
# ══════════════════════════════════════════════════════════════════════════════

def build_figure(probe_df, device_dfs, device_names):
    """
    Build and return a Plotly Figure with one trace per dataset.

    The probe is drawn as a thin line (no markers) because its high sample
    rate (4 s) would make individual dot markers illegible.

    Each device is drawn as a thicker line with dots at each sample point,
    making the lower 1-minute resolution clearly visible.

    hovermode="x unified" means hovering anywhere on the chart shows a single
    tooltip with the values from all traces at the nearest x position.
    """
    fig = go.Figure()

    # ── Probe trace ──────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=probe_df["_abs_time"],   # x-axis: wall-clock datetime
        y=probe_df["_value"],      # y-axis: measurement
        mode="lines",              # lines only — no markers at 4-s resolution
        name="Probe",
        line=dict(width=1.5, color="#00b4d8"),
    ))

    # Colour palette — cycles if there are more than 8 devices
    colors = [
        "#ef233c", "#f77f00", "#2dc653", "#9b5de5",
        "#f15bb5", "#fee440", "#00bbf9", "#fb5607",
    ]

    # ── Device traces (one per file) ─────────────────────────────────────────
    for i, (df, name) in enumerate(zip(device_dfs, device_names)):
        fig.add_trace(go.Scatter(
            x=df["_abs_time"],
            y=df["_value"],
            mode="lines+markers",              # dots show the 1-min sample points
            name=name,                          # filename (without .csv) as label
            line=dict(width=2, color=colors[i % len(colors)]),
            marker=dict(size=5),
        ))

    # ── Layout ───────────────────────────────────────────────────────────────
    fig.update_layout(
        title="Probe + Device Data — Synchronized Timeline",
        xaxis_title="Time",
        yaxis_title="Value",
        template="plotly_dark",        # dark background theme
        hovermode="x unified",         # single tooltip across all traces
        # Place the legend horizontally above the chart instead of inside it
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
        font=dict(family="monospace", size=12),
    )
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — COMMAND-LINE INTERFACE
# Wires everything together. Reads arguments, calls the loaders, builds the
# figure, and writes the output HTML file.
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # argparse handles --flag value parsing and generates --help automatically
    parser = argparse.ArgumentParser(
        description="Sync probe (elapsed-time) + device (absolute-time) CSVs and plot."
    )

    # Required arguments
    parser.add_argument("--probe",   required=True, help="Path to probe CSV")
    parser.add_argument("--devices", required=True, nargs="+",
                        help="Paths to one or more device CSVs (space-separated)")
    parser.add_argument("--start",   required=True,
                        help='Probe start datetime, e.g. "2024-06-01 09:00:00"')

    # Optional column-name overrides (defaults match common export formats)
    parser.add_argument("--probe-time-col",  default="Time",
                        help="Elapsed-time column in probe CSV (default: Time)")
    parser.add_argument("--probe-value-col", default="Value",
                        help="Value column in probe CSV (default: Value)")
    parser.add_argument("--device-time-col", default="Timestamp",
                        help="Timestamp column in device CSVs (default: Timestamp)")
    parser.add_argument("--device-value-col", default="Value",
                        help="Value column in device CSVs (default: Value)")

    # Output
    parser.add_argument("--output", default="chart.html",
                        help="Output HTML file path (default: chart.html)")

    args = parser.parse_args()

    # ── Parse and validate the start datetime ────────────────────────────────
    try:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        print(f"ERROR: --start must be 'YYYY-MM-DD HH:MM:SS', got: {args.start}")
        sys.exit(1)

    # ── Load probe ───────────────────────────────────────────────────────────
    print(f"Loading probe: {args.probe}")
    probe_df = load_probe(
        args.probe, args.probe_time_col, args.probe_value_col, start_dt
    )
    print(f"  {len(probe_df):,} rows | "
          f"{probe_df['_abs_time'].min()} → {probe_df['_abs_time'].max()}")

    # ── Load each device CSV ─────────────────────────────────────────────────
    device_dfs, device_names = [], []
    for path in args.devices:
        # Use the filename (without extension) as the legend label
        name = os.path.splitext(os.path.basename(path))[0]
        print(f"Loading device: {path}  (label: '{name}')")
        df = load_device(path, args.device_time_col, args.device_value_col)
        print(f"  {len(df):,} rows | "
              f"{df['_abs_time'].min()} → {df['_abs_time'].max()}")
        device_dfs.append(df)
        device_names.append(name)

    # ── Build and save the chart ─────────────────────────────────────────────
    print("Building chart…")
    fig = build_figure(probe_df, device_dfs, device_names)

    # write_html produces a self-contained file — no internet needed to open it
    fig.write_html(args.output)
    print(f"Done! Open in your browser: {args.output}")


# Only run main() when this file is executed directly (not when imported)
if __name__ == "__main__":
    main()
