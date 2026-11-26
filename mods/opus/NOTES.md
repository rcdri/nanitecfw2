# Opus on the ECHO NANO (V2.0.0): working notes

## Codec core ("HiFi" core, Cortex-M3)
- Images = segments 73..82, each a full program: code at 0x0 (alias 0x01000000, must stay below
  0x01020000), data at 0x01020000. 73 MP3, 74 WMA, 75 AAC/M4A, 76 WAV, 77 Ogg Vorbis (Tremor),
  78 FLAC, 79 ALAC, 80 APE, 81 DFF, 82 DSF.
- Shared runtime: file I/O over the mailbox (RKFIO table 0x010220e4: [1] Read(buf,n,h) [2] Seek(off,whence,h)
  [3] Tell(h) [5] Length(h), handle at 0x01022108), command loop on [0x0102220c], codec type in [0x01022214].
- Codec entry table 0x0102d15c (10 words); image 77 fills only [4] = 0x29d1, so Ogg = codec type 4.
- CodecFunction(subfn, p1, p2, p3): 3 bitrate (*p1), 4 sample rate, 5 channels, 6 length ms, 7 time ms,
  8 open (ret 1), 10 capture buffer (*p1 = bufs[idx] + off*2, *p2 = 0x200 frames, then idx ^= 1),
  12 decode (512 stereo s16 frames into bufs[idx] + off*2; ret 1, 0 = end), 14 seek (p1 ms), 15 close, 17 bps = 16.
  Out-buffer state 0x01029888 (+4 idx, +8 fill), buffers 0x01029898, offset [0x01022104] (int16 units).
- Runtime order per block: decode (12) -> time (7) -> capture buffer (10) -> mailbox msg 5 to core A.
- Vorbis: vf at 0x010434e0, ov_open 0x100d578, ov_read 0x100dfcc.
- RAM: image 77 never touches 0x0105F000..0x0106FFFF; 0x01070000.. is the shared runtime/IPC area.
  Opus uses ~59 KB of it (state, 8 KB packet buffer, 4 KB read buffer, one decoded frame,
  CELT decoder 18 KB, 16 KB pseudo-stack).

## Core A's side of the audio path
- Core A's get-buffer (A-side codec dispatcher 0x030862f0, case 10): if the codec has signalled a
  block, it posts "decode next" (A2B ch1 cmd 4) **first**, then M2M-copies the block it was given
  (pointer saved from msg 5) into its own I2S ping-pong buffers 0x03023074 / 0x0302c074.
  On hardware the copy starts a few instructions after the post, long before core B can finish
  another block. The emulator used to stop core A on every mailbox write, which let core B finish
  a fast decode (no packet to decode, no file I/O) and replace the pointer first: dropped and
  duplicated 512-frame blocks, and Tremor's buffer pool corrupted (crash at 0x74ae). Fixed in
  emu/periph.py: only the codec core is stopped when it sends.
- Clocks: per-module table at 0x03077500 (36 rows of 7 words: id, then summed frequencies, field 5/6 =
  HiFi core). Active modules while an Ogg file plays: 3, 4, 14 -> HiFi 448 MHz (PLL 1344 MHz / 3),
  core A 192 MHz. nanitec --power sets row 14 to 192 MHz (HiFi 240 MHz total).

## Decoder cost (Unicorn instruction counts, bench.py)
| build | 128 kbps stereo |
|---|---|
| -Os, byte-wise mem*, generic C multiplies | 38.3 M instr/s |
| + word mem*, -O2 for kiss_fft/mdct/celt | 36.0 |
| + OPUS_ARM_INLINE_ASM (SMULL macros) | 29.9 |
Stock Tremor, 48 kHz q6 Vorbis: 21.6 M instr/s. 510 kbps Opus: 43 M instr/s. Worst single call
(one 20 ms frame): ~0.7 M instructions = ~2 ms at 448 MHz.

## Main core: how a file's type is chosen
- Every consumer (folder browser lists 0x03004906/38/6a/95, scanner list in mod39 0x0307baa4, tag
  dispatcher 0x030711f0, codec select) compares the 3-character 8.3 extension against
  "MP1MP2MP3WMAWAVAPEFLAAACM4AOGGMP43GPDFFDSF..." with 1-based index -> tbb.
- FAT32: the 8.3 name comes from the directory entry, read by ReadDirEntry 0x0305b250
  (copies 32 bytes from the sector cache; volume type at [0x03077d00], 3 = exFAT).
- exFAT: MakeShortName 0x0305faf4 builds "NAME~NNNEXT" from the long name (first 3 chars after the
  last dot, upper-cased), so "x.opus" -> "OPU" on both.
- Hooks: ReadDirEntry's unlock call (0x0305b2ae) and MakeShortName's epilogue (0x0305fbfe) turn
  "OPU" into "OGG" in the returned copy. Nothing writes those copies back for audio files.
- Ogg tag reader 0x0306f20a walks pages; for a page whose first packet starts with 3 "vorbis" it calls
  the Vorbis comment parser 0x0306efae with the packet's bytes in that page - 7. The hook at
  0x0306f2cc also accepts 'O' "pusTag" 's' and passes - 8. Like Vorbis, only the part of the tags
  packet inside its first page is read, so tags stored after a big embedded picture are missed
  (ffmpeg-made files have no picture and ~300-byte tags).

## Tests
- bench.py: CELT 20/10/5/2.5/40/60/120 ms frames, 32..510 kbps, CBR, mono, cover-art tags spanning
  110 pages, seeks: 71-73 dB SNR vs ffmpeg/libopus float (fixed-point output, max diff <= 26 LSB);
  hybrid/SILK files refused at open.
- Emulator: FAT32 and exFAT cards; .opus listed, Now Playing tags, library scan (All songs / Artists /
  Albums), playback from browser and library matches the reference block for block (73 dB), seek
  lands on the requested sample, stock Vorbis output bit-identical with and without the mod.
- Real-world files (a Navidrome library: ffmpeg/libopus 96-160 kbps CELT, tags first, no picture):
  the first 256 KB of two decode at 73 dB.
