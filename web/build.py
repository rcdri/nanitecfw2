#!/usr/bin/env python3
"""Build the modded image in one process. The web patcher (index.html) runs this in the
browser with Pyodide; it gives the same bytes as build.sh.

  python3 web/build.py NANOV200.IMG [OUT_DIR]      (default OUT_DIR: out)
"""
import hashlib, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
sys.path[:0] = [ROOT, os.path.join(ROOT, 'mods')]
import fwtool                                           # noqa: E402
import navilib, nanitec, turbo, opus                    # noqa: E402

STOCK_SHA256 = 'e60ddde7ceae9c83f95db7710f4633a31cf72295d4821128274718dec16ff993'
DATE = '20261031'      # see "Install" in README.md


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def build(raw, date=DATE, progress=print):
    """stock image -> navilib + nanitec + turbo + opus, like build.sh"""
    steps = [('navilib', navilib.apply), ('nanitec', nanitec.apply), ('turbo', turbo.apply),
             ('opus', lambda r: opus.apply(r, False, date))]
    real = fwtool.rkcrc32
    try:
        # every step re-finalizes the image, but only the last checksum is kept (each step
        # cuts its input at the header's size): skip the others, they're slow in a browser
        fwtool.rkcrc32 = lambda data, crc=0: 0
        for i, (name, fn) in enumerate(steps):
            if i == len(steps) - 1:
                fwtool.rkcrc32 = real
            progress(name)
            raw = fn(raw)
    finally:
        fwtool.rkcrc32 = real
    return raw


def recovery(raw, date=DATE):
    """unmodified V2.0.0 code, dated like the modded image (README.md, "Recovery")"""
    return nanitec.recovery(raw, date)


def check_stock(raw):
    h = sha256(raw)
    if h != STOCK_SHA256:
        raise SystemExit(f'not the official V2.0.0 NANOV200.IMG (SHA-256 {h}, expected {STOCK_SHA256})')


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    out = argv[1] if len(argv) > 1 else 'out'
    raw = open(argv[0], 'rb').read()
    check_stock(raw)
    os.makedirs(out, exist_ok=True)
    for name, img in (('NANOV200_mod.IMG', build(raw, progress=lambda s: print('applying', s))),
                      ('NANOV200_recovery.IMG', recovery(raw))):
        p = os.path.join(out, name)
        open(p, 'wb').write(img)
        print(f'{sha256(img)}  {p}')


if __name__ == '__main__':
    main(sys.argv[1:])
