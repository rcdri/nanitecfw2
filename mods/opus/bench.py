#!/usr/bin/env python3
"""Run the Opus codec blob (build/opus.bin) on its own in Unicorn, the way the
codec core's runtime drives it, and report the Cortex-M3 instruction count
per second of audio.

    python3 bench.py file.opus [ref.raw] [--bin build/opus.bin] [--seek MS]

ref.raw (optional) is the reference decode (s16le stereo 48 kHz, e.g. from
`ffmpeg -i file.opus -f s16le -ac 2 ref.raw`); the output is compared with it.
"""
import argparse, struct, sys
import numpy as np
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE, UC_HOOK_BLOCK
from unicorn.arm_const import *
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

ENTRY = 0xF5D8
FIO, F_HANDLE = 0x010220E4, 0x01022108
OUT_STATE, OUT_BUFS, OUT_OFF = 0x01029888, 0x01029898, 0x01022104
BUF0, BUF1 = 0x01040000, 0x01041000
STUB = 0x0001FF00            # fake FIO functions live here (+0 read, +4 seek, +8 tell, +12 length)
RET = 0x0001FFF0             # return trap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file')
    ap.add_argument('ref', nargs='?')
    ap.add_argument('--bin', default=__file__.rsplit('/', 1)[0] + '/build/opus.bin')
    ap.add_argument('--seek', type=int)
    ap.add_argument('--out')
    ap.add_argument('--prof', action='store_true', help='per-function breakdown (needs opus.elf next to the .bin)')
    a = ap.parse_args()
    data = open(a.file, 'rb').read()
    blob = open(a.bin, 'rb').read()

    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    uc.mem_map(0, 0x20000)
    uc.mem_map(0x01000000, 0x80000)
    uc.mem_write(ENTRY, blob)
    for i in range(4):                                    # bx lr
        uc.mem_write(STUB + 4 * i, b'\x70\x47\x00\xbf')
    uc.mem_write(RET, b'\xfe\xe7')                         # b .
    fio = [0, STUB | 1, (STUB + 4) | 1, (STUB + 8) | 1, 0, (STUB + 12) | 1]
    uc.mem_write(FIO, struct.pack('<6I', *fio))
    uc.mem_write(F_HANDLE, struct.pack('<I', 7))
    uc.mem_write(OUT_BUFS, struct.pack('<2I', BUF0, BUF1))
    st = {'pos': 0}

    def stub(uc, addr, size, _):
        r0, r1, r2 = (uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
        k = (addr - STUB) // 4
        if k == 0:                                         # Read(buf, size, h)
            chunk = data[st['pos']:st['pos'] + r1]
            uc.mem_write(r0, chunk); st['pos'] += len(chunk); ret = len(chunk)
        elif k == 1:                                       # Seek(off, whence, h)
            st['pos'] = [r0, st['pos'] + r0, len(data) + r0][r1]; ret = 0
        elif k == 2:
            ret = st['pos']
        else:
            ret = len(data)
        uc.reg_write(UC_ARM_REG_R0, ret & 0xffffffff)
    uc.hook_add(UC_HOOK_CODE, stub, begin=STUB, end=STUB + 15)

    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    cache, cnt, per = {}, [0], {}

    def blk(uc, addr, size, _):
        n = cache.get(addr)
        if n is None:
            n = cache[addr] = sum(1 for _ in md.disasm_lite(bytes(uc.mem_read(addr, size)), addr))
        cnt[0] += n
        if a.prof:
            per[addr] = per.get(addr, 0) + n
    uc.hook_add(UC_HOOK_BLOCK, blk)

    def call(cmd, p1=0, p2=0, p3=0):
        for r, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), (cmd, p1, p2, p3)):
            uc.reg_write(r, v)
        uc.reg_write(UC_ARM_REG_SP, 0x0107F000)
        uc.reg_write(UC_ARM_REG_LR, RET | 1)
        uc.emu_start(ENTRY | 1, RET, count=200_000_000)
        return uc.reg_read(UC_ARM_REG_R0)

    def u32(addr):
        return struct.unpack('<I', uc.mem_read(addr, 4))[0]

    c0 = cnt[0]
    assert call(8) == 1, 'open failed'
    c_open = cnt[0] - c0
    info = {}
    for k, name in ((3, 'bitrate'), (4, 'rate'), (5, 'channels'), (6, 'length_ms')):
        call(k, 0x0107F800); info[name] = u32(0x0107F800)
    if a.seek is not None:
        call(14, a.seek)
    out, ncalls, c0, worst = [], 0, cnt[0], 0
    while True:
        idx = u32(OUT_STATE + 4)
        c1 = cnt[0]
        if call(12) != 1:
            break
        worst = max(worst, cnt[0] - c1)
        ncalls += 1
        out.append(bytes(uc.mem_read([BUF0, BUF1][idx], 2048)))
        uc.mem_write(OUT_STATE + 4, struct.pack('<I', idx ^ 1))    # what cmd 10 does
    pcm = np.frombuffer(b''.join(out), dtype='<i2').reshape(-1, 2)
    secs = ncalls * 512 / 48000
    print(f"open: {c_open/1e6:.2f} M insns   info: {info}")
    print(f"decode: {ncalls} calls, {secs:.2f} s of audio, {cnt[0]-c0:,} insns "
          f"= {(cnt[0]-c0)/secs/1e6:.1f} MIPS avg, worst call {worst:,} insns "
          f"({worst/(512/48000)/1e6:.1f} MIPS over one 512-frame period)")
    if a.out:
        pcm.tofile(a.out)
    if a.prof:
        import bisect, collections, subprocess
        syms = []
        for l in subprocess.run(['llvm-nm', '-n', '--print-size', a.bin[:-4] + '.elf'],
                                capture_output=True, text=True).stdout.splitlines():
            p = l.split()
            if len(p) == 4 and p[2] in 'tT':
                syms.append((int(p[0], 16), p[3]))
        addrs = [x[0] for x in syms]
        agg = collections.Counter()
        for ad, n in per.items():
            i = bisect.bisect_right(addrs, ad) - 1
            agg[syms[i][1] if i >= 0 else hex(ad)] += n
        tot = sum(agg.values())
        for name, n in agg.most_common(20):
            print(f"  {name:30s} {n/secs/1e6:6.2f} MIPS {100*n/tot:5.1f}%")
    if a.ref:
        ref = np.fromfile(a.ref, dtype='<i2').reshape(-1, 2).astype(np.float64)
        if a.seek is not None:
            ref = ref[a.seek * 48:]
        n = min(len(ref), len(pcm))
        d = pcm[:n].astype(np.float64) - ref[:n]
        snr = 10 * np.log10((ref[:n] ** 2).sum() / max((d ** 2).sum(), 1e-9))
        print(f"vs ref: {len(pcm)} frames (ref {len(ref)}), SNR {snr:.1f} dB, max diff {np.abs(d).max():.0f}")


if __name__ == '__main__':
    main()
