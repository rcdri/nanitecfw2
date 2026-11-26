#!/usr/bin/env python3
"""nanitec: fixes from maaraneasi/nanitecfw (written for FiiO's V1.7.0), relocated
to SNOWSKY ECHO NANO V2.0.0.

  python3 mods/nanitec.py IN.IMG OUT.IMG [--power] [--no-shuffle] [--date YYYYMMDD]
  python3 mods/nanitec.py --recovery NANOV200.IMG OUT.IMG [--date YYYYMMDD]

IN.IMG is stock V2.0.0, or the output of mods/navilib.py (the two mods don't
touch the same bytes). Every patch site is checked against the stock bytes
first.

What it changes:

  skip      Undecodable tracks are skipped without the "Format Not Supported!"
            popup in Now Playing, and playback stops after 10 failures in a row
            (stock: after as many failures as the playlist has songs).
  shuffle   Main menu gets two items (7 instead of 5):
            "Shuffle": shuffle-play the whole card in one press (play mode
            shuffle, random song from All Songs).
            "Random Album": picks a random album and plays it in order from
            track 1 (play order normal; a shuffle play mode becomes repeat-all).
  about     About shows the software version as "2.0.0m".
  date      The image is dated 2026-10-31 (later day than stock's 10-29; seen to
            install over 10-30 and 10-31 builds; see README.md, "Install").
  power     (--power, off by default) lower decode-core clocks during playback,
            let the CPU sleep between events with the screen on, poll the wheel
            every 1 ms instead of every 0.4 ms. nanitecfw marks this as not
            yet measured and not tested with hi-res (24/192) files.

--recovery writes the unmodified stock code dated 2026-10-31, so you can go
back to stock after flashing a modded image.

Relocation notes (V1.7.0 -> V2.0.0):
  mod21 (main menu) moved by +0x11dc as a whole; mod00 message API +0x98
  (msg_post 0x0306b0d8, test+clear 0x0306b0f4, get_key 0x030632da); globals
  in the 0x0300dxxx block moved by -0x1118 (MusicTypeSelID 0x0300c36a,
  settings 0x0300c04c, ticks 0x0300c02c). mod23 (media browser) was rewritten
  in V2.0.0: browser title id is at 0x0300e704, cursor at 0x0300e968
  ([0] count, +0xc index), key fetch at 0x0307cad6. Clock table rows are at
  0x03077500 (36 x 0x1c); V2.0.0 already lowered rows 5/6 (MP3 <=128k).
"""
import os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from rkfw import parse_header, parse_stage     # noqa: E402
import fwtool                                  # noqa: E402

VERSION = "2.0.0m"

# ---------------------------------------------------------------- addresses
MSG_POST = 0x0306B0D8
GET_KEY = 0x030632DA
DRAW_ICON = 0x03050868          # (x, y, index, table)
HIGHLIGHT = 0x03050642
MM_CAVE = 0x0307A6E0            # mod21: unreferenced "UNICODEXDIC" writer, ..0x0307A816
MM_CAVE_END = 0x0307A816
# mod21 cave: data only read by the main-menu painter, i.e. while mod21 is loaded
ICON_TABLE = MM_CAVE            # 7 icon pointers
ICON = MM_CAVE + 0x1C           # shuffle icon (40 bytes)
ALBUM_ICON = MM_CAVE + 0x44     # random-album icon (40 bytes)
MENU_TABLE = MM_CAVE + 0x6C     # 7 x 8-byte main-menu entries (stock table: 6 at 0x0300E5D0)
# mod00: the FAT routine at 0x0305EE0C..0x0305F0C4 is never called, but the
# compiler put the literal pool of the live FAT functions 0x0305EB86,
# 0x0305EC00 and 0x0305ECB2 in the middle of it (0x0305EF00..0x0305EF1C).
# Overwriting that pool broke file delete/rename. Only the two halves around
# it are free (tools/cavecheck.py: no live reference).
K2 = 0x0305EE10
K2_POOL = 0x0305EF00
K2_POOL_END = 0x0305EF1C
K2_END = 0x0305F0C4
STRIP = K2                      # ..K2_POOL
ROUTE = K2 + 0x3C
KEY = K2_POOL_END               # ..TAG_AT of mods/opus.py (0x0305F05C)
KEY_END = 0x0305F05C
K2_POOL_STOCK = '407d0703447d0703007d07030c7d0703047d0703f7ffff0f027d0703'
FLAG = 0x0309C1F0               # free RAM (see turbo): random-album second step countdown
ACC = 0x0309C1F4                # scroll acceleration: +0 u16 repeats in a row, +2 u8 pending steps, +4 u32 held key

ASM_ROUTE = f"""
    movw    r1, #0xc36a
    movt    r1, #0x0300         ; MusicTypeSelID
    movw    r2, #0xe600
    movt    r2, #0x0300         ; main-menu cursor
    ldrh    r2, [r2]
    movw    r3, #0xc04c
    movt    r3, #0x0300         ; settings / library counters
    ldrh    r0, [r3, #0x30]     ; All Songs count
    cbz     r0, other
    cmp     r2, #5
    beq     shuf
    cmp     r2, #6
    bne     other
    movs    r0, #4
    strh    r0, [r1]            ; MusicTypeSelID = Albums
    movs    r0, #0
    strb    r0, [r3, #0x6f]     ; play order: in order
    ldrb    r0, [r3, #0x6d]
    and     r2, r0, #3
    cmp     r2, #3
    bne     keep
    and     r0, r0, #4
    orr     r0, r0, #2          ; was shuffle -> repeat all (keeps the folder-jump bit)
    strb    r0, [r3, #0x6d]
keep:
    movs    r5, #0x83           ; title "Random Album"
    b       open
shuf:
    movs    r0, #1
    strh    r0, [r1]            ; MusicTypeSelID = All Songs
    strb    r0, [r3, #0x6f]     ; play order: shuffle
    ldrb    r0, [r3, #0x6d]
    and     r0, r0, #4          ; keep the folder-jump bit
    orr     r0, r0, #3          ; play mode: shuffle
    strb    r0, [r3, #0x6d]
    movs    r5, #0x82           ; title "Shuffle"
open:
    movw    r0, #0x113          ; same messages as Music Settings > Play mode
    bl      {MSG_POST:#x}
    movw    r0, #0x114
    bl      {MSG_POST:#x}
    movw    r1, #0xc47a
    movt    r1, #0x0300         ; cleared by mod22 before it opens the browser
    movs    r0, #0
    strb    r0, [r1]
    strh    r0, [sp]
    strh    r5, [sp, #2]        ; window 2 {{0, title}}
    mov     r1, sp
    movs    r0, #2
    b.w     0x0307ad76          ; dispatcher: bl open_window(r0, r1); return 1 (pops r4/r5)
other:
    movs    r0, #0
    b.w     0x0307ad6c          ; stock Media Library path
"""

ASM_STRIP = f"""
    push    {{r0, r1, r2, r3, r4, r5, r6, lr}}
    movw    r4, #0x48a8
    movt    r4, #0x0300         ; icon strip: +0 y, +2 x, +0xa first item
    movs    r5, #3
loop:
    ldrb    r2, [r4, #0xa]
    adds    r2, r2, r5
    cmp     r2, #7
    bhs     next
    ldrsh.w r0, [r4, #2]
    movs    r3, #25
    mla     r0, r3, r5, r0      ; x + 25 * k
    ldrsh.w r1, [r4]
    movw    r3, #{ICON_TABLE & 0xffff:#x}
    movt    r3, #{ICON_TABLE >> 16:#x}
    bl      {DRAW_ICON:#x}
next:
    adds    r5, #1
    cmp     r5, #5
    blo     loop
    pop     {{r0, r1, r2, r3, r4, r5, r6, lr}}
    b.w     {HIGHLIGHT:#x}
"""

ASM_KEY = f"""
    push    {{r4, lr}}
    bl      {GET_KEY:#x}
    ; --- hold-to-scroll acceleration (media browser lists only) ---
    ; While a key is held the browser gets one repeat event per ~107 ms. After
    ; ~1/3/6 s each repeat is followed by 1/4/19 replayed copies, handed out on
    ; polls that have no key, so the browser takes every step itself (the
    ; display stays consistent and the speed is bounded by its redraw rate).
    movw    r3, #{ACC & 0xffff:#x}
    movt    r3, #{ACC >> 16:#x}
    lsrs    r2, r0, #28
    cmp     r2, #3              ; 0x3xxxxxxx = hold repeat
    beq     rep
    cbnz    r0, reset
    ldrb    r2, [r3, #2]        ; no event: replay pending steps
    cbz     r2, keyok
    subs    r2, #1
    strb    r2, [r3, #2]
    ldr     r0, [r3, #4]
    b       edge
reset:
    movs    r2, #0              ; press / long / release: stop
    strh    r2, [r3]
    strb    r2, [r3, #2]
    b       keyok
rep:
    ldrh    r2, [r3]
    adds    r2, #1
    strh    r2, [r3]
    str     r0, [r3, #4]
    movs    r1, #0
    cmp     r2, #9              ; < ~1 s: stock
    blo     setp
    movs    r1, #1              ; 1-3 s: x2
    cmp     r2, #28
    blo     setp
    movs    r1, #4              ; 3-6 s: x5
    cmp     r2, #56
    blo     setp
    movs    r1, #19             ; > 6 s: x20 (redraw-bound)
setp:
    strb    r1, [r3, #2]
    cmp     r2, #9
    blo     keyok               ; not accelerating: stock, including the wrap
edge:
    movw    r1, #0xe968
    movt    r1, #0x0300         ; list cursor: [0] count, +0xc index
    ldrh    r2, [r1, #0xc]
    and     ip, r0, #0xff       ; key id: 4 = next, 8 = prev
    cmp.w   ip, #4
    bne     eup
    ldrh    ip, [r1]
    sub.w   ip, ip, #1
    cmp     r2, ip
    blo     keyok
    b       swallow             ; on the last item: stop instead of wrapping
eup:
    cmp.w   ip, #8
    bne     keyok
    cbnz    r2, keyok
swallow:
    movs    r2, #0
    strb    r2, [r3, #2]
    movs    r0, #0
keyok:
    movw    r1, #0xe704
    movt    r1, #0x0300         ; browser title string id
    ldrh    r2, [r1]
    movw    r4, #0xe968
    movt    r4, #0x0300         ; list cursor: [0] count, +0xa level, +0xc index
    cmp     r2, #0x82
    beq     shuffle
    cmp     r2, #0x83
    beq     album
    movw    r3, #{FLAG & 0xffff:#x}
    movt    r3, #{FLAG >> 16:#x}
    ldrb    r2, [r3]            ; random album, step 2: countdown armed?
    cbz     r2, out
    subs    r2, #1
    strb    r2, [r3]
    ldrh    r2, [r4, #0xa]
    cmp     r2, #1              ; inside the album
    bne     out
    movs    r2, #0
    strb    r2, [r3]
    strh    r2, [r4, #0xc]      ; first track
    b       select
album:
    movs    r2, #0x5c           ; "Albums"
    strh    r2, [r1]
    mov.w   ip, #1
    b       pick
shuffle:
    movs    r2, #0x5a           ; "All Songs"
    strh    r2, [r1]
    mov.w   ip, #0
pick:
    ldrh    r1, [r4]
    cbz     r1, out
    movw    r2, #0xc02c
    movt    r2, #0x0300
    ldr     r0, [r2]            ; 10 ms ticks
    movw    r2, #0xe018
    movt    r2, #0xe000
    ldr     r2, [r2]            ; SysTick current value
    eor     r0, r0, r2
    eor     r0, r0, r0, lsr #16
    uxth    r0, r0
    muls    r0, r1, r0
    lsrs    r0, r0, #16
    strh    r0, [r4, #0xc]      ; index = (rand16 * count) >> 16
    cmp.w   ip, #0
    beq     select
    movw    r3, #{FLAG & 0xffff:#x}
    movt    r3, #{FLAG >> 16:#x}
    movs    r2, #50
    strb    r2, [r3]            ; arm step 2 for the next 50 key polls
select:
    movs    r0, #0x40
    movt    r0, #0x4000         ; select key
out:
    pop     {{r4, pc}}
"""

# 20x16, 1 bpp, page-major (byte (y>>3)*20 + x, bit y&7); nanitecfw's shuffle icon
ICON_BITS = bytes.fromhex(
    '0000000038 7cfee6c6c6c6c6868e8c0800000000'
    '0000000018 38787061616161737f3f1e00000000'.replace(' ', ''))
STOCK_ICONS = (0x03072E77, 0x03072E27, 0x03072DD7, 0x03072DFF, 0x03072E4F)


def asm(src, addr):
    try:
        from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
    except ImportError:
        return None
    enc, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(src.replace(';', '//'), addr)
    return bytes(enc)


def H(s):
    return bytes.fromhex(s.replace(' ', ''))


# pre-assembled fallbacks, regenerated with --print-bytes
PREBUILT = {k: bytes.fromhex(v) for k, v in {
    'route': '4cf26a31c0f200314ef20062c0f2003212884cf24c03c0f20033188ec8b3052a14d0062a35d104200880002083f86f0093f86d0000f00302032a05d100f0040040f0020083f86d0083250ce00120088083f86f0093f86d0000f0040040f0030083f86d00822540f213100cf00ff940f214100cf00bf94cf27a41c0f2003100200870adf80000adf80250694602201bf04cbf00201bf044bf',
    'strip': '7fb544f6a804c0f200340325a27a5219072a0cd2b4f90200192303fb0500b4f900104af2e063c0f20733f1f715fd0135052debd3bde87f40f1f7fbbb',
    'key': '10b504f0dcf94cf2f413c0f20933020f032a0ad028b99a7882b3013a9a70586814e000221a809a7028e01a8801321a8058600021092a06d301211c2a03d30421382a00d313219970092a17d34ef66811c0f200318a8900f0ff0cbcf1040f06d1b1f800c0acf1010c624507d303e0bcf1080f03d112b900229a7000204ef20471c0f200310a884ef66814c0f20034822a15d0832a0ed04cf2f013c0f209331a788ab3013a1a706289012a2cd100221a70a28125e05c220a804ff0010c03e05a220a804ff0000c2188e9b14cf22c02c0f2003210684ef21802cef20002126880ea020080ea104080b24843000ca081bcf1000f05d04cf2f013c0f2093332221a704020c4f2000010bd',
    'bl_307b598': 'e3f73afc',
    'bl_307b64e': 'e3f7dffb',
    'bl_307cad6': 'e2f721fa',
    'b_307ad68': 'e4f770b8',
}.items()}


def code(name, src, addr):
    b = asm(src, addr)
    if b is None:
        b = PREBUILT[name]
    return b


# ---------------------------------------------------------------- patch sets
# (segment, address, expected stock bytes, new bytes)
def skip_sites():
    return [
        # music service, msg 0x55 (file open / decode error): also in Now Playing
        (33, 0x030840AA, H('17d0'), H('00bf')),               # beq skip-if-window-3 -> nop
        # stop after 10 failures in a row instead of N = playlist length
        (33, 0x030840BC, H('baf80010 8142 43d9'), H('0a28 45d2 00bf 00bf')),  # cmp r0,#10; bhs stop
    ]


def shuffle_sites():
    route = code('route', ASM_ROUTE, ROUTE)
    strip = code('strip', ASM_STRIP, STRIP)
    key = code('key', ASM_KEY, KEY)
    assert STRIP + len(strip) <= ROUTE and ROUTE + len(route) <= K2_POOL, (len(strip), len(route))
    assert K2_POOL_END <= KEY and KEY + len(key) <= KEY_END, len(key)
    icons = struct.pack('<7I', *STOCK_ICONS, ICON, ALBUM_ICON)
    assert ICON + 40 <= ALBUM_ICON and ALBUM_ICON + 40 <= MENU_TABLE and MENU_TABLE + 56 <= MM_CAVE_END
    menu = bytes.fromhex('0100000000000300 0101010000005800 0102020000000000 0103030000000100'
                         '01040400 0000ec00 0105050000008200 0106060000008300'.replace(' ', ''))
    bl = lambda tgt, at: code(f'bl_{at:x}', f'bl {tgt:#x}', at)
    b = lambda tgt, at: code(f'b_{at:x}', f'b.w {tgt:#x}', at)
    return [
        # mod21 main menu: 7 items (stock 5). Table moved to the mod21 cave (literal used by the label painter).
        (21, 0x0307B428, H('d0e50003'), struct.pack('<I', MENU_TABLE)),
        (21, 0x0307B14C, H('0428'), H('0628')),               # right key: wrap above 6
        (21, 0x0307B13A, H('0a80'), H('0620')),               # left key on item 0: moveq r0,#6
        (21, 0x0307B13C, H('00d0'), H('00bf')),               #   and store it below
        (21, 0x0307B50C, H('042a'), H('062a')),               # paint: sel+1 if sel < 6
        (21, 0x0307B54E, H('032a'), H('052a')),               # paint: sel+2 if sel < 5
        (21, 0x0307B612, H('0428'), H('0628')),               # paint icons: sel+1
        (21, 0x0307B62C, H('0328'), H('0528')),               # paint icons: sel+2
        (21, 0x0307AD15, H('36'), H('0b')),                   # dispatcher tbb: case 5 -> Media Library code
        (21, 0x0307AD16, H('a9'), H('0b')),                   #   case 6 too (stock: window 0x0a, no menu item)
        (21, 0x0307B6B4, H('24410003'), struct.pack('<I', ICON_TABLE)),   # icon table -> 7 entries
        (21, 0x0307AD68, H('9649 0020'), b(ROUTE, 0x0307AD68)),           # Media Library path -> route
        (21, 0x0307B598, H('d5f753f8'), bl(STRIP, 0x0307B598)),           # draw icons 6 and 7 in the strip
        (21, 0x0307B64E, H('d4f7f8ff'), bl(STRIP, 0x0307B64E)),
        (21, MM_CAVE - 2, H('2de9f04fdff8'), None),                       # caves must be stock
        (0, K2 - 4, H('2de9ff4f'), None),
        (0, K2_POOL, bytes.fromhex(K2_POOL_STOCK), None),                 # shared literal pool: keep
        (21, ICON_TABLE, None, icons + ICON_BITS + ALBUM_BITS),
        (21, MENU_TABLE, None, menu),
        (0, ROUTE, None, route),
        (0, STRIP, None, strip),
        (0, KEY, None, key),
        # mod23 browser key fetch -> random song / random album when opened from the menu
        (23, 0x0307CAD6, H('e6f700fc'), bl(KEY, 0x0307CAD6)),
    ] + [('str', 0x82, lang, txt) for lang, txt in SHUFFLE_STR.items()] \
      + [('str', 0x83, lang, txt) for lang, txt in ALBUM_STR.items()]


def about_sites():
    new = VERSION.encode('utf-16le')
    assert len(new) <= 14
    return [(32, 0x0307E0CC, '2.0.0'.encode('utf-16le') + b'\0' * 4, new + b'\0' * (16 - len(new)))]


def power_sites():
    rows = 0x03077500
    def row(i, b_hz, old_b):
        a = rows + i * 0x1C + 4
        return (0, a, None, struct.pack('<6I', 0, 0, 0, 0, b_hz, b_hz))
    s = []
    for i in (5, 6, 7, 8, 9, 10, 11):     # MP3 / WMA / WAV / AAC: B core 96 (+48) MHz
        s.append(row(i, 96000000, None))
    for i in (14, 15, 16):                # OGG
        s.append(row(i, 192000000, None))
    for i in (19, 20):                    # FLAC / ALAC
        s.append(row(i, 240000000, None))
    s += [
        # main loop: WFI on every pass unless msg 0x1f (screen-timeout inhibit) is set
        (0, 0x0306A204, H('1820'), H('1f20')),
        (0, 0x0306A20C, H('08d1'), H('08d0')),
        # scroll wheel timer (IRQ 0x23): 400 us -> 1000 us
        (0, 0x0306B364, H('4ff4c872'), H('4ff47a72')),
        (1, 0x0307B198, H('4ff4c872'), H('4ff47a72')),
        (9, 0x0307D684, H('4ff4c872'), H('4ff47a72')),
        (33, 0x03083D34, H('4ff4c872'), H('4ff47a72')),
    ]
    return s


ALBUM_BITS = bytes.fromhex('c070180c04c66232129292123262c6040c1870c0'
                           '030e18302063464c484949484c46632030180e03')   # 20x16 disc
ALBUM_STR = {
    0: "随机专辑", 1: "隨機專輯", 2: "Random Album", 3: "ランダムアルバム", 4: "랜덤 앨범",
    5: "Album au hasard", 6: "Zufallsalbum", 7: "Album casuale", 8: "Álbum aleatorio",
    9: "Álbum aleatório", 10: "Случайный альбом", 11: "Slumpat album", 12: "อัลบั้มสุ่ม",
    13: "Losowy album", 14: "Tilfældigt album", 15: "Willekeurig album", 16: "Τυχαίο άλμπουμ",
    17: "Náhodné album", 18: "Rastgele albüm", 19: "אלבום אקראי", 20: "ألبوم عشوائي",
}
SHUFFLE_STR = {
    0: "随机播放", 1: "隨機播放", 2: "Shuffle", 3: "シャッフル", 4: "셔플", 5: "Aléatoire",
    6: "Zufall", 7: "Casuale", 8: "Aleatorio", 9: "Aleatório", 10: "Случайно", 11: "Blanda",
    12: "สุ่ม", 13: "Losowo", 14: "Bland", 15: "Willekeurig", 16: "Τυχαία", 17: "Náhodně",
    18: "Karışık", 19: "ערבוב", 20: "عشوائي",
}

# stock clock rows we expect before --power (id, PLL, A core, A hclk, A pclk, B core, B bus)
POWER_ROWS_STOCK = {5: 150, 6: 150, 7: 300, 8: 300, 9: 450, 10: 300, 11: 300,
                    14: 400, 15: 400, 16: 400, 19: 400, 20: 480}


# ---------------------------------------------------------------- image helpers
def seg_offset(raw, seg, addr, n):
    hdr = parse_header(bytes(raw))
    o, s = hdr['stage'][3]
    base, segs = parse_stage(bytes(raw[o:o + s]))
    sg = segs[seg]
    for pa, va, sz in ((sg['code_pa'], sg['code_va'], sg['code_sz']),
                       (sg['data_pa'], sg['data_va'], sg['data_sz'])):
        if pa <= addr and addr + n <= pa + sz:
            return o + va - base + addr - pa
    raise SystemExit(f"seg{seg} {addr:#x}+{n}: not inside the segment")


def set_string(raw, idx, lang, text):
    hdr = parse_header(bytes(raw))
    off, size = hdr['strtbl'][0]
    blob = bytes(raw[off:off + size])
    langs = fwtool.parse_strings(blob)
    lo, recs = langs[lang]
    assert idx < len(recs)
    ro = off + lo + idx * fwtool.STR_REC + 58
    enc = text.encode('utf-16le')
    assert len(text) <= fwtool.STR_MAXCH
    raw[ro:ro + 200] = enc + b'\0' * (200 - len(enc))


def set_date(raw, yyyymmdd):
    y, m, d = yyyymmdd[:4], yyyymmdd[4:6], yyyymmdd[6:8]
    raw[0:4] = bytes([int(y[2:], 16), int(y[:2], 16), int(d, 16), int(m, 16)])


def apply_sites(raw, sites):
    for seg, addr, old, new in sites:
        if seg == 'str':
            set_string(raw, addr, old, new)
            continue
        n = len(new) if new is not None else len(old)
        off = seg_offset(raw, seg, addr, n)
        if old is not None and raw[off:off + len(old)] != old:
            raise SystemExit(f"seg{seg} {addr:#x}: unexpected bytes {raw[off:off + len(old)].hex()} "
                             f"(expected {old.hex()}; this mod is for ECHO NANO V2.0.0 only, "
                             f"and must be applied once)")
        if new is not None:
            raw[off:off + len(new)] = new


def check_power_rows(raw):
    off = seg_offset(raw, 0, 0x03077500, 36 * 0x1C)
    for i, mhz in POWER_ROWS_STOCK.items():
        r = struct.unpack_from('<7I', raw, off + i * 0x1C)
        if r[0] != i or r[5] != mhz * 1000000:
            raise SystemExit(f"clock row {i}: unexpected {r} (not stock V2.0.0)")


def apply(raw, power=False, shuffle=True, date='20261031'):
    raw = bytearray(raw)
    sites = skip_sites() + about_sites()
    if shuffle:
        sites += shuffle_sites()
    if power:
        check_power_rows(raw)
        sites += power_sites()
    apply_sites(raw, sites)
    set_date(raw, date)
    size = parse_header(bytes(raw))['size']
    return fwtool.finalize(bytes(raw[:size]))


def recovery(raw, date='20261031'):
    raw = bytearray(raw)
    set_date(raw, date)
    size = parse_header(bytes(raw))['size']
    return fwtool.finalize(bytes(raw[:size]))


def main(argv):
    if argv[:1] == ['--print-bytes']:
        out = {'route': asm(ASM_ROUTE, ROUTE).hex(), 'strip': asm(ASM_STRIP, STRIP).hex(),
               'key': asm(ASM_KEY, KEY).hex()}
        for at, tgt in ((0x0307B598, STRIP), (0x0307B64E, STRIP), (0x0307CAD6, KEY)):
            out[f'bl_{at:x}'] = asm(f'bl {tgt:#x}', at).hex()
        out[f'b_{0x0307AD68:x}'] = asm(f'b.w {ROUTE:#x}', 0x0307AD68).hex()
        print(repr(out))
        return
    opts = {'power': False, 'shuffle': True}
    date = None
    rec = False
    pos = []
    it = iter(argv)
    for a in it:
        if a == '--power':
            opts['power'] = True
        elif a == '--no-shuffle':
            opts['shuffle'] = False
        elif a == '--recovery':
            rec = True
        elif a == '--date':
            date = next(it)
        else:
            pos.append(a)
    if len(pos) != 2:
        raise SystemExit(__doc__)
    raw = open(pos[0], 'rb').read()
    if rec:
        out = recovery(raw, date or '20261031')
    else:
        out = apply(raw, date=date or '20261031', **opts)
    open(pos[1], 'wb').write(out)
    print(f"wrote {pos[1]}  (dated {parse_header(out)['date']})")


if __name__ == '__main__':
    main(sys.argv[1:])
