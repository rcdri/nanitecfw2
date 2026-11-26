# V2.0.0 firmware notes

What was learned while building the mods, for whoever patches `NANOV200.IMG` next. Addresses are
runtime addresses. Several overlays share an address range, so a segment ("mod") number goes with
every overlay address. Patch tables for each mod are in [`mods/README.md`](../mods/README.md).
Tools: `tools/fwre.py` (segments, disassembly, references), `fwtool.py unpack` (ELF for Ghidra).

## Image container (RKnanoFW)

- 0x200-byte header at the start and a copy right after the body. Date at offset 0, BCD
  (`yy yy dd mm`, e.g. `26 20 29 10` = 2026‑10‑29). Blob slots: stage ×4, font ×10, gbk ×5,
  strtbl ×10, imageres ×10, unk ×10, then pad/size and the `RKnanoFW` tag.
- The image is padded to a 1 MiB multiple and ends with an RKCRC32. `fwtool.finalize` redoes the
  size, header copy, padding and CRC.
- Dates: the boot scan and the updater never compare dates (see below). On a real player:
  - 2026‑10‑30 installed over stock (2026‑10‑29);
  - 2026‑10‑31 installed over 10‑30, and over 10‑31 again;
  - one image dated 2026‑11‑01 was ignored.

  The boot loader lives in the boot area, not in the image, so its rule is unknown. The build dates
  every image 2026‑10‑31.
- Stage 3 is the code: `{base 0x03050000, count 91}`, then one 32-byte entry per segment
  `{code_va, code_pa, code_sz, data_va, data_pa, data_sz, bss_pa, bss_sz}`. 62 entries are used.
  - Segment 0 is resident (code 0x03050000+0x278f0, data 0x03004000).
  - 1 and 4 are loaded by the bootloader.
  - The rest are overlays paged in at fixed addresses (many share 0x0307a5f4).
  - 73–82 are the codec-core images.
- Growing a segment (Opus does it to 77) means shifting the `*_va` of everything after it, the stage
  size and the offsets of every blob after stage 3 (`mods/opus.py:grow_segment_code`).

## Main core (system core, Cortex-M3, 96–225 MHz depending on the clock profile)

| What | Where |
|---|---|
| Settings struct / tick counter | 0x0300c04c / 0x0300c02c |
| Current window id | 0x03004a74 |
| Main-menu index (mod21) | 0x0300e600 |
| Browser title id / cursor (mod23: +0 count, +0xc index) | 0x0300e704 / 0x0300e968 |
| Message API: post / test+clear / get key | 0x0306b0d8 / 0x0306b0f4 / 0x030632da |
| Main loop WFI | 0x0306a204 |
| Key fetch in library lists (mod23) | 0x0307cad6 |
| Music-service error handler (mod33) | 0x030840a4 |
| Library scanner (mod39), song limit `cmp sl, #0x2000` = 8,192 | 0x0307c34e |
| Index builder (mod40), browser (mod23), main menu (mod21) | |
| Free RAM (turbo's cache uses 0x0309c000–0x0309fa00) | 0x0309b870–0x0309ffef |

**Library database record:** +0x100 navilib's album key (`album \x01 albumartist`), +0x500 the
album artist (else artist) used by the Artist list, +0x600 the album name the UI shows. Now Playing
reads tags from the file itself.

**Clocks:** the per-module table is at 0x03077500. It has 36 rows of 7 words: an id, then frequencies
that are summed over the active modules, then clamped by the limits at +0x3e0. Fields 5–6 are the
HiFi (codec) core. The active-module bitmask is at 0x0300cab0. Enter/exit module is
0x0306af7c / 0x0306afc8, and the recompute is 0x0306ad98. While an Ogg/Opus file plays, modules 3, 4
and 14 are active, which gives PLL 1344 MHz, core A 192 MHz and the HiFi core 448 MHz.
`nanitec --power` lowers the codec rows.

**File types:** every consumer compares the 3-letter 8.3 extension against
`MP1MP2MP3WMAWAVAPEFLAAACM4AOGGMP43GPDFFDSF…`, takes the 1-based index, then a `tbb`. The lists
live in seg0 data at 0x03004906, 0x03004938, 0x0300496a and 0x03004995, in mod39 at 0x0307baa4
(+ CUE), and in mod31 at 0x0307d3ac.

| Routine | Address |
|---|---|
| Tag dispatcher | 0x030711f0 |
| Ogg page walker (tags) | 0x0306f20a |
| Vorbis comment parser | 0x0306efae |
| FAT ReadDirEntry (copies the 32-byte entry; volume type byte 0x03077d00, 3 = exFAT) | 0x0305b250 |
| MakeShortName (exFAT and new LFN files get a synthesized `NAME~NNN.EXT`) | 0x0305faf4 |

## Code caves

A function that is never called is not automatically free space: the compiler shares **literal
pools** between neighbouring functions, so a dead routine can contain constants that live code
loads with `ldr rX, [pc, #imm]`. The first nanitec build overwrote such a pool inside the dead FAT
routine at 0x0305ee0c. That broke the shared directory-entry writer 0x0305ec00 (and 0x0305eb86 /
0x0305ecb2), so every file create/rename/delete crashed: an update could not delete its
`NANOV200.IMG` afterwards and the player re-flashed on every boot.

`tools/cavecheck.py STOCK.IMG MOD.IMG` now gates every build. For each byte the mods change it
decodes all segments at every halfword and reports:
- PC-relative loads and `adr` whose target is in a changed range;
- branches into the middle of one;
- pointers in any code or data;
- `movw`/`movt` pairs.

Each hit is then checked against a linear disassembly of its function. Its function is
classified as live or dead (reachable from a pointer, another segment or a live caller, in the
context where the patched overlay is loaded), and the build fails on any live reference. As a
dynamic cross-check, stock firmware run in the emulator (boot, menus, a library refresh, an update)
never reads, writes or executes the cave ranges or turbo's RAM. The only access is the overlay
loader copying mod21.

| Range | Was | Now holds |
|---|---|---|
| seg0 0x030512bc–0x03051550 | unreferenced "All LRC Time Tags" dump + its strings | turbo cache (..0x030514c2), opus FS hooks (0x030514c4–0x03051510), Music support helper + "OPUS:16bit/48kHz" (0x03051510–0x03051542) |
| seg0 0x030517f0–0x0305199a | four unreferenced framebuffer/glyph dumpers | navilib helpers (..0x030518f0), turbo `.hi` (0x030518f0–0x03051988) |
| seg0 0x0305ee10–0x0305ef00 | first half of the unreferenced FAT routine | nanitec strip (0x0305ee10) and route (0x0305ee4c–0x0305eee4) |
| seg0 **0x0305ef00–0x0305ef1c** | **literal pool of the live FAT functions** | **untouched** (checked as stock bytes before patching) |
| seg0 0x0305ef1c–0x0305f0c4 | rest of the FAT routine | nanitec key handler (..0x0305f024), opus tag hook (0x0305f05c–0x0305f0b8) |
| mod21 0x0307a6e0–0x0307a816 | "UNICODEXDIC" writer | nanitec icon table, icons, menu table (..0x0307a784; only read while mod21 is loaded) |
| RAM 0x0309c000–0x0309fa00 | unused | turbo sector cache (state ..0x0309c164); nanitec counters at 0x0309c1f0 |

**Overlay trap (stock bug):** with the screen off (flag 0x0300c6bc = 1), key pre-processing at
0x03063c14 hands keys straight to the music service in mod33 (0x03081d92 and others), so next/prev
work blind. A library refresh pages mod40 (the index builder, 0x0307a9d0–0x0309b870) over mod33's
range and leaves it there. Once the screen times out after the refresh, the next key press calls
into mod40's zeros, slides through RAM and faults: crash and reboot. nanitec now takes that path
only when mod33's word at 0x03081d90 (0xf0010c7c) is present.

**Not free, despite looking dead:** 0x0306bae8 is called from seg34, and 0x03066162 is the reset
handler (vector at 0x03050004).

Free space left: 14 bytes at 0x03051542, 18 at 0x03051988, 28 at 0x0305eee4, 56 at 0x0305f024,
12 at 0x0305f0b8, and 146 in mod21 (0x0307a784).

## Internal storage, updates and recovery

The internal eMMC as the firmware (and RockUSB Loader mode) addresses it, in 512-byte sectors:

| LBA | What |
|---|---|
| 0 | system area: firmware copy 1 (image body + header copy at `size`) |
| sys/2 | firmware copy 2. At boot the firmware reads the system-area size from the ID block in the boot area: `[0x0300d108]` = sys_mb << 11, `[0x0300d10c]` = half of that |
| sys | "sub 1": 200 MB of raw data (settings `SAVE` block, media database) |
| sys + 409600 | user area, what USB mass storage shows: MBR, FAT32 partition at +0x40 |

On a real ECHO NANO (480 MB of flash) the system area is 26 MB: copy 2 at LBA 26624, user
partition at LBA 462912. The boot area (ID block, loader) sits before LBA 0 and is not reachable
through these LBAs.

**Update** (boot scan in mod1 at 0x0307b4a0, updater in mod8 at 0x0307b7c8):
1. At boot the scan looks for `NANOV001.IMG` or `NANOV100`–`NANOV399.IMG` in the root of
   internal storage. It checks only the `RKnanoFW` tag and the header copy at `size`.
2. The updater writes the file to copy 2 in 8 KB chunks. It writes the first chunk (the header)
   last, so a half-written copy never looks valid.
3. It copies copy 2 over copy 1 (0x0307b9ec).
4. It deletes the file, even when the update failed, and resets.

If the delete fails, the next boot flashes again: that was the loop described above.

**Recovery:** holding *forward + back* while connecting USB starts the boot loader's RockUSB
**Loader** mode (2207:262d; the screen stays off). `tools/echofix.py` (Python + root, e.g. Termux
on a phone) uses it to:
- read both copies;
- find the user FAT;
- rename a stuck `NANOV*.IMG` so the player boots again.

The same LBA layout lets `rkdeveloptool rl/wl` restore copy 1 from a stock image.

## Codec core (HiFi core) and the audio path

See [`mods/opus/NOTES.md`](../mods/opus/NOTES.md). It covers:
- the CodecFunction interface
- the shared output buffers
- the RKFIO file calls
- free RAM in image 77
- how core A requests and copies blocks (it posts "decode next" before copying the current block)
- decoder cost measurements
