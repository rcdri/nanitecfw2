# navilib: Navidrome-style library for the ECHO NANO (V2.0.0)

Fixes the two problems you get when you copy a Navidrome library (`AlbumArtist/Album/[CD1/]track.flac`) onto the player:

| Stock firmware | navilib |
|---|---|
| Albums are grouped **by album name only**, so every "Remixes", "Vol.1" or "Best Of" from different artists merges into one album with interleaved tracks | Albums are grouped by **ALBUM + ALBUMARTIST** (or ARTIST if there's no album artist), the same as Navidrome |
| Track **titles are the file names** (`01 - foo.flac`), because the scanner overwrites the parsed TITLE tag | Titles come from the **TITLE tag**; the file name is used only if the tag is missing |
| Only `ALBUMARTIST` is read | `ALBUMARTIST`, `ALBUM ARTIST` and `ALBUM_ARTIST` are all read (foobar2000 / Mp3tag spellings) |
| `A; B` in ARTIST / ALBUMARTIST shows up as its own artist ("IX Fall$; BLCKK" next to "IX Fall$") | The **Artist list groups by the first name** before `;`, so collaborations sit under the primary artist like in Navidrome. Albums still group by the full credit, and Now Playing shows the full tag |
| Repeated tags (one `ARTIST=` per artist): the **last** one wins (a Pisca/ZhatjRTQA single was filed under ZhatjRTQA) | The **first** one wins, in the library and on Now Playing |

Multi-disc albums (CD1/CD2 folders, or a DISCNUMBER tag) stay one album, sorted disc then track. Compilations tagged `ALBUMARTIST=Various Artists` stay one album, not one per track artist. Japanese and other CJK tags are unchanged (UTF‑16 internally).

## Use

```sh
python3 mods/navilib.py NANOV200.IMG NANOV200_navilib.IMG
```

The script checks the original bytes at every patch site and refuses anything that isn't stock V2.0.0, then fixes up the CRC. No keystone needed: the assembled bytes are included in the script, and the asm source is kept alongside them.

Flash it like an official update (see the main README), renamed to `NANOV200.IMG`. The image is dated 2026‑10‑31 (see the main README on dates). Then **run Media library → Update media library once.** The old database has no album key, so until you rescan, everything shows up as one big album.

## Check a folder before copying it

`tools/navicheck.py` (needs `pip install mutagen`) prints the Album list the modded player will build from a folder, with warnings: albums that will split, missing TITLE/TRACKNUMBER, keys that get cut, the 8,192-track limit.

```sh
python3 tools/navicheck.py "/music/Camellia"                      # report
python3 tools/navicheck.py "/music/Camellia" --dump tags.json     # tags only, a few KB, for emulator tests
python3 tools/navicheck.py --rebuild tags.json lib/               # silent FLACs with the same names and tags
```

## Tagging tips (same as Navidrome)

- Set **ALBUMARTIST** on compilations. Without it, a VA album splits into one album per track artist. That's the same thing Navidrome does without its compilation flag; `COMPILATION=1` by itself is **not** read.
- Only `;` splits artists. `feat.`, `&`, `/` and `,` are left alone, because they also appear inside real names. When the album artist is set (as Navidrome-style libraries do), it decides the Artist list anyway.
- Stock firmware doesn't play **Opus**; the [opus](#opus-opus-playback) mod adds it. The other formats are FLAC/APE/WAV up to 24/192, DSD64–256, MP3, OGG, M4A and WMA.
- Two albums with the same name by different artists appear as **two entries with the same name** in the Album list, sorted by artist. The Artist view (which now lists album artists) is the easy way to tell them apart.
- The album key is limited to 126 characters (album + album artist). That's only a problem if two albums share the first ~120 characters of their names.

## What's patched

| Where | Change |
|---|---|
| seg 39 `0x0307C22E` (library scanner) | `memcpy(title, filename)` now runs only if the title is empty |
| seg 39 `0x0307B80C` (record serializer epilogue) | jumps to a helper that writes `album \x01 albumartist` into the unused record field `+0x100` |
| seg 40 `0x0307B32C/35C/390` (index builder) | Album/Artist/Genre views group by `+0x100` instead of `+0x600` (the UI still displays `+0x600`) |
| seg 0 `0x0306F09C` (Vorbis comment parser) | the ALBUMARTIST key compare goes to a helper that ignores `' '`/`'_'` |
| seg 0 `0x030517F0..0x0305199A` | the three helpers, placed over four unreferenced debug-dump routines |

Tested in the emulator against a test library: Japanese titles, a VA compilation, three different albums called "Remixes", a 2‑CD album in CD1/CD2, an untitled file, an over-long album name, and foobar‑style `ALBUM ARTIST` tags. Browsing, Play all and auto-advance work. The emulator barely decodes FLAC audio (on stock firmware too, since the lossless decoder accelerator isn't emulated), so playback itself is untested beyond the first moment. Make sure you have a RockUSB recovery path before flashing code mods.


---

# nanitec: fixes ported from [nanitecfw](https://github.com/maaraneasi/nanitecfw)

nanitecfw patches FiiO's **V1.7.0**. None of its addresses apply to V2.0.0, so every patch here was relocated by finding the same code in V2.0.0 and re-verified in the emulator.

```sh
python3 mods/navilib.py  NANOV200.IMG tmp.IMG          # optional, the two stack
python3 mods/nanitec.py  tmp.IMG NANOV200_mod.IMG      # [--power] [--no-shuffle] [--date YYYYMMDD]
python3 mods/nanitec.py  --recovery NANOV200.IMG NANOV200_recovery.IMG
```

| Fix | What it does | Emulator test |
|---|---|---|
| Skip unsupported tracks | No "File format not supported!" popup in Now Playing. The track is skipped right away, and playback stops after 10 failures in a row | stock: about 1.1 s popup, then next. Mod: next track starts after 0.09 s |
| Shuffle (main menu, 6th item) | One press: play mode set to shuffle, random song from All Songs. The folder-jump setting is kept | wraps both ways, icon drawn, starts a random song, Back lands on All Songs |
| **Hold-to-scroll acceleration** (new) | Holding Next/Prev in a library list (Songs, Albums, Artists, …) speeds up after about 1, 3 and 6 s. The browser takes every step itself, so the screen stays correct. While accelerating it stops at the first/last item instead of wrapping. Only active in media-library lists: Now Playing seeking, menus and volume are stock | 200‑song list: end reached in 7.5 s instead of about 21 s (about 36 items/s, limited by redraw); stops at 199 and 0; Select plays the right song |
| **Random Album** (main menu, 7th item, new) | Picks a random album and plays it **in order** from track 1. Play order is set to normal, and a shuffle play mode becomes repeat-all | album list → random album → track 1 plays (e.g. "Parousia", Disc1 Track1, 1/4). Disc icon, all 7 labels, wraps both ways |
| Screen-off key crash (stock bug) | After a library refresh, the first key press once the screen had timed out crashed and rebooted the player: keys go to the music service (mod33) with the screen off, but the refresh left the index builder (mod40) in its place. Keys now take that path only while mod33 is in memory | stock and old builds: crash at the first held key after refresh + timeout. Fixed: no crash; blind next/prev during playback still skip tracks; power wakes the screen |
| About version | Software version shows `2.0.0m` | ✓ |
| Date | Image dated 2026‑10‑31 (see the main README) | n/a |
| Power saving (`--power`, off by default) | Decode core 144 MHz (MP3/AAC/WMA/WAV), 240 (OGG), 288 (FLAC/ALAC) instead of 300–480. CPU sleeps (WFI) between events with the screen on. Wheel polled every 1 ms instead of 0.4 ms | boots, UI and playback run. Battery gain and hi‑res dropouts can't be measured in the emulator (nanitecfw hasn't measured them on hardware either) |

**Recovery:** `--recovery` writes FiiO's unmodified V2.0.0 code, dated 2026‑10‑31 like the mods. The updater installs whatever file it finds, and a 10‑31 image is known to install over another 10‑31 image, so you can switch between mods and recovery in either direction.

**Not ported (yet):** nanitecfw's C code that runs during the library refresh. That covers the roughly 5× faster refresh, *Recent Albums*, the track‑artist *Artists* list next to *Album Artists*, and `CFWLOG.TXT`. V2.0.0 rewrote the media‑browser module (mod23) and moved the refresh pipeline, so this needs about 35 firmware symbols re-derived rather than relocated. Its track‑artist field (+0x100) also collides with navilib's album key, which would have to move to +0x200.

The strip, route and key code live in an unreferenced FAT routine in mod00 (0x0305ee10–0x0305f024). That routine holds the literal pool of the live FAT directory-entry functions at 0x0305ef00–0x0305ef1c, and the code goes around it. The first port overwrote that pool, which made every file create/rename/delete crash, including the updater's final delete. The menu table (7 entries), the icon pointer table and both icons live in mod21's own dead "UNICODEXDIC" writer (0x0307a6e0–0x0307a784), since only the menu painter reads them. `tools/cavecheck.py` checks every overwritten byte for literal-pool, `adr`, branch, pointer and `movw`/`movt` references from live code. Dispatcher case 6 used to be a second, unused route to System Settings (window 0x0a).

Relocation map (V1.7.0 → V2.0.0): mod21 +0x11dc as a whole. mod00 message API +0x98 (`msg_post` 0x0306b0d8, test+clear 0x0306b0f4, `get_key` 0x030632da). The 0x0300dxxx globals moved −0x1118 (MusicTypeSelID 0x0300c36a, settings 0x0300c04c, ticks 0x0300c02c). mod23 browser: title id 0x0300e704, cursor 0x0300e968 (+0 count, +0xc index), key fetch 0x0307cad6. mod33 error handler 0x030840a4. Clock table 0x03077500 (V2.0.0 already lowered rows 5/6). Main loop WFI at 0x0306a204. Wheel timer setups at mod00 0x0306b364, mod01 0x0307b198, mod09 0x0307d684, mod33 0x03083d34.


---

# turbo: faster library refresh and boot

```sh
python3 mods/turbo.py IN.IMG OUT.IMG            # IN = stock, navilib and/or nanitec output
python3 mods/turbo.py IN.IMG OUT.IMG --rebuild  # recompile mods/turbo/cache.c (clang + ld.lld)
```

**Why the refresh is slow.** The stock refresh reads storage one 512‑byte sector per call. While scanning, the tag parsers ask for the *same* SD sector again for nearly every field: in a traced refresh, 1,713 of 1,924 SD reads repeated the previous one. The sort then re-reads the same database sectors on internal flash thousands of times. On the device, every one of those is a full SD/eMMC command round trip.

**The fix: a refresh-only sector cache** (`mods/turbo/cache.c`, 670 bytes of Thumb‑2):
- Single-sector reads on both the SD path (0x030616ea) and the internal-flash path (0x03061596) go through a 28‑entry LRU cache (14 KB) in RAM that no module uses (0x0309c000–0x0309fa00, checked across boot, refresh, sort and playback).
- Both write paths (0x030630c4, 0x03063114) invalidate overlapping entries before writing, so the cache can never return stale data.
- It's armed by the refresh driver (mod05), only caches while the "updating media library" window (0x0d) is on screen, and is switched off at the end. Normal use, playback and USB never go through it.
- Code sits in dead debug code in mod00: an unreferenced "All LRC Time Tags" dump at 0x030512bc, plus the free space after nanitec's helper.

| 24‑song test refresh | stock | turbo |
|---|---|---|
| SD card reads | 1,924 | **193** (−90%) |
| internal flash reads | 3,441 | **850** (−75%) |
| all device reads | 5,365 | **1,043** (−81%) |
| database index | — | byte-identical |

Larger libraries gain about the same on the SD side (tag reading is per file). The sort side gains less as the library outgrows 28 cached sectors.

**Boot:** the 36-frame boot-logo animation runs at 10 ms per frame instead of 25 ms. The main menu appears at 2.0 s instead of 2.28 s in the emulator.

---

# opus: Opus playback

```sh
python3 mods/opus.py IN.IMG OUT.IMG [--date YYYYMMDD]     # IN = stock or the other mods' output
python3 mods/opus.py IN.IMG OUT.IMG --rebuild             # recompile mods/opus/ (clang, ld.lld; libopus checkout, see mods/opus/build.sh)
```

`.opus` files show up in the folder browser, get scanned into the library with their tags, and play, seek and show their time like any other track. Works on FAT32 and exFAT cards.

**Decoder.** libopus's fixed-point CELT decoder (xiph/opus `fed7c79`), built for the Cortex‑M3 with its ARMv4 `SMULL` multiply macros, plus a small Ogg/Opus front end (`mods/opus/opus_codec.c`). It's appended to the Ogg Vorbis codec image (segment 77: 57 KB of code in the free part of its 128 KB window). The image's Ogg entry now checks the first page: `OpusHead` streams go to the new decoder, everything else goes to the stock Tremor code unchanged. Opus uses the same clock profile as Ogg Vorbis (the decode core runs at 448 MHz, 240 MHz with `nanitec --power`). It needs about 30 M instructions/s at 128 kbps, against 22 for a 48 kHz Vorbis file, so either clock leaves plenty of headroom.

**Main core.** Every part of the player (browser filter, library scanner, tag reader, codec choice) picks the file type from the 3-letter 8.3 extension. A `.opus` file's is `OPU`, both on FAT32 (stored on the card) and on exFAT (made up by the firmware from the long name). Two hooks make `OPU` read as `OGG` where those names come from: the FAT directory-entry reader and the exFAT short-name generator. The copy in RAM is changed, not the card. The Ogg tag reader also accepts the `OpusTags` header (same layout as Vorbis comments).

| | |
|---|---|
| Supported | CELT mode, which is what libopus/ffmpeg/opusenc produce for music at about 64 kbit/s stereo and up. All frame sizes (2.5–60 ms, 120 ms packets), mono and stereo, 6–510 kbit/s, header output gain. Seeking is sample-exact, with the 80 ms pre-roll from the spec |
| Not supported | SILK and hybrid packets (speech, low bitrates). A file that is mostly SILK/hybrid is refused at open, so `nanitec`'s skip moves on; a stray one inside a CELT file plays as silence. Multichannel (mapping family 1). Tags stored after a large embedded cover in the same header (only the part of the tags in the first Ogg page is read; this is a stock limitation for Ogg Vorbis too) |
| Unchanged | `.ogg` Vorbis decoding: the captured output is bit-identical with and without the mod |

`build.sh` stacks navilib + nanitec + turbo + opus.

Files made by ffmpeg/libopus at 96–160 kbit/s (what Navidrome-style libraries usually hold: tags first, no embedded picture) are the easy case; two real-world ones decode at 73 dB SNR against libopus.

**Tests.** `mods/opus/bench.py` runs the built decoder alone in Unicorn, the way the codec core calls it, and compares the output with an ffmpeg decode:

```sh
python3 mods/opus/bench.py song.opus ref.raw [--seek MS] [--prof]
```

| Test | Result |
|---|---|
| 20/10/5/2.5/40/60/120 ms frames, 32–510 kbit/s, CBR, mono, 460 KB cover tag spanning 110 pages, seeks | 71–73 dB SNR against libopus's float decoder (max error 7 LSB, 26 at 64 kbit/s) |
| Hybrid/SILK files | refused at open |
| Emulator, FAT32 and exFAT cards | `.opus` listed; Now Playing shows TITLE/ARTIST; library scan fills All Songs, Artists (`ALBUMARTIST`), Albums. Playback from the browser and from an album matches the reference block for block. Seek lands on the requested sample |

**Tag padding.** Taggers (mutagen/Picard, opusenc) pad OpusTags with about 1 KB of zeros after the last comment. The stock Vorbis-comment parser (0x0306efae) has no comment count and reads each comment's key one byte per file read. It took the zeros for zero-length comments, seeked back and re-read the same bytes until its 512-unknown-keys limit: about 130 KB of one-byte reads per Opus file. 25 small Opus files cost 3.3 million SD reads and 80 s of emulated time. A zero-length comment is invalid in Vorbis too, so the per-comment length check (0x0306f008) now also stops on zero. The same 25 files now take 91 reads, and every title, album and album artist still reaches the database.

**Music support.** Settings → System settings → Music support lists the formats from a 10-slot table filled by 0x030534BA. A helper replaces the store of entry 4's flag (0x030534E6), adds entry 5 "OPUS:16bit/48kHz" (the decoder outputs 16-bit PCM at 48 kHz) and raises the count to 6 (0x030534F6).

**Patched.** Segment 77: decoder appended after the stock code, Ogg codec slot `0x0102D16C` → `ogg_entry`. Segment 0:
- `0x0305B2AE` (ReadDirEntry), `0x0305FBFE` (MakeShortName) and `0x0306F2CC` (Ogg tag reader): hooks;
- `0x0306F008`: comment-length check;
- `0x030534E6` and `0x030534F6`: Music support.

The helpers sit in the unused tails of the dead-code caves that turbo and nanitec use (`0x030514C4`, `0x03051510`, `0x0305F05C`). Notes on the codec interface, memory map and the core A audio path are in `mods/opus/NOTES.md`.

**Emulator fix found on the way.** The mailbox model used to pause core A every time it posted a message. Core A's audio path posts "decode the next block" and only then copies the block it already has. With core A paused, the codec core could finish a fast block first and swap the pointer, so blocks were dropped and repeated, and stock Vorbis crashed. Now only the codec core is paused when it posts (`emu/periph.py`). Vorbis and Opus captures are clean.

---

# Ideas backlog (not built yet)

| Idea | Why | Notes from the investigation |
|---|---|---|
| Recent Albums / track-artist Artists | nanitecfw features | Needs folder dates during the walk and an extra sort pass. Move navilib's key to +0x200 so the track artist can use +0x100 |
| **Faster sort** (next big one) | On a 200‑song refresh the sort makes 10,775 of 12,388 reads, and it grows faster than the library | The stock sort keys each song by its first 4 characters. On a tie (every track of an album, "The …", "Vol. …") the comparator re-reads both full records from flash: up to 111 reads per record for 200 songs. Fix: port nanitecfw's compact key store, written during the walk via navilib's serializer hook, plus a hash comparator into V2.0.0's mod40 |
| Library size limit | Big Navidrome libraries | **Checked: V2.0.0 stops scanning at 8,192 songs** (mod39 0x0307c34e, `cmp sl, #0x2000`). Files past that, in FAT directory order, are silently skipped. Raising it needs larger u16 tables in mod40/mod23 and RAM that isn't free. Keep the card under 8,192 tracks |
