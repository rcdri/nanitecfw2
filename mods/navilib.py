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

4. .cue sheets are ignored, as Navidrome does (Settings > Music Settings >
   CUE sheets > Stock brings the stock behaviour back). Stock turns every TRACK of a
   .cue into a song that points at the sheet's first FILE, so an album of
   separate files with a leftover .cue got N copies of track 1 (and lost the
   real track 1). With no PERFORMER line those copies had sort keys starting
   with a NUL, which the song-level sort silently dropped; the player lists an
   album's songs by counting entries, so every album after it opened the
   wrong songs (stock firmware too). The song-level sort now keeps such keys.

5. "A; B" is cut to "A" for the Artist list and the album key, so an album
   whose tracks credit different guests ("IX Fall$; Freddy") stays one album.
   The cut also clears the rest of the first 4 characters, so short names
   like "xi; Nora2r" don't show up as a second "xi".

6. Settings: Music Settings > CUE sheets (Off / Stock) and System Settings >
   Power saving (Off / On: what nanitec --power used to build in, switchable).

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
    movs    r0, #1              ; the serializer epilogue we replaced
    pop     {r4, r5, r6, pc}
"""

# +0x500 (album artist, else artist) is only used by the library browser (Artist list);
# Now Playing reads the file's own tags. Keep the first name of "A; B; C" there so the
# Artist list groups collaborations under the primary artist, like Navidrome. The record
# serializer's epilogue jumps here first; the album key is built afterwards from the cut
# name, so "Album" by "A" and by "A; B" (featured artists, no ALBUMARTIST) stay one album.
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
    movs    r2, r3
    bne     trim
term:
    movs    r1, #0
nul_fill:                       ; NULs up to the 4th char: the index compares 4 chars raw
    strh.w  r1, [r0, r2, lsl #1]
    adds    r2, #1
    cmp     r2, #4
    blo     nul_fill
cut_done:
    b.w     {key:#x}            ; then build the album key from the primary artist
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
    'key': '05f5807005f5c0617e2331f8022b22b120f8022b013bf8d10de0012220f8022b013b08d005f5a06131f8022b1ab120f8022b013bf8d100220280012070bd',
    'aa': '30b500230024934211d2c55c0133202df9d05f2df7d0a5f1610cbcf1190f98bf203d11f804c001346545ecd006e011f804c0bcf1000f01d1002030bd012030bd',
    'cut': '05f5a060002230f8121099b13b2903d001327f2af7d30de062b1531e30f81310202901d11a00f8d1002120f812100132042afad3fff794bf',
    'mv': '022d01d0062d04d1b0f800c0bcf1000f01d11bf0ddbd7047',
    'site_title': 'b4f8000118b92046d5f7dbfa00bf',
    'site_key': 'd6f748b8',
    'site_aa': 'e2f7d8fb',
    'restore': 'b4f86832a7f86832237f3b777047',
    'cue_set': '002c0cbf0220002085f868022bf08abe',
    'cue_cur': '192901d129f046be042906d197f8681202290cbf0021012105e0072905d197f8691201f00101a181b08029f051be',
    'cue_list': '4cf24c00c0f2003090f86802022803d02cf47f0c4cf4803ccdf800c67047',
    'site_restore': 'd6f756fd',
    'site_cur': 'd6f70eba',
    'site_list': 'd5f795fe',
    'site_mv': 'e2f780fb',
    'pwr_set': '85f869422bf060be',
    'wfi': '0949097809b919f03dbd08480068c043c00f7047044a1278002a0cbf4ff4c8724ff47a72704700bfb5c20003c0ca0003',
    'pwr_row': 'c4ebc4010d480078a8b10222601f062807d904220938022803d905220538012809d8064842430598104405900698104406900ef1560e704700bfb5c20003006cdc02',
    'site_wfi': 'e7f755fa',
    'site_row': 'e6f70dfc',
    'site_wheel0': 'e6f7b0f9',
    'site_wheel1': 'd6f796fa',
    'site_wheel9': 'd4f720f8',
    'site_wheel33': 'cdf7c8fc',
}.items()}

TITLE_AT = CAVE
KEY_AT = CAVE + 0x10
AA_AT = CAVE + 0x60
CUT_AT = 0x030518A0          # dead code after the AA helper, ..0x030518F0 (turbo uses 0x030518F0..)
MV_AT = CUT_AT + 0x38
CUT_END = 0x030518F0


def build_patches():
    t = asm(ASM_TITLE.format(memcpy=MEMCPY), TITLE_AT) or PREBUILT['title']
    k = asm(ASM_KEY, KEY_AT) or PREBUILT['key']
    aa = asm(ASM_AA, AA_AT) or PREBUILT['aa']
    cut = asm(ASM_CUT.replace('{key:#x}', f'{KEY_AT:#x}'), CUT_AT) or PREBUILT['cut']
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
    site_key = asm(f"b.w {CUT_AT:#x}", 0x0307B80C) or PREBUILT['site_key']
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
        # seg 40: song-level sort: emit bucket 0 (keys starting with NUL) too, like the
        # album/artist levels do, so no song falls out of the lists
        (40, 0x0307AE6A, bytes.fromhex('2dd0'), bytes.fromhex('00d0')),
    ]


# ---------------------------------------------------------------- CUE sheets setting
# Music Settings gets a 7th item, "CUE sheets": Stock / Off. The menus are data:
# every string-table record (per language) is a node, header u16s = id, parent, index,
# first child, prev, next, action, child count, children[21]. The FM Settings subtree
# (0x10b, regions 0x10c-0x10f) is never shown on this player; 0x10b-0x10e become the
# item and its two choices. The choice lives in the settings struct at +0x268
# (0x0300C04C; saved with it, 0x2d8 bytes; nothing else uses 0x25b..0x2d3), stored as
# 2 = Stock, anything else = Off (a fresh block holds 0).
# (An "Auto" that skips only split-track sheets isn't possible this way: the directory
# pre-pass (0x0305dfa4) already validates each sheet and claims its FILE's entry, and
# skipping a sheet after that leaves the folder index inconsistent; the refresh crashes.)
CUE_CAVE = 0x03051650        # literal + strings of three unreferenced UMsg.c printf helpers
CUE_CAVE_END = 0x03051700    # (0x03051606..0x0305164c, PWR_CAVE). Not 0x0305164c..0x03051650:
                             # that word is the live bit-flag helpers' pool (0x03051554..)
PWR_CAVE = 0x03051606        # the three printf helpers themselves (cavecheck: no live reference)
PWR_CAVE_END = 0x0305164C
CUE_OFF = 0x268
PWR_OFF = 0x269
RESTORE_AT = 0x03051542       # the last 14 free bytes of turbo/opus's cave (..0x03051550)
CUE_SET_AT = CUE_CAVE
CUE_CUR_AT = CUE_CAVE + 0x10
CUE_LIST_AT = CUE_CAVE + 0x3E
PWR_SET_AT = CUE_CAVE + 0x5C
WFI_AT = CUE_CAVE + 0x64
WHEEL_AT = WFI_AT + 0x14
PWR_ROW_AT = PWR_CAVE
SETTINGS = 0x0300C04C

# boot: the SAVE-block loader copies known fields one by one; also copy ours
ASM_RESTORE = f"""
    ldrh.w  r3, [r4, #{CUE_OFF:#x}]   ; CUE sheets (+0x268) and Power saving (+0x269)
    strh.w  r3, [r7, #{CUE_OFF:#x}]
    ldrb    r3, [r4, #0x1c]     ; the copy we displaced
    strb    r3, [r7, #0x1c]
    bx      lr
"""
# settings (mod32) leaf dispatcher, ids 0x10c-0x10d: r4 = chosen index, r5 = settings
ASM_CUE_SET = f"""
    cmp     r4, #0              ; Stock (0) -> 2, Off (1) -> 0
    ite     eq
    moveq   r0, #2
    movne   r0, #0
    strb.w  r0, [r5, #{CUE_OFF:#x}]
    b.w     0x0307d374          ; common exit: saves the settings if they changed
"""
# settings (mod32) "current choice" switch, branch for ids 0x101..0x121 (r1 = id - 0x107)
ASM_CUE_CUR = f"""
    cmp     r1, #0x19           ; the case we displaced (id 0x120)
    bne     not_120
    b.w     0x0307b2f4
not_120:
    cmp     r1, #4              ; id 0x10b: CUE sheets
    bne     not_cue
    ldrb.w  r1, [r7, #{CUE_OFF:#x}]
    cmp     r1, #2              ; shown: Stock 0, Off 1
    ite     eq
    moveq   r1, #0
    movne   r1, #1
    b       show
not_cue:
    cmp     r1, #7              ; id 0x10e: Power saving
    bne     out
    ldrb.w  r1, [r7, #{PWR_OFF:#x}]
    and     r1, r1, #1          ; shown: Off 0, On 1
show:
    strh    r1, [r4, #0xc]
    strh    r0, [r6, #4]
out:
    b.w     0x0307b330
"""
# scanner (mod39) start: it copies the extension list "MP1...DSFCUE" to the stack; the
# last word but one ("SFCU") is stored here. Off: "C" -> 0x01, so no .cue file matches,
# in the directory pre-pass and in the walk alike (as if the firmware had no CUE support).
ASM_CUE_LIST = f"""
    movw    r0, #0xc04c
    movt    r0, #0x0300
    ldrb.w  r0, [r0, #{CUE_OFF:#x}]
    cmp     r0, #2              ; Stock: keep "CUE"
    beq     keep
    bic     ip, ip, #0xff0000
    orr     ip, ip, #0x10000
keep:
    str.w   ip, [sp, #0x600]    ; the store we displaced (r0 is dead here)
    bx      lr
"""
# ---------------------------------------------------------------- Power saving setting
# System Settings gets an 11th item, "Power saving": Off / On (nodes 0x10e, 0x10f, 0x111:
# the rest of the FM subtree and the first Record Settings leaf, both never shown).
# +0x269 of the settings struct: 1 = On. On does what nanitec --power used to do for good:
#   - decode-core clocks: the codec clock rows (0x03077500, summed by 0x0306ad98 over the
#     active clients) for MP3/WMA/WAV/AAC (rows 5-11), OGG (14-16) and FLAC/ALAC (19, 20)
#     count as A 0 MHz and B core = B bus = 96 / 192 / 240 MHz. Takes effect the next time
#     the clocks are worked out (next track);
#   - the main loop (0x0306a204) sleeps on every pass unless msg 0x1f (the screen-timeout
#     inhibit) is set, instead of only when msg 0x18 is set. Takes effect at once;
#   - the scroll-wheel timer (IRQ 0x23) polls every 1 ms instead of 0.4 ms. Its four setup
#     sites (mod0, mod1 at boot, mod9, mod33 when playback starts) ask us for the period.

# leaf dispatcher, ids 0x10f (Off, index 0) and 0x111 (On, index 1)
ASM_PWR_SET = f"""
    strb.w  r4, [r5, #{PWR_OFF:#x}]
    b.w     0x0307d374
"""
# main loop: replaces "bl 0x0306b138" (test msg r0 = 0x18) at 0x0306a206.
# Then the wheel timer setup: replaces "mov.w r2, #400" (flags and lr are free at all
# four sites). One block, so both can use the literals at its end.
ASM_WFI = f"""
    ldr     r1, pwr_byte
    ldrb    r1, [r1]
    cbnz    r1, on
    b.w     0x0306b138          ; Off: stock test of msg 0x18
on:
    ldr     r0, msgs
    ldr     r0, [r0]            ; msg bits 0..31
    mvns    r0, r0
    lsrs    r0, r0, #31         ; 1 (sleep) unless msg 0x1f is set
    bx      lr
wheel:                          ; = WHEEL_AT (WFI_AT + 0x14)
    ldr     r2, pwr_byte
    ldrb    r2, [r2]
    cmp     r2, #0
    ite     eq
    moveq.w r2, #400
    movne.w r2, #1000
    bx      lr
    .align 2
pwr_byte:
    .word   {SETTINGS + PWR_OFF:#x}
msgs:
    .word   0x0300cac0
"""
# clock sum loop: replaces "rsb r1, r4, r4, lsl #3" (r4 = row) at 0x0306ade8. On, for the
# codec rows: add B core and B bus to the sums ourselves and skip the row (0x0306ae42).
ASM_PWR_ROW = f"""
    rsb     r1, r4, r4, lsl #3
    ldr     r0, pwr_byte2
    ldrb    r0, [r0]
    cbz     r0, out
    movs    r2, #2              ; x 48 MHz
    subs    r0, r4, #5
    cmp     r0, #6              ; rows 5..11
    bls     hit
    movs    r2, #4
    subs    r0, #9
    cmp     r0, #2              ; rows 14..16
    bls     hit
    movs    r2, #5
    subs    r0, #5
    cmp     r0, #1              ; rows 19, 20
    bhi     out
hit:
    ldr     r0, mhz48
    muls    r2, r0
    ldr     r0, [sp, #0x14]     ; B core sum
    add     r0, r2
    str     r0, [sp, #0x14]
    ldr     r0, [sp, #0x18]     ; B bus sum
    add     r0, r2
    str     r0, [sp, #0x18]
    add.w   lr, lr, #0x56       ; return to 0x0306ae42: next row
out:
    bx      lr
    .align 2
pwr_byte2:
    .word   {SETTINGS + PWR_OFF:#x}
mhz48:
    .word   48000000
"""
PWR_STR = {  # item, Off, On; other languages use English
    0: ('省电模式', '关闭', '开启'),
    1: ('省電模式', '關閉', '開啟'),
    3: ('省電力モード', 'オフ', 'オン'),
}
PWR_STR_EN = ('Power saving', 'Off', 'On')

CUE_STR = {  # item, Stock, Off; other languages use English
    0: ('CUE 文件', '原厂', '关闭'),
    1: ('CUE 檔案', '原廠', '關閉'),
    3: ('CUEシート', '標準', 'オフ'),
}
CUE_STR_EN = ('CUE sheets', 'Stock', 'Off')


def cue_code():
    out = []
    for name, src, at in (('restore', ASM_RESTORE, RESTORE_AT), ('cue_set', ASM_CUE_SET, CUE_SET_AT),
                          ('cue_cur', ASM_CUE_CUR, CUE_CUR_AT), ('cue_list', ASM_CUE_LIST, CUE_LIST_AT),
                          ('pwr_set', ASM_PWR_SET, PWR_SET_AT), ('wfi', ASM_WFI, WFI_AT),
                          ('pwr_row', ASM_PWR_ROW, PWR_ROW_AT)):
        out.append((at, asm(src, at) or PREBUILT[name]))
    assert RESTORE_AT + len(out[0][1]) <= 0x03051550
    assert PWR_ROW_AT + len(out[6][1]) <= PWR_CAVE_END
    wfi = out[5][1]
    k = WHEEL_AT - WFI_AT
    assert wfi[k - 2:k] == bytes.fromhex('7047') and wfi[k + 1] == 0x4a, wfi.hex()  # bx lr | ldr r2, [pc, ..]
    ends = [a + len(b) for a, b in out[1:6]]
    starts = [a for a, _ in out[2:6]] + [CUE_CAVE_END]
    assert all(e <= s for e, s in zip(ends, starts)), [hex(e) for e in ends]
    return out


SITE_NAMES = {(1, 0x0307AA92): 'site_restore', (32, 0x0307B240): 'site_cur', (39, 0x0307B960): 'site_list',
              (0, 0x0306A206): 'site_wfi', (0, 0x0306ADE8): 'site_row', (0, 0x0306B364): 'site_wheel0',
              (1, 0x0307B198): 'site_wheel1', (9, 0x0307D684): 'site_wheel9', (33, 0x03083D34): 'site_wheel33'}


def cue_sites():
    rel = lambda tgt: struct.pack('<I', (tgt - 0x0307CF28) & 0xffffffff)
    bl = lambda tgt, at, name: asm(f"bl {tgt:#x}", at) or PREBUILT[name]
    return [
        (0, CUE_CAVE, bytes.fromhex('c62f07032e2e5c2e'), None),            # caves still stock
        (0, PWR_CAVE, bytes.fromhex('08b539230090104a'), None),
        (0, RESTORE_AT, b'C Time Tags ==', None),                     # rest of the LRC dump's string
        (1, 0x0307AA92, bytes.fromhex('237f3b77'), asm(f"bl {RESTORE_AT:#x}", 0x0307AA92) or PREBUILT['site_restore']),
        (32, 0x0307B240, bytes.fromhex('58d075e0'), asm(f"b.w {CUE_CUR_AT:#x}", 0x0307B240) or PREBUILT['site_cur']),
        # leaf dispatcher jump table (offsets from 0x0307cf28), ids 0x10c, 0x10d
        (32, 0x0307CF98, bytes.fromhex('4c040000'), rel(CUE_SET_AT)),
        (32, 0x0307CF9C, bytes.fromhex('4c040000'), rel(CUE_SET_AT)),
        (39, 0x0307B960, bytes.fromhex('cdf800c6'), asm(f"bl {CUE_LIST_AT:#x}", 0x0307B960) or PREBUILT['site_list']),
        # Power saving: ids 0x10f (Off), 0x111 (On)
        (32, 0x0307CFA4, bytes.fromhex('4c040000'), rel(PWR_SET_AT)),
        (32, 0x0307CFAC, bytes.fromhex('4c040000'), rel(PWR_SET_AT)),
        (0, 0x0306A206, bytes.fromhex('00f097ff'), bl(WFI_AT, 0x0306A206, 'site_wfi')),
        (0, 0x0306ADE8, bytes.fromhex('c4eb c401'.replace(' ', '')), bl(PWR_ROW_AT, 0x0306ADE8, 'site_row')),
    ] + [(seg, at, bytes.fromhex('4ff4c872'), bl(WHEEL_AT, at, f'site_wheel{seg}'))
         for seg, at in ((0, 0x0306B364), (1, 0x0307B198), (9, 0x0307D684), (33, 0x03083D34))]


def cue_menu(raw):
    """relink the string-table nodes, every language"""
    hdr = parse_header(bytes(raw))
    off, size = hdr['strtbl'][0]
    langs = fwtool.parse_strings(bytes(raw[off:off + size]))
    F = 0xffff
    nodes = {
        # Music Settings > CUE sheets: Stock / Off
        0x10b: [0x10b, 0xed, 6, 0x10c, 0x108, F, F, 2, 0x10c, 0x10d],
        0x10c: [0x10c, 0x10b, 0, F, F, 0x10d, 0x17, 0],
        0x10d: [0x10d, 0x10b, 1, F, 0x10c, F, 0x18, 0],
        # System Settings > Power saving: Off / On
        0x10e: [0x10e, 0x120, 10, 0x10f, 0x161, F, F, 2, 0x10f, 0x111],
        0x10f: [0x10f, 0x10e, 0, F, F, 0x111, 0x1a, 0],
        0x111: [0x111, 0x10e, 1, F, 0x10f, F, 0x1b, 0],
    }
    sys_kids = [0x121, 0x133, 0x140, 0x143, 0x147, 0x15d, 0x15e, 0x15f, 0x160, 0x161]
    for li, (lo, recs) in enumerate(langs):
        rec = lambda i: off + lo + i * fwtool.STR_REC
        h = lambda i: list(struct.unpack_from('<29H', raw, rec(i)))
        ed, g, st, last = h(0xed), h(0x108), h(0x120), h(0x161)
        assert ed[:3] == [0xed, 0xec, 0] and ed[7] == 6 and ed[8:14] == [0xee, 0xf7, 0x100, 0x106, 0x107, 0x108], li
        assert g[:3] == [0x108, 0xed, 5] and g[5] == F and h(0x10b)[:2] == [0x10b, 0xec], li
        assert st[:3] == [0x120, 0xec, 3] and st[7] == 10 and st[8:18] == sys_kids and st[18] == F, li
        assert last[:3] == [0x161, 0x120, 9] and last[5] == F, li
        assert h(0x10e)[:2] == [0x10e, 0x10b] and h(0x10f)[:2] == [0x10f, 0x10b] and h(0x111)[:2] == [0x111, 0x110], li
        ed[7], ed[14] = 7, 0x10b
        g[5] = 0x10b
        st[7], st[18] = 11, 0x10e
        last[5] = 0x10e
        for i, v in ((0xed, ed), (0x108, g), (0x120, st), (0x161, last)):
            struct.pack_into('<29H', raw, rec(i), *v)
        for i, v in nodes.items():
            struct.pack_into('<29H', raw, rec(i), *(v + [F] * (29 - len(v))))
        texts = list(zip((0x10b, 0x10c, 0x10d), CUE_STR.get(li, CUE_STR_EN)))
        texts += zip((0x10e, 0x10f, 0x111), PWR_STR.get(li, PWR_STR_EN))
        for i, txt in texts:
            enc = txt.encode('utf-16le')
            assert len(enc) < 200
            raw[rec(i) + 58:rec(i) + 258] = enc + bytes(200 - len(enc))


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
    for seg, addr, old, new in cue_sites():
        off = seg_offset(raw, seg, addr, len(old))
        if raw[off:off + len(old)] != old:
            raise SystemExit(f"seg{seg} {addr:#x}: unexpected bytes {raw[off:off + len(old)].hex()} "
                             f"(this patch is for ECHO NANO V2.0.0 only)")
        if new is not None:
            raw[off:off + len(new)] = new
    for at, blob in cue_code():
        o = seg_offset(raw, 0, at, len(blob))
        raw[o:o + len(blob)] = blob
    cue_menu(raw)
    # newer date than stock: the loader boots the firmware copy with the newest date
    raw[0:4] = bytes.fromhex('26203110')          # 2026-10-31 (stock V2.0.0: 2026-10-29)
    size = parse_header(bytes(raw))['size']
    return fwtool.finalize(bytes(raw[:size]))


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == '--print-bytes':
        (t, k, aa, cut, mv), sites = build_patches()
        print(repr({'title': t.hex(), 'key': k.hex(), 'aa': aa.hex(), 'cut': cut.hex(), 'mv': mv.hex(),
                    'site_title': sites[0][3].hex(), 'site_key': sites[1][3].hex(),
                    'site_aa': sites[5][3].hex(), 'site_mv': sites[6][3].hex()}))
        names = ('restore', 'cue_set', 'cue_cur', 'cue_list', 'pwr_set', 'wfi', 'pwr_row')
        print(repr({n: b.hex() for n, (_, b) in zip(names, cue_code())}))
        print(repr({SITE_NAMES[(seg, at)]: new.hex() for seg, at, _, new in cue_sites()
                    if (seg, at) in SITE_NAMES}))
        raise SystemExit
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    out = apply(open(sys.argv[1], 'rb').read())
    open(sys.argv[2], 'wb').write(out)
    print(f"wrote {sys.argv[2]}  (navilib: album = ALBUM+ALBUMARTIST, titles from tags)")
