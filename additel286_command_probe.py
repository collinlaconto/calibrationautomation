"""
Additel 286 - Command Prober
=============================
Sends a broad set of candidate SCPI commands and reports which ones
the device responds to. Use this to discover what commands the ADT286 supports.

QUICK START
-----------
1. pip install pyusb
2. Download libusb-1.0.dll from https://libusb.info
3. Update LIBUSB_DLL_PATH below
4. Run: python additel286_command_probe.py
"""

import usb.core
import usb.util
from usb.backend import libusb1
import time
import sys

# =============================================================================
# USER SETTINGS
# =============================================================================

LIBUSB_DLL_PATH = r"C:\path\to\libusb-1.0.dll"  # <-- update this
VENDOR_ID       = 0x2E19
PRODUCT_ID      = 0x011E

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


def send(dev, ep_out, ep_in, command, timeout_ms=2000, chunk=4096):
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
# PROBE
# =============================================================================

def run_probe(dev, ep_out, ep_in):
    candidates = [
        # -- Standard IEEE 488.2 ------------------------------------------
        ("Identity",                "*IDN?\r\n"),
        ("Reset",                   "*RST\r\n"),
        ("Error queue",             "SYST:ERR?\r\n"),
        # -- Standard SCPI memory/file commands ---------------------------
        ("Catalog (root)",          "MMEMory:CATalog?\r\n"),
        ("Catalog /DAQ",            "MMEMory:CATalog? \"/DAQ\"\r\n"),
        ("Catalog /data",           "MMEMory:CATalog? \"/data\"\r\n"),
        ("Catalog /log",            "MMEMory:CATalog? \"/log\"\r\n"),
        ("Catalog TAU",             "MMEMory:CATalog? \"TAU\"\r\n"),
        ("Catalog TAU\\DAQ",        "MMEMory:CATalog? \"TAU\\DAQ\"\r\n"),
        ("Catalog TAU\\data",       "MMEMory:CATalog? \"TAU\\data\"\r\n"),
        # -- Additel-style DAQ commands -----------------------------------
        ("DAQ file list",           "DAQ:FILE:LIST?\r\n"),
        ("DAQ file catalog",        "DAQ:FILE:CAT?\r\n"),
        ("DAQ data list",           "DAQ:DATA:LIST?\r\n"),
        ("DAQ log list",            "DAQ:LOG:LIST?\r\n"),
        ("DAQ record list",         "DAQ:REC:LIST?\r\n"),
        ("DAQ count",               "DAQ:COUNT?\r\n"),
        ("DAQ name",                "DAQ:NAME?\r\n"),
        # -- Data management commands -------------------------------------
        ("Data list",               "DATA:LIST?\r\n"),
        ("Data catalog",            "DATA:CAT?\r\n"),
        ("Data file list",          "DATA:FILE:LIST?\r\n"),
        ("Data count",              "DATA:COUNT?\r\n"),
        ("Memory catalog",          "MEM:CAT?\r\n"),
        ("Memory data",             "MEM:DATA?\r\n"),
        # -- Application / log data ---------------------------------------
        ("App data list",           "APP:DATA:LIST?\r\n"),
        ("App file list",           "APP:FILE:LIST?\r\n"),
        ("Log list",                "LOG:LIST?\r\n"),
        ("Log count",               "LOG:COUNT?\r\n"),
        ("Log file list",           "LOG:FILE:LIST?\r\n"),
        # -- Measurement queries (sanity check) ---------------------------
        ("Measure temp ch1",        "MEAS:TEMP1?\r\n"),
        ("Measure temp ch2",        "MEAS:TEMP2?\r\n"),
        ("Fetch",                   "FETC?\r\n"),
        ("Read",                    "READ?\r\n"),
        # -- System info --------------------------------------------------
        ("System version",          "SYST:VERS?\r\n"),
        ("System date",             "SYST:DATE?\r\n"),
        ("System time",             "SYST:TIME?\r\n"),
    ]

    print("\n-- Running Command Probe --")
    print("  Trying {0} candidate commands...\n".format(len(candidates)))

    found = []
    for label, cmd in candidates:
        sys.stdout.write("  {0:<30} -> ".format(label))
        sys.stdout.flush()
        r = send(dev, ep_out, ep_in, cmd)
        if r and not r.startswith("[ERROR"):
            print("RESPONSE: " + r[:120])
            found.append((label, cmd, r))
        else:
            print("(no response)")
        time.sleep(0.1)

    print("\n-- Summary --")
    if found:
        print("  {0} command(s) returned a response:\n".format(len(found)))
        for label, cmd, r in found:
            print("  [{0}]".format(label))
            print("    Command : " + cmd.strip())
            print("    Response: " + r[:300] + "\n")
    else:
        print("  No commands returned a response.")
        print("\n  Possible causes:")
        print("  * PRODUCT_ID is wrong -- re-check Device Manager")
        print("  * Additel USB driver not installed")
        print("  * Device not in a ready state")

    return found


def action_custom(dev, ep_out, ep_in):
    """Send a custom command typed by the user."""
    cmd = input("\n  Enter command (without \\r\\n, e.g. MMEMory:CATalog?): ").strip()
    if not cmd:
        return
    full_cmd = cmd + "\r\n"
    print("  Sending: " + full_cmd.strip())
    r = send(dev, ep_out, ep_in, full_cmd, timeout_ms=5000)
    if r:
        print("  Response: " + r)
    else:
        print("  No response.")

# =============================================================================
# MAIN MENU
# =============================================================================

def main():
    print("=" * 52)
    print("  Additel 286  --  Command Prober")
    print("=" * 52)

    try:
        dev, ep_out, ep_in = connect()
        print("  Connected  (VID=0x{0:04X}, PID=0x{1:04X})\n".format(VENDOR_ID, PRODUCT_ID))
    except RuntimeError as e:
        print("\n[Connection failed]\n" + str(e))
        return

    menu = [
        ("1", "Run full probe (all candidate commands)", run_probe),
        ("2", "Send a custom command manually",          action_custom),
        ("q", "Quit",                                   None),
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
