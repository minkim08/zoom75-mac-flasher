#!/usr/bin/env python3
"""
zoom75_flash.py - Flash Meletrix Zoom75 (wireless/tri-mode, AT32F415) firmware from macOS.

Reimplements what WuqueDFUupgradeProgrammer.exe does:
  1. Send the "enter DFU" command (0x1302) over the keyboard's raw HID interface.
  2. Keyboard reboots into Artery's ROM USB DFU bootloader (VID 2E3C, PID DF11).
  3. Write the firmware to 0x08000000 with dfu-util (DfuSe mode).

Requirements (macOS):
  brew install dfu-util hidapi
  python3 -m pip install hidapi

Usage:
  python3 zoom75_flash.py firmware.hex            # full flash
  python3 zoom75_flash.py firmware.hex --dry-run  # check everything, write nothing
  python3 zoom75_flash.py firmware.hex --skip-hid # board already in DFU mode
  python3 zoom75_flash.py --list                  # show detected devices
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

# ---- Constants recovered from WuqueDFUupgradeProgrammer.exe ------------------
DFU_VID, DFU_PID = 0x2E3C, 0xDF11           # Artery ROM DFU bootloader
FLASH_BASE = 0x08000000
FLASH_SIZE = 256 * 1024                     # AT32F415RCT7: 256 KB
CMD_ENTER_DFU = 0x1302

# Keyboard HID matching, as in ConnectDevice():
#   vendor protocol -> interface 2, 64-byte frames
#   VIA protocol    -> interface 1, 32-byte frames
VENDOR_VIDS = {0x1EA7, 0x05AC}
VIA_VIDS = {0x1EA7, 0xFEED, 0x05AC}
VIA_VID_PREFIX = 0x5540                     # tool matches "vid_554x" (Meletrix MCUs, e.g. 0x5542)

CRC16_POLY = 0x1021


# ---- CRC-16/CCITT-FALSE (matches the tool's CRC16_TABLE, init 0xFFFF) --------
def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ CRC16_POLY) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def build_frame(cmd: int, frame_len: int) -> bytes:
    """Mirror AssembleSendCommandFrame for 0x13xx commands with no params.

    Layout: [cmd hi][cmd lo][00 00 00 00][crc lo][crc hi][00 ... pad to frame_len]
    CRC is computed over the full padded frame with the CRC bytes zeroed.
    """
    body = bytearray(frame_len)
    body[0] = (cmd >> 8) & 0xFF
    body[1] = cmd & 0xFF
    crc = crc16_ccitt_false(bytes(body))
    body[6] = crc & 0xFF          # tool emits low byte first ("X2" of crc&0xFF, then crc>>8)
    body[7] = (crc >> 8) & 0xFF
    return bytes(body)


# ---- Intel HEX -> flat binary ------------------------------------------------
def hex_to_bin(path: str):
    mem = {}
    base = 0
    with open(path, "r") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise ValueError(f"line {lineno}: not an Intel HEX record")
            rec = bytes.fromhex(line[1:])
            if sum(rec) & 0xFF:
                raise ValueError(f"line {lineno}: bad checksum")
            count, addr, rtype = rec[0], (rec[1] << 8) | rec[2], rec[3]
            data = rec[4:4 + count]
            if rtype == 0x00:
                for i, b in enumerate(data):
                    mem[base + addr + i] = b
            elif rtype == 0x01:
                break
            elif rtype == 0x02:
                base = int.from_bytes(data, "big") << 4
            elif rtype == 0x04:
                base = int.from_bytes(data, "big") << 16
            # 0x03 / 0x05 (start address) ignored
    if not mem:
        raise ValueError("hex file contains no data")
    lo, hi = min(mem), max(mem) + 1
    blob = bytearray(b"\xFF" * (hi - lo))
    for a, b in mem.items():
        blob[a - lo] = b
    return lo, bytes(blob)


def sanity_check_image(start: int, blob: bytes):
    if start != FLASH_BASE:
        sys.exit(f"Refusing: image starts at {start:#010x}, expected {FLASH_BASE:#010x}.")
    if len(blob) > FLASH_SIZE:
        sys.exit(f"Refusing: image is {len(blob)} bytes, flash is {FLASH_SIZE}.")
    sp = int.from_bytes(blob[0:4], "little")
    reset = int.from_bytes(blob[4:8], "little")
    if not (0x20000000 <= sp <= 0x20020000):
        sys.exit(f"Refusing: initial SP {sp:#010x} doesn't look like SRAM.")
    if not (FLASH_BASE <= (reset & ~1) < FLASH_BASE + len(blob)) or not (reset & 1):
        sys.exit(f"Refusing: reset vector {reset:#010x} is outside the image or not Thumb.")
    print(f"Image OK: {len(blob)} bytes @ {start:#010x}, SP={sp:#010x}, reset={reset:#010x}")


# ---- HID side ----------------------------------------------------------------
def import_hid():
    try:
        import hid  # from the 'hidapi' pip package
    except ImportError as e:
        sys.exit(f"Could not import 'hid': {e}\n"
                 f"This script is running under: {sys.executable}\n"
                 f"Install hidapi into that exact interpreter:\n"
                 f"  {sys.executable} -m pip install hidapi\n"
                 f"(add --user --break-system-packages if pip refuses)")
    if not hasattr(hid, "enumerate") or not hasattr(hid, "device"):
        sys.exit(f"The 'hid' module at {getattr(hid, '__file__', '?')} isn't the hidapi package "
                 f"(probably the unrelated 'hid' package). Fix with:\n"
                 f"  {sys.executable} -m pip uninstall hid\n"
                 f"  {sys.executable} -m pip install hidapi")
    return hid


def classify(dev):
    vid, iface = dev["vendor_id"], dev.get("interface_number", -1)
    if iface == 1 and (vid in VIA_VIDS or (vid & 0xFFF0) == VIA_VID_PREFIX):
        return "via", 32
    # Vendor path needs a vendor-defined collection; plain keyboard/consumer
    # collections (usage page 0x0001 / 0x000c) can't take command frames.
    if iface == 2 and vid in VENDOR_VIDS and dev.get("usage_page", 0) >= 0xFF00:
        return "vendor", 64
    return None, None


def find_keyboard(hid):
    hits = []
    for d in hid.enumerate():
        kind, flen = classify(d)
        if kind:
            hits.append((d, kind, flen))
    return hits


def list_devices():
    hid = import_hid()
    print("Candidate keyboard interfaces:")
    hits = find_keyboard(hid)
    hits.sort(key=lambda h: h[0].get("usage_page", 0) != 0xFF60)
    for n, (d, kind, flen) in enumerate(hits):
        mark = "  <- will use" if n == 0 else ""
        print(f"  {d['vendor_id']:04x}:{d['product_id']:04x} if={d.get('interface_number')} "
              f"usage_page={d.get('usage_page', 0):#06x} mode={kind} frame={flen} "
              f"'{d.get('product_string')}'{mark}")
    if not hits:
        print("  (none)")
    print("DFU bootloader present:", dfu_present())


def send_enter_dfu(dry_run: bool):
    hid = import_hid()
    hits = find_keyboard(hid)
    if not hits:
        sys.exit("Keyboard not found. Plug it in with a USB cable (wired mode), then retry.\n"
                 "Run with --list to see what's detected.")
    # Prefer usage page 0xFF60 (VIA raw HID) if several interfaces match
    hits.sort(key=lambda h: h[0].get("usage_page", 0) != 0xFF60)
    d, kind, flen = hits[0]
    frame = build_frame(CMD_ENTER_DFU, flen)
    print(f"Keyboard: {d['vendor_id']:04x}:{d['product_id']:04x} '{d.get('product_string')}' "
          f"({kind}, {flen}-byte frames)")
    print("Enter-DFU frame:", frame.hex(" "))
    if dry_run:
        print("[dry-run] not sending.")
        return
    dev = hid.device()
    dev.open_path(d["path"])
    try:
        dev.write(b"\x00" + frame)   # report ID 0 + frame, same as the tool
    finally:
        try:
            dev.close()
        except Exception:
            pass


# ---- DFU side ----------------------------------------------------------------
def dfu_present() -> bool:
    if not shutil.which("dfu-util"):
        return False
    out = subprocess.run(["dfu-util", "-l"], capture_output=True, text=True).stdout
    return f"{DFU_VID:04x}:{DFU_PID:04x}" in out.lower()


def wait_for_dfu(timeout=10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if dfu_present():
            return True
        time.sleep(0.5)
    return False


def flash(binpath: str, dry_run: bool):
    cmd = ["dfu-util", "-d", f"{DFU_VID:04x}:{DFU_PID:04x}", "-a", "0",
           "-s", f"{FLASH_BASE:#010x}:leave", "-D", binpath]
    print("Running:", " ".join(cmd))
    if dry_run:
        print("[dry-run] not flashing.")
        return 0
    return subprocess.run(cmd).returncode


def main():
    ap = argparse.ArgumentParser(description="Flash Zoom75 wireless firmware from macOS")
    ap.add_argument("hex", nargs="?", help="Firmware .hex from meletrix.com")
    ap.add_argument("--dry-run", action="store_true", help="Validate and print, change nothing")
    ap.add_argument("--skip-hid", action="store_true", help="Board is already in DFU mode")
    ap.add_argument("--list", action="store_true", help="List detected devices and exit")
    a = ap.parse_args()

    if a.list:
        list_devices()
        return
    if not a.hex:
        ap.error("firmware .hex required")
    if not shutil.which("dfu-util") and not a.dry_run:
        sys.exit("Missing dfu-util. Run: brew install dfu-util")

    start, blob = hex_to_bin(a.hex)
    sanity_check_image(start, blob)
    tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    tmp.write(blob)
    tmp.close()

    try:
        if not a.skip_hid:
            if dfu_present():
                print("Board is already in DFU mode, skipping HID step.")
            else:
                send_enter_dfu(a.dry_run)
                if not a.dry_run:
                    print("Waiting for DFU bootloader (2E3C:DF11)...")
                    if not wait_for_dfu():
                        sys.exit("Bootloader didn't appear within 10s. Check `dfu-util -l`; if "
                                 "2e3c:df11 shows up late, rerun with --skip-hid. Run --list "
                                 "to see which keyboard interface was used.")
        elif not a.dry_run and not dfu_present():
            sys.exit("No DFU device (2E3C:DF11) found. Check `dfu-util -l`.")

        rc = flash(tmp.name, a.dry_run)
        # dfu-util sometimes exits non-zero on ':leave' after a successful write
        if rc == 0:
            print("Done. Keyboard should reboot into the new firmware.")
        else:
            print(f"dfu-util exited with {rc}. If the log shows 'Download done' / "
                  f"'File downloaded successfully', the write succeeded and only the "
                  f"post-flash reset complained. Unplug and replug the keyboard.")
            sys.exit(rc)
    finally:
        os.unlink(tmp.name)


if __name__ == "__main__":
    main()
