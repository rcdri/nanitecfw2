#!/usr/bin/env python3
"""Unpack / repack SNOWSKY ECHO NANO firmware images (RKnanoFW container).

  python3 fwtool.py info    NANOV200.IMG
  python3 fwtool.py unpack  NANOV200.IMG work/
  python3 fwtool.py pack    work/ NANOV201.IMG          # rebuilds header copy + CRC
  python3 fwtool.py patch   NANOV200.IMG out.IMG 0:0x03066162=00bf00bf ...
  python3 fwtool.py fixcrc  file.IMG                     # just recompute trailer + CRC

File layout (all little endian):
  0x000  header (512 B): BCD date, "Rockchip", "RKnano SDK 1.0", blob table
         (4 stages, 10 fonts, 5 gbk, 10 strtbl, 10 imageres, 10 unk; {u32 off,u32 size}),
         body size at 0x1F4, "RKnanoFW" at 0x1F8
  ...    blobs; stage 3 = code: {u32 base, u32 count} + count * 32 B segment entries
         {code_va, code_pa, code_sz, data_va, data_pa, data_sz, bss_pa, bss_sz}
  size   copy of the 512 B header, zero padding to a 1 MiB boundary,
         u32 RKCRC32 (poly 0x04C10DB7, init 0, MSB first) over everything before it.
The player itself only checks the RKnanoFW tag and that the header copy matches.
"""
import argparse, json, os, struct, sys, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rkfw import parse_header, parse_stage, CATS   # noqa: E402
from rkcrc import rkcrc32                            # noqa: E402

SEG_FIELDS = ["code_va", "code_pa", "code_sz", "data_va", "data_pa", "data_sz", "bss_pa", "bss_sz"]
STR_REC = 258           # 29 x u16 tree fields + 100 x UTF-16 (99 chars + NUL)
STR_MAXCH = 99
IMG_ENT = 16            # {u16 w, h, x, y; u32 len, off}
IMG_NAME = 108


# ------------------------------------------------------------------ helpers
def blobs(hdr):
    out = []
    for cat, _ in CATS:
        for i, (off, size) in enumerate(hdr[cat]):
            if size:
                out.append((f"{cat}{i}", cat, i, off, size))
    return out


def finalize(body):
    """body = header + blobs. Pad to 16 KiB, store the size, append the header
    copy, zero-pad to a 1 MiB boundary and append the RKCRC32."""
    body = bytearray(body)
    body += b'\0' * (-len(body) % 0x4000)
    hdr = parse_header(bytes(body))
    struct.pack_into('<I', body, hdr['size_off'], len(body))
    img = body + body[:0x200]
    img += b'\0' * (-len(img) % 0x100000)
    img += struct.pack('<I', rkcrc32(bytes(img)))
    return bytes(img)


def check(raw):
    hdr = parse_header(raw)
    ok_tag = hdr['magic'] == b'RKnanoFW'
    size = hdr['size']
    ok_copy = raw[size:size + 0x200] == raw[:0x200]
    crc = struct.unpack('<I', raw[-4:])[0]
    ok_crc = rkcrc32(raw[:-4]) == crc
    return hdr, ok_tag, ok_copy, ok_crc, crc


def rgb565_to_rgb(px):
    return ((px >> 11 & 31) * 255 // 31, (px >> 5 & 63) * 255 // 63, (px & 31) * 255 // 31)


def rgb_to_rgb565(p):
    r, g, b = p[:3]
    return ((r * 31 + 127) // 255) << 11 | ((g * 63 + 127) // 255) << 5 | ((b * 31 + 127) // 255)


# ------------------------------------------------------------------ images
def parse_images(blob):
    assert blob[:14] == b'ROCK26IMAGERES', "bad imageres magic"
    n = struct.unpack_from('<I', blob, 16)[0]
    ents = [struct.unpack_from('<4H2I', blob, 32 + IMG_ENT * i) for i in range(n)]
    end = max(e[5] + e[4] for e in ents)
    tail = blob[end:]
    names = [tail[i * IMG_NAME:(i + 1) * IMG_NAME].split(b'\0')[0].decode('latin1')
             for i in range(n)]
    return ents, names, end


def build_images(blob, new_pixels):
    """new_pixels: {index: (w, h, x, y, bytes_rgb565be)} -> rebuilt blob"""
    ents, names, end = parse_images(blob)
    first = min(e[5] for e in ents)
    head = bytearray(blob[:first])
    order = sorted(range(len(ents)), key=lambda i: ents[i][5])
    data = bytearray()
    new_ents = list(ents)
    for i in order:
        w, h, x, y, ln, off = ents[i]
        if i in new_pixels:
            w, h, x, y, px = new_pixels[i]
        else:
            px = blob[off:off + ln]
        new_ents[i] = (w, h, x, y, len(px), first + len(data))
        data += px
    for i, e in enumerate(new_ents):
        struct.pack_into('<4H2I', head, 32 + IMG_ENT * i, *e)
    return bytes(head) + bytes(data) + blob[end:]


# ------------------------------------------------------------------ strings
def parse_strings(blob):
    n = struct.unpack_from('<H', blob, 0)[0]
    offs = struct.unpack_from(f'<{n}I', blob, 2)
    langs = []
    for li, o in enumerate(offs):
        end = offs[li + 1] if li + 1 < n else len(blob)
        count = (end - o) // STR_REC
        recs = []
        for k in range(count):
            rec = blob[o + k * STR_REC:o + (k + 1) * STR_REC]
            txt = rec[58:].decode('utf-16le', 'replace').split('\0')[0]
            recs.append(txt)
        langs.append((o, recs))
    return langs


def esc(s):
    return s.replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n').replace('\r', '\\r')


def unesc(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            out.append({'t': '\t', 'n': '\n', 'r': '\r', '\\': '\\'}.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


# ------------------------------------------------------------------ ELF (for Ghidra/IDA)
def write_elf(path, sections, entry):
    """sections: list of (name, addr, bytes, is_code). ELF32 ARM, one PT_LOAD per section."""
    shstr = b'\0' + b''.join(n.encode() + b'\0' for n, *_ in sections) + b'.shstrtab\0'
    ehsz, phsz, shsz = 52, 32, 40
    nph = len(sections)
    data_off = ehsz + phsz * nph
    blobs_ = []
    off = data_off
    for name, addr, data, code in sections:
        blobs_.append(off)
        off += len(data)
    shstr_off = off
    off += len(shstr)
    off = (off + 3) & ~3
    shoff = off
    out = bytearray()
    out += b'\x7fELF\x01\x01\x01' + b'\0' * 9
    out += struct.pack('<HHIIIIIHHHHHH', 2, 40, 1, entry, ehsz, shoff, 0x05000200,
                       ehsz, phsz, nph, shsz, len(sections) + 2, len(sections) + 1)
    for (name, addr, data, code), o in zip(sections, blobs_):
        flags = 5 if code else 6
        out += struct.pack('<8I', 1, o, addr, addr, len(data), len(data), flags, 4)
    for name, addr, data, code in sections:
        out += data
    out += shstr
    out += b'\0' * (shoff - len(out))
    out += b'\0' * shsz
    noff = 1
    for (name, addr, data, code), o in zip(sections, blobs_):
        flags = 0x6 if code else 0x3          # AX / WA
        out += struct.pack('<10I', noff, 1, flags, addr, o, len(data), 0, 0, 4, 0)
        noff += len(name) + 1
    out += struct.pack('<10I', noff, 3, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0)
    open(path, 'wb').write(out)


# ------------------------------------------------------------------ commands
def cmd_info(a):
    raw = open(a.image, 'rb').read()
    hdr, ok_tag, ok_copy, ok_crc, crc = check(raw)
    print(f"file      {a.image}  ({len(raw):#x} bytes)")
    print(f"date      {hdr['date']}   vendor {hdr['vendor']!r}  model {hdr['model']!r}")
    print(f"body size {hdr['size']:#x}")
    print(f"tag       {'OK' if ok_tag else 'BAD'}   header copy {'OK' if ok_copy else 'BAD'}"
          f"   RKCRC32 {crc:#010x} {'OK' if ok_crc else 'BAD'}")
    for name, cat, i, off, size in blobs(hdr):
        print(f"  {name:10s} {off:#09x} .. {off + size:#09x}  ({size:#x})")
    o, s = hdr['stage'][3]
    base, segs = parse_stage(raw[o:o + s])
    used = [k for k, sg in enumerate(segs) if sg['code_sz'] or sg['data_sz']]
    print(f"stage3    base {base:#x}, {len(segs)} segments ({len(used)} non-empty)")


def cmd_unpack(a):
    raw = open(a.image, 'rb').read()
    hdr, ok_tag, ok_copy, ok_crc, crc = check(raw)
    d = a.outdir
    for sub in ('blobs', 'code', 'images', 'strings'):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    man = {'source': os.path.basename(a.image), 'sha256': hashlib.sha256(raw).hexdigest(),
           'size': hdr['size'], 'blobs': []}
    open(os.path.join(d, 'blobs', 'header.bin'), 'wb').write(raw[:0x200])
    for name, cat, i, off, size in blobs(hdr):
        open(os.path.join(d, 'blobs', name + '.bin'), 'wb').write(raw[off:off + size])
        man['blobs'].append({'name': name, 'cat': cat, 'index': i, 'offset': off, 'size': size})

    # ---- code
    o, s = hdr['stage'][3]
    stage = raw[o:o + s]
    base, segs = parse_stage(stage)
    tsv = ["seg\tcode_pa\tcode_sz\tdata_pa\tdata_sz\tbss_pa\tbss_sz\tcode_va\tdata_va"]
    elf_secs = []
    for k, sg in enumerate(segs):
        if not (sg['code_sz'] or sg['data_sz']):
            continue
        tsv.append(f"{k}\t{sg['code_pa']:#010x}\t{sg['code_sz']:#x}\t{sg['data_pa']:#010x}\t"
                   f"{sg['data_sz']:#x}\t{sg['bss_pa']:#010x}\t{sg['bss_sz']:#x}\t"
                   f"{sg['code_va']:#010x}\t{sg['data_va']:#010x}")
        if sg['code_sz']:
            c = stage[sg['code_va'] - base:sg['code_va'] - base + sg['code_sz']]
            open(os.path.join(d, 'code', f"seg{k:02d}_code_{sg['code_pa']:08x}.bin"), 'wb').write(c)
            elf_secs.append((f".text.seg{k:02d}", sg['code_pa'], c, True))
        if sg['data_sz']:
            c = stage[sg['data_va'] - base:sg['data_va'] - base + sg['data_sz']]
            open(os.path.join(d, 'code', f"seg{k:02d}_data_{sg['data_pa']:08x}.bin"), 'wb').write(c)
            elf_secs.append((f".data.seg{k:02d}", sg['data_pa'], c, False))
    open(os.path.join(d, 'code', 'segments.tsv'), 'w').write('\n'.join(tsv) + '\n')
    entry = struct.unpack_from('<I', stage, segs[0]['code_va'] - base + 4)[0]
    write_elf(os.path.join(d, 'code', 'firmware.elf'), elf_secs, entry)
    # a "boot view": resident segment 0 + the init overlays the bootloader preloads
    boot = [x for x in elf_secs if x[0].endswith(('seg00', 'seg01', 'seg04'))]
    write_elf(os.path.join(d, 'code', 'boot_view.elf'), boot, entry)

    # ---- images
    if hdr['imageres'] and hdr['imageres'][0][1]:
        from PIL import Image
        io_, is_ = hdr['imageres'][0]
        blob = raw[io_:io_ + is_]
        ents, names, end = parse_images(blob)
        meta = []
        for i, ((w, h, x, y, ln, off), nm) in enumerate(zip(ents, names)):
            px = struct.unpack_from(f'>{w * h}H', blob, off)
            im = Image.new('RGB', (w, h))
            im.putdata([rgb565_to_rgb(p) for p in px])
            fn = f"{i:03d}_{os.path.splitext(nm)[0]}.png".replace('/', '_')
            im.save(os.path.join(d, 'images', fn))
            meta.append({'index': i, 'file': fn, 'name': nm, 'w': w, 'h': h, 'x': x, 'y': y,
                         'sha1': hashlib.sha1(blob[off:off + ln]).hexdigest()})
        json.dump(meta, open(os.path.join(d, 'images', 'images.json'), 'w'), indent=1)

    # ---- strings
    if hdr['strtbl'] and hdr['strtbl'][0][1]:
        so, ss = hdr['strtbl'][0]
        langs = parse_strings(raw[so:so + ss])
        summary = []
        for li, (off, recs) in enumerate(langs):
            with open(os.path.join(d, 'strings', f"lang{li:02d}.tsv"), 'w', encoding='utf-8') as f:
                f.write("# index<TAB>text   (max 99 chars, \\n \\t escapes)\n")
                for k, t in enumerate(recs):
                    f.write(f"{k}\t{esc(t)}\n")
            summary.append(f"lang{li:02d}\t{recs[0] if recs else ''}\t{recs[2] if len(recs) > 2 else ''}")
        open(os.path.join(d, 'strings', 'languages.txt'), 'w', encoding='utf-8').write(
            "# file\trecord 0\trecord 2\n" + '\n'.join(summary) + '\n')

    json.dump(man, open(os.path.join(d, 'manifest.json'), 'w'), indent=1)
    print(f"unpacked {a.image} -> {d}  (crc {'OK' if ok_crc else 'BAD'})")
    print("  blobs/    raw blobs (edit fonts/gbk here if you know the format)")
    print("  code/     per-segment binaries, segments.tsv, firmware.elf / boot_view.elf for Ghidra")
    print("  images/   PNGs (+images.json); black pixels are lit on the OLED")
    print("  strings/  one TSV per language")


def cmd_pack(a):
    d = a.workdir
    man = json.load(open(os.path.join(d, 'manifest.json')))
    header = bytearray(open(os.path.join(d, 'blobs', 'header.bin'), 'rb').read())
    hdr = parse_header(bytes(header) + b'\0' * 0)
    data = {}
    for bl in man['blobs']:
        data[bl['name']] = bytearray(open(os.path.join(d, 'blobs', bl['name'] + '.bin'), 'rb').read())
    changes = []

    # ---- code: segment binaries back into stage3
    stage_name = 'stage3'
    stage = data[stage_name]
    base, segs = parse_stage(bytes(stage))
    cdir = os.path.join(d, 'code')
    for fn in sorted(os.listdir(cdir)):
        if not fn.startswith('seg') or not fn.endswith('.bin'):
            continue
        k, kind = int(fn[3:5]), fn.split('_')[1]
        sg = segs[k]
        va, sz = (sg['code_va'], sg['code_sz']) if kind == 'code' else (sg['data_va'], sg['data_sz'])
        new = open(os.path.join(cdir, fn), 'rb').read()
        if len(new) != sz:
            raise SystemExit(f"{fn}: size changed ({len(new):#x} != {sz:#x}); segments are fixed-size")
        o = va - base
        if bytes(stage[o:o + sz]) != new:
            stage[o:o + sz] = new
            changes.append(f"code {fn}")

    # ---- images
    idir = os.path.join(d, 'images')
    if os.path.exists(os.path.join(idir, 'images.json')):
        from PIL import Image
        meta = json.load(open(os.path.join(idir, 'images.json')))
        iname = 'imageres0'
        blob = bytes(data[iname])
        ents, names, end = parse_images(blob)
        newpx = {}
        for m in meta:
            p = os.path.join(idir, m['file'])
            if not os.path.exists(p):
                continue
            im = Image.open(p).convert('RGB')
            w, h = im.size
            px = struct.pack(f'>{w * h}H', *[rgb_to_rgb565(q) for q in (im.get_flattened_data() if hasattr(im, 'get_flattened_data') else im.getdata())])
            if hashlib.sha1(px).hexdigest() == m['sha1'] and (w, h) == (m['w'], m['h']):
                continue
            if (w, h) != (m['w'], m['h']) and not a.allow_resize:
                raise SystemExit(f"{m['file']}: size {w}x{h} != original {m['w']}x{m['h']} "
                                 f"(use --allow-resize)")
            newpx[m['index']] = (w, h, m['x'], m['y'], px)
            changes.append(f"image {m['file']}")
        if newpx:
            data[iname] = bytearray(build_images(blob, newpx))

    # ---- strings
    sdir = os.path.join(d, 'strings')
    if 'strtbl0' in data and os.path.isdir(sdir):
        blob = data['strtbl0']
        langs = parse_strings(bytes(blob))
        for li, (off, recs) in enumerate(langs):
            p = os.path.join(sdir, f"lang{li:02d}.tsv")
            if not os.path.exists(p):
                continue
            for line in open(p, encoding='utf-8'):
                line = line.rstrip('\n')
                if not line or line.startswith('#'):
                    continue
                k, _, txt = line.partition('\t')
                k = int(k)
                txt = unesc(txt)
                if txt == recs[k]:
                    continue
                if len(txt) > STR_MAXCH:
                    raise SystemExit(f"lang{li:02d}.tsv #{k}: more than {STR_MAXCH} characters")
                ro = off + k * STR_REC + 58
                enc = txt.encode('utf-16le')
                blob[ro:ro + 200] = enc + b'\0' * (200 - len(enc))
                changes.append(f"string lang{li:02d}#{k}")

    # ---- assemble (blobs keep their order; later blobs move if one grows)
    order = sorted(man['blobs'], key=lambda b: b['offset'])
    body = bytearray(header)
    pos = 0x200
    for bl in order:
        if bl['offset'] != pos and bl['offset'] > pos:
            body += b'\0' * (bl['offset'] - pos)      # keep original gaps
            pos = bl['offset']
        newoff = len(body)
        body += data[bl['name']]
        cat, i = bl['cat'], bl['index']
        slot = hdr[cat + '_slot_off'] + 8 * i
        struct.pack_into('<II', body, slot, newoff, len(data[bl['name']]))
        pos = len(body)
    out = finalize(bytes(body))
    open(a.out, 'wb').write(out)
    print(f"packed {len(changes)} change(s) -> {a.out} ({len(out):#x} bytes)")
    for c in changes[:50]:
        print("  ", c)
    if len(changes) > 50:
        print(f"   ... and {len(changes) - 50} more")
    if len(out) != man.get('orig_len', len(out)):
        pass


def cmd_patch(a):
    raw = bytearray(open(a.image, 'rb').read())
    hdr = parse_header(bytes(raw))
    o, s = hdr['stage'][3]
    base, segs = parse_stage(bytes(raw[o:o + s]))
    for p in a.patches:
        where, _, val = p.partition('=')
        seg, _, addr = where.partition(':')
        seg, addr = int(seg, 0), int(addr, 0)
        if val.startswith('"') or not all(c in '0123456789abcdefABCDEF' for c in val):
            from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
            enc, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(val.strip('"'), addr)
            new = bytes(enc)
        else:
            new = bytes.fromhex(val)
        sg = segs[seg]
        for pa, va, sz in ((sg['code_pa'], sg['code_va'], sg['code_sz']),
                           (sg['data_pa'], sg['data_va'], sg['data_sz'])):
            if pa <= addr and addr + len(new) <= pa + sz:
                fo = o + (va - base) + (addr - pa)
                print(f"seg{seg} {addr:#010x}: {raw[fo:fo + len(new)].hex()} -> {new.hex()}")
                raw[fo:fo + len(new)] = new
                break
        else:
            raise SystemExit(f"{p}: address not inside segment {seg}")
    size = hdr['size']
    out = finalize(bytes(raw[:size]))
    open(a.out, 'wb').write(out)
    print(f"wrote {a.out}")


def cmd_fixcrc(a):
    raw = open(a.image, 'rb').read()
    hdr = parse_header(raw)
    out = finalize(raw[:hdr['size']])
    open(a.out or a.image, 'wb').write(out)
    print(f"CRC {struct.unpack('<I', out[-4:])[0]:#010x} -> {a.out or a.image}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    p = sp.add_parser('info'); p.add_argument('image'); p.set_defaults(fn=cmd_info)
    p = sp.add_parser('unpack'); p.add_argument('image'); p.add_argument('outdir'); p.set_defaults(fn=cmd_unpack)
    p = sp.add_parser('pack'); p.add_argument('workdir'); p.add_argument('out')
    p.add_argument('--allow-resize', action='store_true', help='allow images to change size')
    p.set_defaults(fn=cmd_pack)
    p = sp.add_parser('patch', help='SEG:ADDR=HEX or SEG:ADDR="thumb asm" (needs keystone)')
    p.add_argument('image'); p.add_argument('out'); p.add_argument('patches', nargs='+')
    p.set_defaults(fn=cmd_patch)
    p = sp.add_parser('fixcrc'); p.add_argument('image'); p.add_argument('out', nargs='?')
    p.set_defaults(fn=cmd_fixcrc)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
