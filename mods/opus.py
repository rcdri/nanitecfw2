#!/usr/bin/env python3
"""opus: Opus (.opus) playback for SNOWSKY ECHO NANO V2.0.0.

  python3 mods/opus.py IN.IMG OUT.IMG [--date YYYYMMDD] [--rebuild]

IN.IMG: stock V2.0.0 or the output of the other mods (navilib / nanitec / turbo).
The image keeps IN.IMG's date unless --date is given (see nanitec.py).

Codec core: a fixed-point CELT decoder (libopus, mods/opus/) plus an Ogg/Opus
front end is appended to the Ogg Vorbis codec image (segment 77, whose code
grows from 0xF5D8 to ~0x1DB00 bytes of its 128 KB window). The image's codec
slot for Ogg now points at ogg_entry(), which sends streams that start with
"OpusHead" to the new decoder and everything else to the stock Tremor code,
unchanged. State lives in core-B RAM the image never uses (0x0105F000..).

Main core: every part of the player picks a file's type from its 8.3 extension
("OPU" for *.opus, both on FAT32 where it is on disk and on exFAT where the
firmware makes one up from the long name). Two small hooks make "OPU" read as
"OGG" right where those names come from (FAT directory-entry reader, exFAT
short-name generator), so the folder browser, library scan, tag reader and
codec choice all treat .opus as Ogg. Nothing is written to the card. The Ogg
tag reader also accepts an "OpusTags" packet (same layout as Vorbis comments
after an 8-byte magic).

The stock Vorbis-comment parser trusts the packet length and has no notion of
the comment count. Taggers (mutagen/Picard, opusenc) append zero padding to
OpusTags, which the parser read as zero-length comments: it then seeked back
and re-read the same bytes up to 512 times, about 130 KB of one-byte reads per
file. A zero-length comment is invalid in both formats, so it now ends the list.

Settings > System settings > Music support gets an "OPUS:16bit/48kHz" line.

--rebuild recompiles mods/opus/build/opus.bin with mods/opus/build.sh
(clang + ld.lld, libopus sources in $OPUS_SRC).
"""
import os, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from rkfw import parse_header, parse_stage   # noqa: E402
import fwtool                                 # noqa: E402
from nanitec import apply_sites, set_date, H  # noqa: E402

SEG = 77
STOCK_CODE_SZ = 0xF5D8
TABLE_SLOT4 = 0x0102D16C          # codec table 0x0102D15C + 4*4 (Ogg)
BLOB = os.path.join(HERE, 'opus', 'build', 'opus.bin')
ELF = os.path.join(HERE, 'opus', 'build', 'opus.elf')


def entry_addr():
    nm = subprocess.check_output(['llvm-nm', ELF]).decode() if os.path.exists(ELF) else ''
    for l in nm.splitlines():
        v, _, n = l.split()
        if n == 'ogg_entry':
            return int(v, 16)
    return 0x0000F5D8             # ogg_entry is placed first (KEEP(.text.ogg_entry))


def grow_segment_code(raw, seg, extra):
    """append `extra` bytes to a segment's code, shifting everything after it"""
    raw = bytearray(raw[:parse_header(bytes(raw))['size']])
    hdr = parse_header(bytes(raw))
    so, ss = hdr['stage'][3]
    stage = raw[so:so + ss]
    base, segs = parse_stage(bytes(stage))
    sg = segs[seg]
    extra = bytes(extra) + b'\0' * (-len(extra) % 4)
    grow = len(extra)
    ins = sg['code_va'] - base + sg['code_sz']
    for k, s in enumerate(segs):
        for va, sz in (('code_va', 'code_sz'), ('data_va', 'data_sz')):
            if s[sz] and s[va] - base >= ins:
                s[va] += grow
    sg['code_sz'] += grow
    for k, s in enumerate(segs):
        struct.pack_into('<8I', stage, 8 + 32 * k, *[s[f] for f in fwtool.SEG_FIELDS])
    stage = stage[:ins] + extra + stage[ins:]
    body = raw[:so] + stage + raw[so + ss:]
    # header slots: stage 3 grows, blobs after it move
    for cat, n in (('stage', 4), ('font', 10), ('gbk', 5), ('strtbl', 10), ('imageres', 10), ('unk', 10)):
        for i in range(n):
            slot = hdr[cat + '_slot_off'] + 8 * i
            off, size = struct.unpack_from('<II', body, slot)
            if not size:
                continue
            if (cat, i) == ('stage', 3):
                size += grow
            elif off > so:
                off += grow
            struct.pack_into('<II', body, slot, off, size)
    return body


def apply(raw, rebuild=False, date=None):
    if rebuild:
        subprocess.check_call([os.path.join(HERE, 'opus', 'build.sh')])
    blob = open(BLOB, 'rb').read()
    entry = entry_addr()
    raw = bytearray(raw)
    hdr = parse_header(bytes(raw))
    so, ss = hdr['stage'][3]
    base, segs = parse_stage(bytes(raw[so:so + ss]))
    if segs[SEG]['code_sz'] != STOCK_CODE_SZ:
        raise SystemExit(f"segment {SEG} code is {segs[SEG]['code_sz']:#x} bytes, expected {STOCK_CODE_SZ:#x} "
                         "(already patched, or not V2.0.0)")
    assert STOCK_CODE_SZ + len(blob) <= 0x1FF00
    # codec slot 4 (Ogg) -> ogg_entry; the stock entry 0x29d1 is called for non-Opus streams
    apply_sites(raw, [(SEG, TABLE_SLOT4, H('d1290000'), struct.pack('<I', entry | 1))] + MAIN_SITES)
    if date:
        set_date(raw, date)
    body = grow_segment_code(raw, SEG, blob)
    return fwtool.finalize(bytes(body))


# ------------------------------------------------------------------ main core
FS_AT = 0x030514C4       # seg0 dead "All LRC Time Tags" dump strings after turbo's code (..0x03051550)
TAG_AT = 0x0305F05C      # seg0 dead FAT routine (K2) after nanitec's code (..0x0305F0C4)

ASM_FS = """
fdt_hook:                         ; bl from ReadDirEntry (0x305b2ae, was bl unlock): r4 = 32-byte entry copy
    push    {r4, lr}
    ldr     r0, =0x03077d00       ; volume type, 3 = exFAT (raw entries: names come from sn_hook)
    ldrb    r0, [r0]
    cmp     r0, #3
    beq     fdt_done
    ldrb    r0, [r4, #0xb]
    cmp     r0, #0xf              ; long-name slot
    beq     fdt_done
    mov     r0, r4
    bl      opu2ogg
fdt_done:
    pop.w   {r4, lr}
    b.w     0x306a4fe             ; the unlock call it replaced
sn_hook:                          ; b.w from MakeShortName's epilogue (0x305fbfe): r4 = name[11]
    mov     r0, r4
    bl      opu2ogg
    add     sp, #0xc
    movs    r0, #0
    pop.w   {r4, r5, r6, r7, r8, r9, pc}
opu2ogg:                          ; "xxxxxxxxOPU" -> "xxxxxxxxOGG"
    ldrb    r1, [r0, #8]
    cmp     r1, #0x4f
    bne     o_done
    ldrb    r1, [r0, #9]
    cmp     r1, #0x50
    bne     o_done
    ldrb    r1, [r0, #10]
    cmp     r1, #0x55
    bne     o_done
    movs    r1, #0x47
    strb    r1, [r0, #9]
    strb    r1, [r0, #10]
o_done:
    bx      lr
"""

ASM_TAG = """
    ; Ogg tag reader (0x306f20a), first packet of a page: type byte in r0, next 6 bytes at sp+0x110
    cmp     r0, #3
    bne     not_vorbis
    b.w     0x306f2d0             ; stock: compare with "vorbis"
not_vorbis:
    cmp     r0, #0x4f             ; 'O' "pusTag" ('s')
    bne     skip
    ldr     r0, [sp, #0x110]
    ldr     r1, =0x54737570
    cmp     r0, r1
    bne     skip
    ldrh.w  r0, [sp, #0x114]
    movw    r1, #0x6761
    cmp     r0, r1
    bne     skip
    mov     r2, r7                ; step over the final 's'
    movs    r1, #1
    movs    r0, #1
    bl      0x305c952
    ldrb.w  r2, [sp, #0x104]      ; packet bytes in this page (lacing up to the first < 255) - 8
    movs    r0, #0
    mvn     r1, #7
lp:
    cmp     r0, r2
    bge     dn
    ldrb    r3, [r5, r0]
    adds    r0, #1
    add     r1, r3
    cmp     r3, #0xff
    beq     lp
dn:
    mov     r3, r1
    mov     r2, r6
    mov     r1, r8
    mov     r0, r7
    bl      0x306efae             ; the Vorbis comment parser
    b.w     0x306f358
skip:
    b.w     0x306f2dc             ; stock: skip the rest of the page
"""

PREBUILT = {k: bytes.fromhex(v) for k, v in {
    'fs': '10b511480078032805d0e07a0f2802d0204600f00bf8bde8104019f00eb8204600f004f803b00020bde8f083'
          '017a4f2908d1417a502905d1817a552902d1472141728172704700bf007d0703',
    'tag': '032801d110f036b94f2822d14498124988421ed1bdf8140146f26171884218d13a4601210120fdf766fc9df8'
           '042100206ff00701904204da2b5c01301944ff2bf8d00b463246414638460ff082ff10f055b910f015b900bf'
           '70757354',
    'site_fdt': 'f6f709f9',      # bl FS_AT              @ 0x0305B2AE
    'site_sn': 'f1f770bc',       # b.w FS_AT + 0x1E      @ 0x0305FBFE
    'site_tag': 'eff7c6be',      # b.w TAG_AT            @ 0x0306F2CC
}.items()}


def asm(src, addr):
    try:
        from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
    except ImportError:
        return None
    enc, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(src.replace(';', '//'), addr)
    return bytes(enc)


def main_sites():
    fs = asm(ASM_FS, FS_AT) or PREBUILT['fs']
    tag = asm(ASM_TAG, TAG_AT) or PREBUILT['tag']
    assert fs == PREBUILT['fs'] and tag == PREBUILT['tag'], 'cave code changed: update PREBUILT'
    assert FS_AT + len(fs) <= 0x03051550 and TAG_AT + len(tag) <= 0x0305F0C4
    assert fs[0x1E:0x20] == bytes.fromhex('2046')     # sn_hook starts with mov r0, r4
    return [
        # the caves must still be stock (dead code / its strings)
        (0, FS_AT, H('6e65640a00000000'), None),
        (0, TAG_AT, H('012402d0002c08bf'), None),
        (0, FS_AT, None, fs),
        (0, TAG_AT, None, tag),
        (0, 0x0305B2AE, H('0ff026f9'), PREBUILT['site_fdt']),   # ReadDirEntry: bl unlock -> bl fdt_hook
        (0, 0x0305FBFE, H('03b00020'), PREBUILT['site_sn']),    # MakeShortName: epilogue -> b.w sn_hook
        (0, 0x0306F2CC, H('032805d1'), PREBUILT['site_tag']),   # Ogg tags: cmp r0,#3; bne -> b.w tag hook
    ]


# Settings > Music support lists the formats in a 10-slot table (16 bytes each:
# UTF-16 string pointer, 0x64 at +0xC) filled by 0x030534BA with 5 entries and a
# count of 5. A sixth entry is added by a helper that replaces the store of
# entry 4's flag. The helper and its string go in the same dead strings, after
# fdt/sn (..0x03051550).
SUP_AT = 0x03051510
SUP_STR = 'OPUS:16bit/48kHz'      # the decoder outputs 16-bit PCM at 48 kHz

# Vorbis-comment parser 0x0306efae, per comment: "len > remaining -> stop" becomes
# "len == 0 or len > remaining -> stop" (unsigned len-1 >= remaining).
#   stock: ldrb.w r0,[sp,#3]; ldr r5,[sp]; bfi r5,r0,#24,#8; cmp sb,r5; blo 0x306f0e4
#   new:   ldr r5,[sp]; subs r0,r5,#1; cmp r0,sb; bhs 0x306f0e4; nop x3
PAD_SITE = (0, 0x0306F008, H('9df80300 009d 60f31f65 a945 66d3'),
            H('009d 681e 4845 69d2 00bf 00bf 00bf'))


def thumb_bl(src, dst):
    off = dst - (src + 4)
    assert -(1 << 24) <= off < (1 << 24) and not off & 1
    s = (off >> 24) & 1
    j1 = (~((off >> 23) & 1) ^ s) & 1
    j2 = (~((off >> 22) & 1) ^ s) & 1
    return struct.pack('<HH', 0xF000 | (s << 10) | ((off >> 12) & 0x3FF),
                       0xD000 | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF))


def support_sites():
    code = H('a0f84c10'      # strh.w r1, [r0, #0x4c]   (entry 4 flag: the store we replaced)
             '02a2'          # adr    r2, string        (SUP_AT + 0x10)
             '0265'          # str    r2, [r0, #0x50]   (entry 5 text)
             'a0f85c10'      # strh.w r1, [r0, #0x5c]   (entry 5 flag = 0x64)
             '7047'          # bx     lr
             '00bf')         # nop (align the string)
    blob = code + SUP_STR.encode('utf-16le') + b'\0\0'
    assert len(code) == 0x10 and SUP_AT + len(blob) <= 0x03051550
    return [
        (0, SUP_AT, H('2564206d73000000'), None),                 # still the stock dump strings
        (0, SUP_AT, None, blob),
        (0, 0x030534E6, H('a0f84c10'), thumb_bl(0x030534E6, SUP_AT)),
        (0, 0x030534F6, H('0522'), H('0622')),                    # count 5 -> 6
    ]


MAIN_SITES = main_sites() + support_sites() + [PAD_SITE]


if __name__ == '__main__':
    args, date, it = [], None, iter(sys.argv[1:])
    for x in it:
        if x == '--date':
            date = next(it)
        elif not x.startswith('--'):
            args.append(x)
    if len(args) != 2:
        raise SystemExit(__doc__)
    out = apply(open(args[0], 'rb').read(), '--rebuild' in sys.argv, date)
    open(args[1], 'wb').write(out)
    print(f"wrote {args[1]}  (opus: CELT decoder in codec image {SEG}, +{len(open(BLOB, 'rb').read()):#x} bytes; "
          f"dated {parse_header(out)['date']})")
