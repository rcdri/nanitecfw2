# Emulator and image tools

`echonano.py` emulates the FiiO / SNOWSKY **ECHO NANO** (`NANOxxx.IMG`, Rockchip RKNanoD, two
Cortex‑M3 cores). You can boot a stock or modded IMG on your PC, use its UI from the keyboard,
browse and play music, and record what it shows and plays. `fwtool.py` unpacks, edits and repacks
the image. Every mod in this repo was developed and tested this way before flashing.

It was built against **V2.0.0** (`NANOV200.IMG`, 2026‑10‑29). Commands below run from the repo root.

![demo](demo.gif)

## What works

| Area | Status |
|---|---|
| Boot (logo animation, first‑boot factory init, main menu) | ✅ |
| 128×32 SSD1306 OLED over bit‑banged I²C | ✅ decoded from the real command stream |
| Keys (ADC ladder + GPIO), screen off/on, long‑press power‑off | ✅ |
| Internal storage (eMMC) with the firmware's own partitioning and FAT32 | ✅ |
| microSD slot (card detect, hot insert/eject) | ✅ |
| File browser, media library prompts, settings | ✅ |
| **Playback**: second "HiFi" core runs the real codec image, mailbox IPC, DMA → I²S | ✅ WAV, Ogg Vorbis and Opus match a reference decode block for block; you can record the output |
| Firmware debug UART (`--uart`) | ✅ |
| USB mass storage / USB DAC mode | ❌ (not emulated) |
| Battery is a fixed ~4.0 V and the charger is never plugged in | ⚠️ |
| DAC volume (CS43131 over I²C): the recorded audio is pre‑volume | ⚠️ |

## Install

```bash
pip install unicorn pillow pyfatfs          # required
pip install capstone keystone-engine        # optional: tools/fwre.py and assembler patches
```
The window uses Tk, which ships with python.org builds. On Debian/Ubuntu install it with `apt install python3-tk`.

## Run it

```bash
# window (creates NANOV200.nand.img on first run, lets the firmware format it, copies your music in)
python3 echonano.py run NANOV200.IMG --music ~/Music/some-folder

# with a microSD card image (created if missing)
python3 echonano.py run NANOV200.IMG --sd sd.img --sd-music ~/Music/other

# headless + scripted (CI, screenshots, GIFs, audio capture)
python3 echonano.py script NANOV200.IMG examples/demo.txt --audio-out play.wav
```

| Key (window) | Device |
|---|---|
| ↑ / ← | prev (scroll up/left) |
| ↓ / → | next |
| Enter | ok / select |
| Backspace / Esc | back |
| M / Tab | menu (options/settings) |
| Space / P | power: tap = screen off/on, hold ≈2 s = power off |
| F2 / F3 | screenshot / start‑stop GIF |
| F5 | insert / eject the microSD |

Script commands: `wait S`, `press KEY [S]`, `shot FILE.png`, `gif FILE.gif` / `gif stop`,
`sd in|out`, `dump ADDR LEN`. Times are in emulated seconds, so runs are deterministic.

Other options: `--speed 2` runs the window at 2× real time. `--cpi N` sets how many emulated cycles
each instruction counts as; raise it if the UI feels slow. `--no-coreb` skips the codec core.

## Modding workflow

```bash
python3 fwtool.py unpack NANOV200.IMG work/
#   work/images/NNN_NAME.png     240 bitmaps (128x32 etc.), RGB565 source. BLACK = lit pixel.
#   work/strings/langXX.tsv      21 languages x 431 strings (lang00 = zh-CN, lang02 = English)
#   work/code/                   per-segment .bin, segments.tsv, firmware.elf / boot_view.elf
#   work/blobs/                  raw fonts (12/16 px), charset tables, ...
# ...edit PNGs / TSVs / segment bins...
python3 fwtool.py pack work/ NANOV201.IMG       # rewrites header copy, padding and RKCRC32
python3 echonano.py run NANOV201.IMG             # test it (the eMMC image gets the new fw)
```

An unedited `pack` reproduces the official file byte for byte, so `cmp` is a good sanity check.

**Images**: keep the dimensions. `--allow-resize` works because imageres is the last blob, but test
first. The boot animation is `A_POWERON00..35` plus `000_A_POWERON0`.
**Strings**: at most 99 characters, escapes `\n \t`. Unchanged lines are skipped.
**Code**: segment binaries must keep their size. For quick binary patches by runtime address:

```bash
# SEG:ADDR=hex  or  SEG:ADDR="thumb asm" (needs keystone)
python3 fwtool.py patch NANOV200.IMG INVERT.IMG 4:0x0307a410=a720   # OLED init: A6 -> A7 (inverted display)
```

Segment 0 is resident, 1 and 4 are loaded by the bootloader, and the rest are overlays the firmware
pages in at fixed addresses. Several overlays share an address, so always give the segment. Segments
73–82 are the codec‑core images (one per format), which execute at 0x0 on that core.
`tools/fwre.py IMG dis|refs|str|segs` disassembles and finds references.

For Ghidra, load `code/firmware.elf` as ARM Cortex‑M little‑endian (overlapping overlays become
overlay blocks). `boot_view.elf` holds just the resident segment and the init segments, with no
conflicts.

Flashing, dates and recovery are covered in the [main README](../README.md#install).

## How the emulation works

The main core is Unicorn in Cortex‑M mode. NVIC, SysTick and exception entry/exit are written in
Python because Unicorn has none. Time is counted in instructions × `cpi`, and a WFI halt fast‑forwards
to the next event.

| Address | Block | Model |
|---|---|---|
| 0x0300_0000 | system RAM (1 MB) | shared with core B |
| 0x0100_0000 | HiFi‑core RAM (code at 0x0 for that core) | shared |
| 0x400C_0000 | DW APB UART | debug console |
| 0x400D_0000 | SAR‑ADC, IRQ 20 | ch0 battery, ch1 key ladder |
| 0x400E_0000 | RK timer ×2, IRQ 19 | 24 MHz |
| 0x4011_0000 | mailbox A↔B | A2B → core‑B IRQ 9+n, B2A → core‑A IRQ 9+n. A message from core B pauses core B so core A answers promptly. Core A keeps running after it posts, as on hardware: its audio path posts "decode next" and then copies the block it already has |
| 0x4016/0x4017/0x5003_0000 | DW GPIO 0/1/2 | GPIO0.24/25 = OLED I²C, GPIO0.11 = SD detect (low), GPIO2.5 = power key (high), GPIO2.1 = ok (low) |
| 0x4018_0000 | CRU | PLL lock, soft reset 0x32 = HiFi core |
| 0x4003_0000 | I²S, TX FIFO +0x24 | audio sink (WAV capture) |
| 0x6000_0000 | DW AHB DMAC, IRQ 6 | mem↔mem instantly, →I²S paced at 44.1 kHz |
| 0x6002/0x6003_0000 | DW MMC (SD / eMMC) | stub; card I/O is HLE |

Several firmware routines are replaced with Python: card identify (it installs a fake card whose
read/write hit `nand.img`), the OLED byte writer, and the busy‑wait delays. Their addresses are found
by byte signature (`emu/fwprofile.py`), so other firmware versions have a chance of working.

The **virtual eMMC** layout is:
- ID block at 0x40 (system-area size in MB at sector 0x41, offset 2);
- system area at 0x2000, with firmware copy 1 at its start and copy 2 at half its size, where the
  firmware itself looks;
- a 200 MB "sub 1" area (settings / DB);
- the user FAT32 area. The firmware formats it itself on first boot.

See [FIRMWARE_NOTES](FIRMWARE_NOTES.md#internal-storage-updates-and-recovery) for the real
player's numbers.

## Known issues / ideas

- The audio sample rate is assumed to be 44.1 kHz. The real MCLK isn't modelled yet, so 48 kHz
  files play about 9% slow.
- Emulation speed is roughly 50–100 % of real time on a fast machine.
- USB (mass storage, UAC, RockUSB) isn't emulated. Neither are charging or battery drain.
- The watchdog isn't emulated: code that would stall the real player until it resets runs on here.
- Emulated time runs at about 5 cycles per instruction, so CPU-heavy work (tag parsing) is slower
  in emulated seconds than on the player, while card I/O costs nothing.
