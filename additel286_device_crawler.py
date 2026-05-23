"""
Additel 286 - Directory Browser & File Downloader
===================================================
Manually crawl the device file system using MMEMory:CATalog?
and download files by path. Tries both forward slash and backslash
path formats (the device runs Windows CE).

QUICK START
-----------
1. pip install pyusb
2. Download libusb-1.0.dll from https://libusb.info
3. Update LIBUSB_DLL_PATH below
4. Run: python additel286_device_crawler.py
"""

import usb.core
import usb.util
from usb.backend import libusb1
import time
import os
import sys

# =============================================================================
# USER SETTINGS
# =============================================================================

LIBUSB_DLL_PATH = r"C:\path\to\libusb-1.0.dll"  # <-- update this
VENDOR_ID       = 0x2E19
PRODUCT_ID      = 0x011E

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# USB CONNECTION
# =============================================================================

def connect():
    be = libusb1.get_backend(find_library=lambda x: LIBUSB_DLL_PATH)
    if be is None:
        raise RuntimeError(
            "Could not load libusb DLL from:\n  {0}\n\n"
            "Download from https://libusb.info and update LIBUSB_DLL_PATH.".format(LIBUSB_DLL_PATH)
        )
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID, backend=be)
    if dev is None:
        raise RuntimeError(
            "Device not found (VID=0x{0:04X}, PID=0x{1:04X}).\n\n"
            "Check:\n"
            "  * USB cable is plugged in and device is powered on\n"
            "  * Additel USB driver is installed\n"
            "  * PRODUCT_ID matches Device Manager".format(VENDOR_ID, PRODUCT_ID)
        )
    dev.set_configuration()
    intf = dev.get_active_configuration()[(0, 0)]
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
        usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in  = usb.util.find_descriptor(intf, custom_match=lambda e:
        usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    if not ep_out or not ep_in:
        raise RuntimeError("USB endpoints not found on device.")
    return dev, ep_out, ep_in


def send(dev, ep_out, ep_in, command, timeout_ms=5000, chunk=4096):
    """Send a SCPI command string and return the response (or None)."""
    ep_out.write(command.encode("utf-8"))
    time.sleep(0.15)
    try:
        parts = []
        while True:
            block = dev.read(ep_in.bEndpointAddress, chunk, timeout=timeout_ms)
            parts.append(bytes(block))
            if len(block) < chunk:
                break
        return b"".join(parts).decode("utf-8", errors="replace").strip()
    except usb.core.USBTimeoutError:
        return None
    except Exception as e:
        return "[ERROR: {0}]".format(e)

# =============================================================================
# FILE SYSTEM HELPERS
# =============================================================================

def catalog(dev, ep_out, ep_in, path=""):
    """
    Run MMEMory:CATalog? on a path and return a list of
    (name, kind, size) tuples where kind is FILE or DIRECTORY.
    """
    if path:
        cmd = "MMEMory:CATalog? \"{0}\"\r\n".format(path)
    else:
        cmd = "MMEMory:CATalog?\r\n"

    r = send(dev, ep_out, ep_in, cmd, timeout_ms=5000)
    if not r or r.startswith("[ERROR"):
        return [], r

    tokens = [t.strip().strip('"') for t in r.split(",")]
    entries = []
    i = 0
    while i + 2 < len(tokens):
        try:
            size = int(tokens[i])
            name = tokens[i + 1]
            kind = tokens[i + 2]
            if name:
                entries.append((name, kind, size))
            i += 3
        except ValueError:
            i += 1
    return entries, r


def try_path_variants(dev, ep_out, ep_in, base):
    """
    Try multiple path separator styles and return (entries, working_path).
    Windows CE uses backslashes; some firmware accepts forward slashes too.
    """
    variants = [
        base,
        base.replace("/", "\\"),
        "\\" + base.lstrip("\\/"),
        base.rstrip("\\/") + "\\",
    ]
    # Remove duplicates while preserving order
    seen = []
    for v in variants:
        if v not in seen:
            seen.append(v)

    for v in seen:
        entries, raw = catalog(dev, ep_out, ep_in, v)
        if entries:
            return entries, v, raw
    return [], base, None


def find_all_csv(dev, ep_out, ep_in, path, sep="\\", depth=0, max_depth=6):
    """Recursively find all CSV files under a given path."""
    if depth > max_depth:
        return []

    entries, raw = catalog(dev, ep_out, ep_in, path)
    csv_files = []

    for name, kind, size in entries:
        full = path + sep + name
        if kind == "FILE" and name.lower().endswith(".csv"):
            csv_files.append((full, size))
        elif kind == "DIRECTORY":
            indent = "  " * (depth + 1)
            print(indent + "-> " + full)
            csv_files.extend(find_all_csv(dev, ep_out, ep_in, full, sep, depth + 1, max_depth))

    return csv_files

# =============================================================================
# MENU ACTIONS
# =============================================================================

def action_browse_manual(dev, ep_out, ep_in):
    """Catalog any path the user types, trying multiple separator styles."""
    path = input("\n  Path to browse (press Enter for root): ").strip()

    if path:
        print("\n  Trying path variants for: " + path)
        entries, used, raw = try_path_variants(dev, ep_out, ep_in, path)
        if entries:
            print("  Working path: \"" + used + "\"\n")
        else:
            # Fall back to raw catalog output even if parsing failed
            _, raw = catalog(dev, ep_out, ep_in, path)
            print("  Raw response: " + str(raw))
            return
    else:
        entries, raw = catalog(dev, ep_out, ep_in, "")
        used = "(root)"

    if entries:
        print("  Contents of [{0}]:".format(used))
        print("  {0:<40} {1:<12} {2}".format("Name", "Type", "Size"))
        print("  " + "-" * 60)
        for name, kind, size in entries:
            print("  {0:<40} {1:<12} {2}".format(name, kind, size))
        print("\n  Total: {0} item(s)".format(len(entries)))
    else:
        print("  No response or empty directory.")
        print("  Raw response: " + str(raw))


def action_deep_scan(dev, ep_out, ep_in):
    """Scan all known root directories recursively for CSV files."""
    print("\n-- Deep Scan for CSV Files --")
    root_dirs = ["TAU", "StartUp", "FTDI", "IME", "Windows"]

    all_csv = []
    for root in root_dirs:
        print("\n  Scanning /{0} ...".format(root))
        entries, used, raw = try_path_variants(dev, ep_out, ep_in, root)

        if not entries:
            print("  No response.")
            continue

        sep = "\\" if "\\" in used else "/"
        print("  Found {0} item(s) at root. Recursing...".format(len(entries)))
        csv_in_dir = find_all_csv(dev, ep_out, ep_in, used.rstrip("\\/"), sep, depth=1)
        all_csv.extend(csv_in_dir)

    if all_csv:
        print("\n  Found {0} CSV file(s):".format(len(all_csv)))
        for i, (path, size) in enumerate(all_csv, 1):
            print("    [{0}] {1}  ({2} bytes)".format(i, path, size))

        confirm = input("\n  Download all? (y/n): ").strip().lower()
        if confirm == "y":
            download_list(dev, ep_out, ep_in, [p for p, s in all_csv])
    else:
        print("\n  No CSV files found in any directory.")


def action_download_by_path(dev, ep_out, ep_in):
    """Download a single file whose path the user types."""
    path = input("\n  Full file path on device (e.g. TAU\\DAQ\\log.csv): ").strip()
    if not path:
        return
    download_list(dev, ep_out, ep_in, [path])


def download_list(dev, ep_out, ep_in, file_paths):
    print("\n-- Downloading --")
    for filepath in file_paths:
        cmd      = "MMEMory:DATA? \"{0}\"\r\n".format(filepath)
        filename = os.path.basename(filepath.replace("\\", "/"))
        savepath = os.path.join(OUTPUT_DIR, filename)

        print("  Requesting: " + filepath)
        r = send(dev, ep_out, ep_in, cmd, timeout_ms=20000)
        if r and not r.startswith("[ERROR"):
            with open(savepath, "w") as out:
                out.write(r)
            print("  Saved -> " + savepath)
        else:
            print("  Failed. Response: " + str(r))
    print("\n  Done. Files saved to: " + OUTPUT_DIR)


def action_identify(dev, ep_out, ep_in):
    print("\n-- Device Identity --")
    r = send(dev, ep_out, ep_in, "*IDN?\r\n")
    print("  " + r if r else "  No response.")


def action_raw_command(dev, ep_out, ep_in):
    """Send any raw command and print the response."""
    cmd = input("\n  Enter command (without \\r\\n): ").strip()
    if not cmd:
        return
    r = send(dev, ep_out, ep_in, cmd + "\r\n", timeout_ms=5000)
    print("  Response: " + str(r))

# =============================================================================
# MAIN MENU
# =============================================================================

def main():
    print("=" * 52)
    print("  Additel 286  --  Directory Browser")
    print("=" * 52)

    try:
        dev, ep_out, ep_in = connect()
        print("  Connected  (VID=0x{0:04X}, PID=0x{1:04X})\n".format(VENDOR_ID, PRODUCT_ID))
    except RuntimeError as e:
        print("\n[Connection failed]\n" + str(e))
        return

    menu = [
        ("1", "Identify device",                action_identify),
        ("2", "Browse a path manually",          action_browse_manual),
        ("3", "Deep scan all directories",       action_deep_scan),
        ("4", "Download a file by path",         action_download_by_path),
        ("5", "Send a raw command",              action_raw_command),
        ("q", "Quit",                            None),
    ]

    while True:
        print("\n-- Menu --")
        for key, label, _ in menu:
            print("  {0}) {1}".format(key, label))
        choice = input("\n  Choose: ").strip().lower()
        if choice == "q":
            print("  Goodbye.")
            break
        else:
            for key, label, action in menu:
                if choice == key and action:
                    action(dev, ep_out, ep_in)
                    break
            else:
                print("  Invalid choice.")


if __name__ == "__main__":
    main()
