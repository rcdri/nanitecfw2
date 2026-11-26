#!/usr/bin/env python3
"""turbo: speed-ups for SNOWSKY ECHO NANO V2.0.0.

  python3 mods/turbo.py IN.IMG OUT.IMG [--rebuild]

IN.IMG: stock V2.0.0 or the output of navilib.py / nanitec.py (no overlap).

1. Library refresh sector cache (mods/turbo/cache.c). During "Update media
   library" single-sector reads go through a 28-entry LRU cache in unused RAM;
   both card-write paths invalidate overlapping entries, and the cache is only
   active while the refresh window is on screen. Measured in the emulator on a
   24-song test library: 5365 -> ~950 device reads (SD card -89 %), with a
   byte-identical database. On hardware every avoided read is a full SD/eMMC
   command round trip, which is what the stock refresh spends its time on.

2. Boot logo animation: 36 frames at 10 ms instead of 25 ms (-0.54 s).

The cache code is prebuilt below; --rebuild recompiles mods/turbo/cache.c
with clang + ld.lld (thumbv7m) first.
"""
import os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from rkfw import parse_header                  # noqa: E402
import fwtool                                  # noqa: E402
from nanitec import apply_sites, asm, H        # noqa: E402

CAVE = 0x030512BC            # unreferenced "All LRC Time Tags" debug dump, ..0x03051550
CAVE_END = 0x03051550
HI = 0x030518F0              # dead code after navilib/nanitec helpers, ..0x0305199A
HI_END = 0x0305199A

# filled by --rebuild
CACHE_BIN = (bytes.fromhex(
    '80b56f4682b09c4613460a460021cdf800c000f002f802b080bdf0b503af2de9000f81b0d7f808a09346012b61d14cf2'
    '000cc0f2093cdcf8002044f23026c5f25246b24255d1dcf80420002a51d044f67422c0f2003212780d2a4ad100f07f09'
    '0cf118030cf500724ff0ff344ff00008002500bf5e794eb153f8046c5e4502d11e79b1453ed01e6801e000bf0026a642'
    '3cbf3446a846013502f500721c2d03f10c03e7d15a460123cdf800a00124664600f09cf8316901f101013161a8bb08eb'
    '480006eb800506eb482047f6bb1300f50070c0f2053351464ff400729847b068c5f814b0013085f81c90b060a8616c77'
    '1ae05a46c7f808a001b0bde8000fbde8f04073e0dce902010130ccf808001860481c47f6bb13ccf80c00c0f205335046'
    '11464ff400729847002001b0bde8000ff0bd80b56f4682b09c4613460a4601468020cdf800c0fff768ff02b080bdf0b5'
    '03af4df8048d984615460e46044600f00af8204631462a4643465df8048bbde8f04000f05fbab0b502af4cf2000ec0f2'
    '093edef8003044f23024c5f25244a34218bfb0bd4ff0000c002300bf0eeb0304657f4db1257f854206d1143425686d1a'
    '954238bf84f809c00c33b3f5a87fedd1b0bdf0b503af4df8048d04460020984615460e46fff7cfff204631462a464346'
    '5df8048bbde8f04000f02cba9c46009b802804d108461146624600f05fba1146624600f054ba'), bytes.fromhex(
    '70b50d46dff804c060470000c930060370b50025dff804c0604700001931060380b56f464cf2000e44f23023c0f2093e'
    'c5f252434ff0010c0022cee9003c0023cee90222cef810200eeb03010c33b3f5a87f4a77f8d14af2f551c0f20731bde8'
    '804008474cf204014af2f553c0f209310022c0f207330a60184770b50d46dff804c060479b15060370b50025dff804c0'
    '60470000ef160603'))
SYMS = {'cache_begin': 50665744, 'cache_end': 50665812, 'cache_read': 50664124, 'cache_read0': 50664430, 'cache_write': 50664458, 'cache_write0': 50664574, 'cached': 50664150}


def rebuild():
    d = os.path.join(HERE, 'turbo')
    with tempfile.TemporaryDirectory() as t:
        o, elf, binf = (os.path.join(t, x) for x in ('cache.o', 'cache.elf', 'cache.bin'))
        subprocess.check_call(['clang', '--target=thumbv7m-none-eabi', '-mcpu=cortex-m3', '-mthumb',
                               '-Os', '-ffreestanding', '-fno-builtin', '-nostdlib', '-Wall', '-Wextra',
                               '-Werror', '-c', os.path.join(d, 'cache.c'), '-o', o])
        subprocess.check_call(['ld.lld', '-T', os.path.join(d, 'link.ld'), o, '-o', elf],
                              stderr=subprocess.DEVNULL)
        parts = []
        for sec in ('.text', '.hi'):
            subprocess.check_call(['llvm-objcopy', '-O', 'binary', '-j', sec, elf, binf])
            parts.append(open(binf, 'rb').read())
        nm = subprocess.check_output(['llvm-nm', elf]).decode()
        syms = {n: int(v, 16) for v, _, n in (l.split() for l in nm.splitlines())}
        return parts, syms


def sites(blobs, syms):
    blob, hi = blobs
    assert CAVE + len(blob) <= CAVE_END and HI + len(hi) <= HI_END
    br = lambda op, tgt, at: asm(f'{op} {tgt:#x}', at)
    def B(key, op, tgt, at):
        x = br(op, tgt, at)
        return x if x is not None else bytes.fromhex(BRANCHES[key])
    return [
        (0, CAVE, H('2de9f04f dff8ec91'), None),                      # cave still stock
        (0, CAVE, None, blob),
        (0, HI, H('bde8f84f'), hi),                                   # stock dead code there too
        (0, 0x03061596, H('70b5 0d46'), B('r', 'b.w', syms['cache_read'], 0x03061596)),
        (0, 0x030616EA, H('70b5 0025'), B('r0', 'b.w', syms['cache_read0'], 0x030616EA)),
        (0, 0x030630C4, H('70b5 0d46'), B('w', 'b.w', syms['cache_write'], 0x030630C4)),
        (0, 0x03063114, H('70b5 0025'), B('w0', 'b.w', syms['cache_write0'], 0x03063114)),
        (5, 0x0307A65E, H('fff7c9ff'), B('b', 'bl', syms['cache_begin'], 0x0307A65E)),   # progress(0)
        (5, 0x0307A6BA, H('fff79bff'), B('e', 'bl', syms['cache_end'], 0x0307A6BA)),     # progress(3)
        (0, 0x03050450, H('1920'), H('0a20')),                       # boot logo: 25 -> 10 ms/frame
    ]


BRANCHES = {'r': 'eff791be', 'r0': 'eff780be', 'w': 'eef7a1b9', 'w0': 'eef7b3b9', 'b': 'd7f757f9', 'e': 'd7f74bf9'}


def apply(raw, rebuild_code=False):
    raw = bytearray(raw)
    if rebuild_code:
        blob, syms = rebuild()
    else:
        blob, syms = CACHE_BIN, SYMS
    apply_sites(raw, sites(blob, syms))
    size = parse_header(bytes(raw))['size']
    return fwtool.finalize(bytes(raw[:size]))


if __name__ == '__main__':
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    if '--print' in sys.argv:
        blob, syms = rebuild()
        br = {}
        for k, fn, at, op in (('r', 'cache_read', 0x03061596, 'b.w'), ('r0', 'cache_read0', 0x030616EA, 'b.w'), ('w', 'cache_write', 0x030630C4, 'b.w'),
                              ('w0', 'cache_write0', 0x03063114, 'b.w'), ('b', 'cache_begin', 0x0307A65E, 'bl'),
                              ('e', 'cache_end', 0x0307A6BA, 'bl')):
            br[k] = asm(f'{op} {syms[fn]:#x}', at).hex()
        print(blob[0].hex() + ' ' + blob[1].hex()); print(repr({k: v for k, v in syms.items() if k.startswith('cache')})); print(repr(br))
        raise SystemExit
    if len(a) != 2:
        raise SystemExit(__doc__)
    out = apply(open(a[0], 'rb').read(), '--rebuild' in sys.argv)
    open(a[1], 'wb').write(out)
    print(f"wrote {a[1]}  (turbo: refresh sector cache, faster boot logo)")
