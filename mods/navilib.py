#!/usr/bin/env python3
"""navilib: Navidrome-style media library for the SNOWSKY ECHO NANO (V2.0.0).

  python3 mods/navilib.py NANOV200.IMG NANOV201.IMG

What it changes (firmware V2.0.0 only, every patch site is verified first):

1. Albums are identified by ALBUM + ALBUMARTIST (falls back to ARTIST), like
   Navidrome, instead of by album name alone. Two different "Remixes" or
   "Vol.1" by different artists no longer merge into one album with
   interleaved tracks. The library scanner (overlay seg 39) now stores a
   composite key "<album>\\x01<album artist>" in an unused field of each
   track record (+0x100), and the index builder (overlay seg 40) groups the
   Album / Artist / Genre views on that key. The UI still displays the plain
   album name (it reads +0x600), so nothing looks different except that
   albums stop bleeding into each other.

2. ALBUM ARTIST / ALBUM_ARTIST (foobar2000, Mp3tag) are accepted as well as
   ALBUMARTIST in FLAC/Ogg tags, the same spellings Navidrome reads.

3. Track titles come from the TITLE tag (TIT2 / ©nam / Vorbis TITLE) instead
   of the file name. The stock scanner deliberately overwrote the parsed
   title with the file name. If a file has no title tag, it still falls back
   to the file name.

The new code lives in a block of dead code in the resident segment
(0x030517F0..0x0305199A: four debug framebuffer/glyph dump routines that
nothing calls).

The image is dated 2026-10-31 (see README.md, "Install").

After flashing, run Media library -> Update media library once (records made
by the stock firmware have no album key).
"""
import os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from rkfw import parse_header, parse_stage     # noqa: E402
import fwtool                                  # noqa: E402

CAVE = 0x030517F0          # dead code in segment 0 (unreferenced debug dumpers)
CAVE_END = 0x0305199A
MEMCPY = 0x030579BA

# ---------------------------------------------------------------- new code
# Assembled with keystone (thumb); source kept here for reference / rebuilds.
ASM_TITLE = """
    mov     r1, r0              ; src  = temp record +0x000 (file name)
    add.w   r0, r1, #0x100      ; dst  = temp record +0x100 (title)
    mov.w   r2, #0x100
    b.w     {memcpy:#x}
"""
ASM_KEY = """
    add.w   r0, r5, #0x100      ; dst  = final record +0x100 (album key)
    add.w   r1, r5, #0x600      ; album
    movs    r3, #0x7e           ; chars left (field is 0x80 chars incl. NUL)
album_loop:
    ldrh    r2, [r1], #2
    cbz     r2, separator
    strh    r2, [r0], #2
    subs    r3, #1
    bne     album_loop
    b       done
separator:
    movs    r2, #1              ; U+0001 sorts before every printable char
    strh    r2, [r0], #2
    subs    r3, #1
    beq     done
    add.w   r1, r5, #0x500      ; album artist (scanner already prefers ALBUMARTIST)
artist_loop:
    ldrh    r2, [r1], #2
    cbz     r2, done
    strh    r2, [r0], #2
    subs    r3, #1
    bne     artist_loop
done:
    movs    r2, #0
    strh    r2, [r0]
    b.w     {cut:#x}            ; then cut the stored artist to the primary one
"""

# +0x500 (album artist, else artist) is only used by the library browser (Artist list);
# Now Playing reads the file's own tags. Keep the first name of "A; B; C" there so the
# Artist list groups collaborations under the primary artist, like Navidrome.
ASM_CUT = """
    add.w   r0, r5, #0x500
    movs    r2, #0
cut_loop:
    ldrh.w  r1, [r0, r2, lsl #1]
    cbz     r1, cut_done
    cmp     r1, #0x3b           ; ';'
    beq     cut_here
    adds    r2, #1
    cmp     r2, #0x7f
    blo     cut_loop
    b       cut_done
cut_here:
    cbz     r2, cut_done        ; leading ';': leave as is
trim:
    subs    r3, r2, #1
    ldrh.w  r1, [r0, r3, lsl #1]
    cmp     r1, #0x20           ; drop the space before ';'
    bne     term
    mov     r2, r3
    cmp     r2, #0
    bne     trim
term:
    movs    r1, #0
    strh.w  r1, [r0, r2, lsl #1]
cut_done:
    movs    r0, #1              ; the serializer epilogue we replaced
    pop     {r4, r5, r6, pc}
"""

# Vorbis comments may repeat ARTIST / ALBUMARTIST (one entry per artist). Stock keeps
# the last one; keep the first (the primary artist) instead. r5 = field id (2 artist,
# 6 album artist) in the parser; the tag struct is cleared per file.
ASM_MV = """
    cmp     r5, #2
    beq     mv_chk
    cmp     r5, #6
    bne     mv_go
mv_chk:
    ldrh    ip, [r0]
    cmp.w   ip, #0
    bne     mv_skip
mv_go:
    b.w     0x0306d4a8          ; stock UTF-8 -> UTF-16 tag copy
mv_skip:
    bx      lr
"""


ASM_AA = """
    push    {r4, r5, lr}
    movs    r3, #0              ; i: index into the tag key (bytes, not NUL-terminated)
    movs    r4, #0              ; j: index into "ALBUMARTIST"
aa_loop:
    cmp     r3, r2
    bhs     aa_end
    ldrb    r5, [r0, r3]
    adds    r3, #1
    cmp     r5, #0x20           ; ignore ' ' and '_': ALBUM ARTIST, ALBUM_ARTIST
    beq     aa_loop
    cmp     r5, #0x5f
    beq     aa_loop
    sub.w   ip, r5, #0x61       ; upper-case a..z
    cmp.w   ip, #0x19
    it      ls
    subls   r5, #0x20
    ldrb    ip, [r1, r4]
    adds    r4, #1
    cmp     r5, ip
    beq     aa_loop
    b       aa_fail
aa_end:
    ldrb    ip, [r1, r4]
    cmp.w   ip, #0
    bne     aa_fail
    movs    r0, #0              ; 0 = match (same contract as the stock compare)
    pop     {r4, r5, pc}
aa_fail:
    movs    r0, #1
    pop     {r4, r5, pc}
"""


def asm(src, addr):
    try:
        from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
    except ImportError:
        return None
    enc, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(src.replace(';', '//'), addr)
    return bytes(enc)


# pre-assembled fallbacks (filled in by `--print-bytes` during development)
PREBUILT = {k: bytes.fromhex(v) for k, v in {
    'title': '014601f580704ff4807206f0deb8',
    'key': '05f5807005f5c0617e2331f8022b22b120f8022b013bf8d10de0012220f8022b013b08d005f5a06131f8022b1ab120f8022b013bf8d10022028000f031b8',
    'aa': '30b500230024934211d2c55c0133202df9d05f2df7d0a5f1610cbcf1190f98bf203d11f804c001346545ecd006e011f804c0bcf1000f01d1002030bd012030bd',
    'cut': '05f5a060002230f8121089b13b2903d001327f2af7d30be052b1531e30f81310202902d11a46002af7d1002120f81210012070bd',
    'mv': '022d01d0062d04d1b0f800c0bcf1000f01d11bf0dfbd7047',
    'site_title': 'b4f8000118b92046d5f7dbfa00bf',
    'site_key': 'd5f7f8bf',
    'site_aa': 'e2f7d8fb',
    'site_mv': 'e2f77efb',
}.items()}

TITLE_AT = CAVE
KEY_AT = CAVE + 0x10
AA_AT = CAVE + 0x60
CUT_AT = 0x030518A0          # dead code after the AA helper, ..0x030518F0 (turbo uses 0x030518F0..)
MV_AT = CUT_AT + 0x34
CUT_END = 0x030518F0


def build_patches():
    t = asm(ASM_TITLE.format(memcpy=MEMCPY), TITLE_AT) or PREBUILT['title']
    k = asm(ASM_KEY.replace('{cut:#x}', f'{CUT_AT:#x}'), KEY_AT) or PREBUILT['key']
    aa = asm(ASM_AA, AA_AT) or PREBUILT['aa']
    cut = asm(ASM_CUT, CUT_AT) or PREBUILT['cut']
    mv = asm(ASM_MV, MV_AT) or PREBUILT['mv']
    assert TITLE_AT + len(t) <= KEY_AT and KEY_AT + len(k) <= AA_AT and AA_AT + len(aa) <= CUT_AT
    assert CUT_AT + len(cut) <= MV_AT and MV_AT + len(mv) <= CUT_END, (len(cut), len(mv))
    # call sites (segment, address, expected original bytes, new bytes)
    site_title = asm(f"""
        ldrh.w  r0, [r4, #0x100]
        cbnz    r0, skip
        mov     r0, r4
        bl      {TITLE_AT:#x}
        nop
    skip:
    """, 0x0307C22E) or PREBUILT['site_title']
    site_key = asm(f"b.w {KEY_AT:#x}", 0x0307B80C) or PREBUILT['site_key']
    site_aa = asm(f"bl {AA_AT:#x}", 0x0306F09C) or PREBUILT['site_aa']
    site_mv = asm(f"bl {MV_AT:#x}", 0x0306F1D4) or PREBUILT['site_mv']
    key100 = bytes.fromhex('4ff48070')            # mov.w r0, #0x100
    return (t, k, aa, cut, mv), [
        # seg 39: scanner, "title = file name" memcpy -> only if the tag had no title
        (39, 0x0307C22E, bytes.fromhex('4ff48072214604f58070dbf7bffb'), site_title),
        # seg 39: record serialiser epilogue -> also write the album key
        (39, 0x0307B80C, bytes.fromhex('012070bd'), site_key),
        # seg 40: index builder, album level of Album / Artist / Genre views: +0x600 -> +0x100
        (40, 0x0307B32C, bytes.fromhex('4ff4c060'), key100),
        (40, 0x0307B35C, bytes.fromhex('4ff4c060'), key100),
        (40, 0x0307B390, bytes.fromhex('4ff4c060'), key100),
        # seg 0: Vorbis-comment parser, ALBUMARTIST key compare -> also accept
        # "ALBUM ARTIST" / "ALBUM_ARTIST" (foobar2000 / Mp3tag spellings)
        (0, 0x0306F09C, bytes.fromhex('e8f711fd'), site_aa),
        # seg 0: Vorbis-comment parser, tag value copy -> first ARTIST / ALBUMARTIST wins
        (0, 0x0306F1D4, bytes.fromhex('fef768f9'), site_mv),
    ]


def seg_offset(raw, seg, addr, n):
    hdr = parse_header(raw)
    o, s = hdr['stage'][3]
    base, segs = parse_stage(raw[o:o + s])
    sg = segs[seg]
    assert sg['code_pa'] <= addr and addr + n <= sg['code_pa'] + sg['code_sz'], (seg, hex(addr))
    return o + sg['code_va'] - base + addr - sg['code_pa']


def apply(raw):
    raw = bytearray(raw)
    code, sites = build_patches()
    # the code cave must still be the stock dead code
    cave_off = seg_offset(raw, 0, CAVE, CAVE_END - CAVE)
    if raw[cave_off:cave_off + 4] != bytes.fromhex('f0418046'):
        raise SystemExit("code cave is not stock (already patched, or not V2.0.0)")
    for seg, addr, old, new in sites:
        assert len(old) == len(new), (hex(addr), len(old), len(new))
        off = seg_offset(raw, seg, addr, len(old))
        if raw[off:off + len(old)] != old:
            raise SystemExit(f"seg{seg} {addr:#x}: unexpected bytes {raw[off:off + len(old)].hex()} "
                             f"(this patch is for ECHO NANO V2.0.0 only)")
        raw[off:off + len(new)] = new
    for at, blob in zip((TITLE_AT, KEY_AT, AA_AT, CUT_AT, MV_AT), code):
        o = cave_off + (at - CAVE)
        raw[o:o + len(blob)] = blob
    # newer date than stock: the loader boots the firmware copy with the newest date
    raw[0:4] = bytes.fromhex('26203110')          # 2026-10-31 (stock V2.0.0: 2026-10-29)
    size = parse_header(bytes(raw))['size']
    return fwtool.finalize(bytes(raw[:size]))


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == '--print-bytes':
        (t, k, aa, cut, mv), sites = build_patches()
        print(repr({'title': t.hex(), 'key': k.hex(), 'aa': aa.hex(), 'cut': cut.hex(), 'mv': mv.hex(),
                    'site_title': sites[0][3].hex(), 'site_key': sites[1][3].hex(),
                    'site_aa': sites[-2][3].hex(), 'site_mv': sites[-1][3].hex()}))
        raise SystemExit
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    out = apply(open(sys.argv[1], 'rb').read())
    open(sys.argv[2], 'wb').write(out)
    print(f"wrote {sys.argv[2]}  (navilib: album = ALBUM+ALBUMARTIST, titles from tags)")
