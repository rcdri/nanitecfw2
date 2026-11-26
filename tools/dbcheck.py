#!/usr/bin/env python3
"""dbcheck: read the ECHO NANO media library database and check that every album
in the Album and Artist views opens the tracks it should.

  python3 dbcheck.py echodb.gz [TEXT]           a dump made by `echofix.py dumpdb`
  python3 dbcheck.py --nand NAND.IMG [TEXT]     an emulator NAND image (tests)

TEXT (optional): also list every album (Album view and Artist view) whose name or
one of whose song titles contains TEXT, with the songs it opens.

Database layout (V2.0.0, offsets in 512-byte sectors from the start of "sub 1",
the raw area after the system area):
  0               header; u16 at +0x2e songs, +0x30 All-songs entries, +0x32 artists,
                  +0x34 albums, +0x36 genres
  0x1800          song records, 0x800 bytes each: +0x14 sort text (disc, track, name),
                  +0x100 navilib album key, +0x300 file name, +0x400 title,
                  +0x500 artist, +0x600 album, +0x700 genre
  0x1800+0x52000  index lists: u16 record numbers (16 KB each) and group tables of
                  8-byte entries (u16 start, u16 first child / start, u16 children /
                  count, u16 count). Album view: songs 0x52140, albums 0x52160.
                  Artist view: songs 0x52020, artists 0x52040, albums 0x520c0.
"""
import gzip, struct, sys
from collections import Counter

BASE = 0x1800
LISTS = {'all': 0x52000, 'ar_songs': 0x52020, 'ar_artists': 0x52040, 'ar_albums': 0x520c0,
         'al_songs': 0x52140, 'al_albums': 0x52160}
IDX_SPAN = (0x52000, 0x52380)


class Dump:
    """sector -> bytes for the ranges a dump holds"""

    def __init__(self):
        self.chunks = []

    @classmethod
    def from_file(cls, path):
        d = cls()
        with gzip.open(path, 'rb') as f:
            if f.read(8) != b'ECHODB1\n':
                raise SystemExit(f'{path}: not an echofix dumpdb file')
            while True:
                off, n = struct.unpack('<II', f.read(8))
                if n == 0:
                    break
                d.chunks.append((off, f.read(n * 512)))
        return d

    @classmethod
    def from_nand(cls, path, sub1_file_sector=0x2000 + 0x10000):
        d = cls()
        f = open(path, 'rb')
        def grab(off, n):
            f.seek((sub1_file_sector + off) * 512)
            d.chunks.append((off, f.read(n * 512)))
        grab(0, 0x20)
        count = struct.unpack_from('<H', d.read(0x2e // 512 * 0, 0x40), 0x2e)[0]
        grab(BASE, count * 4)
        grab(BASE + IDX_SPAN[0], IDX_SPAN[1] - IDX_SPAN[0])
        return d

    def read(self, sector, nbytes, byte_off=0):
        for off, data in self.chunks:
            a = (sector - off) * 512 + byte_off
            if 0 <= a and a + nbytes <= len(data):
                return data[a:a + nbytes]
        raise KeyError(f'sector {sector:#x} not in dump')


def s16(b):
    out = []
    for i in range(0, len(b) - 1, 2):
        c = b[i] | b[i + 1] << 8
        if c == 0:
            break
        out.append(chr(c) if c >= 0x20 else f'\\x{c:02x}')
    return ''.join(out)


def records(d, count):
    recs = []
    for i in range(count):
        r = d.read(BASE + 4 * i, 0x800)
        recs.append(dict(i=i, sort=s16(r[0x14:0x2c] + b'\0\0'), key=s16(r[0x100:0x200]),
                         file=s16(r[0x300:0x400]), title=s16(r[0x400:0x500]),
                         artist=s16(r[0x500:0x600]), album=s16(r[0x600:0x700]),
                         genre=s16(r[0x700:0x800]), marker=r[0x2c:0x34]))
    return recs


def u16s(d, sector, n):
    return list(struct.unpack(f'<{n}H', d.read(BASE + sector, 2 * n))) if n else []


def groups(d, sector, n):
    raw = d.read(BASE + sector, 8 * n) if n else b''
    return [struct.unpack_from('<4H', raw, 8 * k) for k in range(n)]


def main():
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    d = Dump.from_nand(a[0]) if '--nand' in sys.argv else Dump.from_file(a[0])
    text = a[1].lower() if len(a) > 1 else None
    h = d.read(0, 0x40)
    count, n_all, n_artists, n_albums, n_genres = struct.unpack_from('<5H', h, 0x2e)
    print(f'songs {count}, All-songs entries {n_all}, artists {n_artists}, albums {n_albums}, genres {n_genres}')
    recs = records(d, count)
    stock = all(not r['key'] for r in recs)           # stock firmware: albums by name only
    gkey = (lambda r: r['album']) if stock else (lambda r: r['key'])
    if stock:
        print('no album keys: database made by stock firmware (albums grouped by name)')
    bad_marker = [r['i'] for r in recs if r['marker'] not in (bytes.fromhex('0046004900 4c0045'.replace(' ', '')),)]
    if bad_marker:
        print(f'!! {len(bad_marker)} records without the FILE marker, e.g. {bad_marker[:10]}')
    nokey = [] if stock else [r for r in recs if not r['key'].startswith(r['album'] + '\\x01')]
    print(f'records whose album key does not start with their album name: {len(nokey)}')
    for r in nokey[:15]:
        print(f"   #{r['i']}: key {r['key']!r}  album {r['album']!r}  file {r['file']!r}")

    problems = 0
    # ---------------------------------------------------------------- Album view
    songs = u16s(d, LISTS['al_songs'], count)
    if sorted(songs) != list(range(count)):
        print('!! Album view song list is not a permutation of the records')
        problems += 1
    alb = groups(d, LISTS['al_albums'], n_albums + 1)
    pos = 0
    for k, (start, s2, n, n2) in enumerate(alb[:n_albums]):
        part = [recs[songs[p]] for p in range(start, min(start + n, count))]
        why = []
        if start != pos:
            why.append(f'starts at {start}, previous albums end at {pos}')
        if len({gkey(r) for r in part}) > 1:
            why.append('mixed albums: ' + ', '.join(f'{x!r} x{c}' for x, c in Counter(gkey(r) for r in part).items()))
        if why:
            problems += 1
            if problems <= 30:
                print(f"!! album #{k} {part[0]['album'] if part else '?'!r}: " + '; '.join(why))
        pos = start + n
    if pos != count:
        print(f'!! Album view groups cover {pos} songs, not {count}')
        problems += 1
    keys = Counter(gkey(r) for r in recs)
    print(f'Album view: {n_albums} albums, {len(keys)} distinct albums in the records')

    # ---------------------------------------------------------------- Artist view
    asongs = u16s(d, LISTS['ar_songs'], count)
    arts = groups(d, LISTS['ar_artists'], n_artists + 1)
    n_aral = sum(g[2] for g in arts[:n_artists])
    aral = groups(d, LISTS['ar_albums'], n_aral + 1)
    for k, (start, child, nch, n) in enumerate(arts[:n_artists]):
        sub = aral[child:child + nch]
        p = start
        for j, (s, _, c, _) in enumerate(sub):
            part = [recs[asongs[q]] for q in range(s, min(s + c, count))]
            why = []
            if s != p:
                why.append(f'starts at {s}, expected {p}')
            if len({gkey(r) for r in part}) > 1:
                why.append('mixed albums: ' + ', '.join(f'{x!r} x{c}' for x, c in Counter(gkey(r) for r in part).items()))
            if len({r['artist'] for r in part}) > 1:
                why.append('mixed artists')
            if why:
                problems += 1
                if problems <= 30:
                    print(f"!! artist #{k} {recs[asongs[start]]['artist']!r}, album {j} "
                          f"{part[0]['album'] if part else '?'!r}: " + '; '.join(why))
            p = s + c
        if p != start + n:
            problems += 1
            if problems <= 30:
                print(f"!! artist #{k}: its albums cover {p - start} songs, the artist has {n}")
    print(f'Artist view: {n_artists} artists, {n_aral} artist albums')
    print('problems:', problems)

    if text:
        def show(label, part):
            print(f"\n{label}: {part[0]['album']!r} ({len(part)} songs)")
            for r in part:
                print(f"   {r['sort'][:8]}  {r['title']!r}  [album {r['album']!r}, artist {r['artist']!r}]  "
                      f"{r['file']!r}")
        hit = lambda part: part and (text in part[0]['album'].lower() or any(text in r['title'].lower() for r in part))
        for k, (start, _, n, _) in enumerate(alb[:n_albums]):
            part = [recs[songs[p]] for p in range(start, min(start + n, count))]
            if hit(part):
                show(f'Album view, album #{k}', part)
        for k, (start, child, nch, n) in enumerate(arts[:n_artists]):
            for j, (s, _, c, _) in enumerate(aral[child:child + nch]):
                part = [recs[asongs[q]] for q in range(s, min(s + c, count))]
                if hit(part):
                    show(f"Artist view, artist #{k} {recs[asongs[start]]['artist']!r}, album {j + 1}", part)

if __name__ == '__main__':
    main()
