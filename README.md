# zoom75-mac-flasher

Flash firmware onto a **Meletrix Zoom75 Wireless** (tri-mode) keyboard from macOS. No Windows, no VM.

Meletrix's official updater, `WuqueDFUupgradeProgrammer.exe`, only runs on Windows, and it usually fails inside Parallels or UTM because the keyboard re-enumerates as a new USB device partway through the flash. This script reimplements the same process natively using `hidapi` and `dfu-util`.

> [!WARNING]
> Use at your own risk. The author is not responsible for any damage to your keyboard or other hardware. See [Disclaimer](#disclaimer).

## Requirements

- macOS (should also work on Linux)
- Python 3.8+
- [dfu-util](https://dfu-util.sourceforge.net/) and [hidapi](https://github.com/libusb/hidapi)

```sh
brew install dfu-util hidapi
python3 -m pip install hidapi
```

If pip refuses with "externally managed environment", add `--user --break-system-packages`.

## Usage

1. Download the firmware `.hex` for your keyboard from the [Meletrix firmware page](https://meletrix.com/pages/firmwares).
2. Connect the keyboard **with a USB cable**, in wired mode.
3. Check the keyboard is detected:

   ```sh
   python3 zoom75_flash.py --list
   ```

   You should see one interface with `usage_page=0xff60`, marked `<- will use`.

4. Do a dry run. This validates the image and prints what would be sent, without touching the keyboard:

   ```sh
   python3 zoom75_flash.py firmware.hex --dry-run
   ```

5. Flash:

   ```sh
   python3 zoom75_flash.py firmware.hex
   ```

6. When `dfu-util` says `File downloaded successfully`, unplug and replug the keyboard.

### Options

| Flag | What it does |
| --- | --- |
| `--list` | Show detected keyboard interfaces and whether the DFU bootloader is present |
| `--dry-run` | Validate the image and print the commands, but change nothing |
| `--skip-hid` | Skip the enter-bootloader command, for when the board is already in DFU mode |

## Expected output

The end of a successful flash looks like this:

```
Download done.
File downloaded successfully
Submitting leave request...
dfu-util: Error during download get_status
```

**That last error is harmless.** The chip resets as soon as it's asked to, so it disappears before `dfu-util` can read its status. The script exits non-zero in this case and tells you to replug, which is all you need to do.

## Compatibility

| Board | Status |
| --- | --- |
| Zoom75 Wireless (`WQ206A1...` firmware) | Tested, firmware `SV3.0.0_20250412` |
| Zoom75 TIGA, Zoom98, Zoom TKL, ZoomPad TIGA, Zoom TKL DYNA | Untested. These use the same Windows tool and `WQ2xx` `.hex` firmware, so they should work |
| Zoom75 wired, Zoom65 V3 | Not applicable. These run QMK, use [QMK Toolbox](https://github.com/qmk/qmk_toolbox) |
| Zoom75 HE, BOOG75, Zoom65 V3 HE | Not supported. Different updater |

If you try it on an untested board, please open an issue with the `--list` output and whether it worked.

## Safety

- **Only flash firmware made for your exact board.** The script checks that the image is well-formed but can't tell one model's firmware from another's. The filename prefix identifies the model (`WQ206A1` is the Zoom75 Wireless, `WQ206B1` is the Zoom75 TIGA).
- **Don't unplug during the erase or download steps.**
- **It's hard to permanently brick.** The DFU bootloader lives in the chip's ROM and can't be overwritten. If a bad flash leaves the keyboard unresponsive, the bootloader can still be reached by opening the board and pulling the MCU's BOOT0 pin high while plugging in, then running with `--skip-hid`.

## How it works

The Windows updater is a .NET application wrapping Artery's DFU libraries. Decompiling it showed the process is:

1. **Find the keyboard's command interface.** For VIA-style boards this is HID interface 1, usage page `0xFF60`, with 32-byte reports. (The tool also supports a vendor-protocol variant on interface 2 with 64-byte reports.)
2. **Send the enter-bootloader command `0x1302`.** The frame is:

   ```
   13 02 00 00 00 00 [CRC lo] [CRC hi] 00 ... (zero-padded to the report size)
   ```

   The CRC is CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`), computed over the whole padded frame with the CRC bytes zeroed, and stored low byte first. For a 32-byte frame that's `30 d2`.

3. **The keyboard reboots into the Artery AT32 ROM bootloader**, which shows up as a standard USB DFU device, VID `2E3C`, PID `DF11`. It's DfuSe-compatible, so `dfu-util` works with no drivers on macOS.
4. **Write the image to `0x08000000`** (the start of internal flash on the AT32F415) with `dfu-util`, then reset.

The script converts the Intel HEX to a flat binary itself and refuses to flash anything that doesn't start at `0x08000000`, doesn't fit in 256 KB, or doesn't have a plausible stack pointer and reset vector.

## Troubleshooting

**"Could not import 'hid'"**: `pip3` and `python3` point at different Pythons. Install with `python3 -m pip install hidapi`, which uses the same interpreter that runs the script. If you previously installed the unrelated `hid` package, remove it with `python3 -m pip uninstall hid`.

**Keyboard not found**: Make sure it's connected by cable and switched to wired mode. Bluetooth and 2.4G don't expose the command interface.

**Bootloader didn't appear**: Run `dfu-util -l`. If `2e3c:df11` shows up after a delay, rerun with `--skip-hid`.

## Acknowledgements

- [MHooijberg/Zoom75-Wireless-Firmware](https://github.com/MHooijberg/Zoom75-Wireless-Firmware) for identifying the AT32F415 MCU.
- [dfu-util](https://dfu-util.sourceforge.net/) and [hidapi](https://github.com/libusb/hidapi), which do the heavy lifting.

## Disclaimer

Not affiliated with or endorsed by Meletrix or Wuque Studio. This repo contains no Meletrix code or firmware; download firmware from Meletrix directly.

**Flashing firmware is entirely at your own risk.** This is an unofficial tool built by reverse-engineering the Windows updater. The author is not responsible for any damage to your keyboard or any other hardware, including bricked devices, data loss, or voided warranties, resulting from the use of this software. The software is provided "as is", without warranty of any kind, as stated in the [MIT License](LICENSE).

## License

[MIT](LICENSE)
