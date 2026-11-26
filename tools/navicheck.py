#!/usr/bin/env python3
"""navicheck: predict how a folder will appear in the ECHO NANO media library
with the navilib mod, and flag tagging problems.  Needs: pip install mutagen

  python3 tools/navicheck.py /path/to/Artist            # album list the player will show
  python3 tools/navicheck.py /path/to/Artist --dump tags.json
                                                        # tags only (no audio), for emulator tests
  python3 tools/navicheck.py --rebuild tags.json OUTDIR # silent FLACs with the same names and tags

Rules (from the firmware, see mods/README.md):
  album key  = ALBUM + ALBUMARTIST ("ALBUM ARTIST"/"ALBUM_ARTIST" too), else ARTIST, cut
               at the first ';' ("A; B" -> "A"), first 126 characters; one Album-list
               entry per key, shown by ALBUM name
  order      = DISCNUMBER, TRACKNUMBER, then the first 4 characters of the file name
  title      = TITLE, else the file name
  .cue       = ignored (every audio file is one song), unless Settings > CUE sheets = Stock
FLAC/Ogg (Vorbis comments) are what navilib was verified with; other formats are
reported with the same rules but marked "unverified".
"""
import json, os, re, sys, subprocess, unicodedata
from collections import defaultdict

AUDIO = ('.flac', '.ogg', '.oga', '.opus', '.mp3', '.m4a', '.aac', '.wav', '.ape', '.wma', '.dsf', '.dff', '.aif', '.aiff')
VORBIS = ('.flac', '.ogg', '.oga')
LIMIT = 8192


def first(tags, *keys):
    for k in keys:
        for tk, v in tags.items():
            if tk.upper() == k:
                v = v[0] if isinstance(v, list) else v
                if str(v).strip():
                    return str(v)
    return ''


def read_tags(path):
    from mutagen import File
    f = File(path, easy=True)
    out = {}
    if f is None or f.tags is None:
        return out
    for k, v in f.tags.items():
        out[k.upper()] = [str(x) for x in (v if isinstance(v, list) else [v])]
    # mutagen easy ID3/MP4 call it "albumartist"; raw Vorbis spellings stay as-is
    return out


def num(s):
    m = re.match(r'\s*(\d+)', s or '')
    return int(m.group(1)) if m else 0


def primary(name):
    """what navilib keeps of "A; B" (its cut looks at the first 127 characters)"""
    i = name.find(';', 0, 127)
    return name[:i].rstrip(' ') if i > 0 else name


def entry(rel, tags):
    ext = os.path.splitext(rel)[1].lower()
    album = first(tags, 'ALBUM')
    aa = first(tags, 'ALBUMARTIST', 'ALBUM ARTIST', 'ALBUM_ARTIST')
    artist = first(tags, 'ARTIST')
    return {
        'path': rel, 'ext': ext, 'album': album, 'albumartist': aa, 'artist': artist,
        'group_artist': aa or artist, 'title': first(tags, 'TITLE') or os.path.splitext(os.path.basename(rel))[0],
        'disc': num(first(tags, 'DISCNUMBER')), 'track': num(first(tags, 'TRACKNUMBER')),
        'has_title': bool(first(tags, 'TITLE')), 'has_aa': bool(aa),
        'key': (album + '\x01' + primary(aa or artist))[:126],
        'compilation': first(tags, 'COMPILATION'),
    }


def scan(root):
    songs = []
    for d, _, files in os.walk(root):
        for n in sorted(files):
            if n.lower().endswith(AUDIO) and not n.startswith('._'):
                p = os.path.join(d, n)
                try:
                    tags = read_tags(p)
                except Exception as e:      # unreadable file: the player skips it too
                    tags = {'_error': [str(e)]}
                songs.append((os.path.relpath(p, root), tags))
    return songs


def report(songs):
    E = [entry(r, t) for r, t in songs]
    groups = defaultdict(list)
    for e in E:
        groups[e['key']].append(e)
    by_name = defaultdict(list)
    for k in groups:
        by_name[k.split('\x01')[0]].append(k)
    print(f"{len(E)} tracks -> {len(groups)} entries in the Album list\n")
    for k in sorted(groups, key=lambda k: (unicodedata.normalize('NFKC', k).casefold())):
        g = sorted(groups[k], key=lambda e: (e['disc'], e['track'], os.path.basename(e['path'])[:4]))
        name, who = k.split('\x01')
        dup = f"   (one of {len(by_name[name])} albums named this)" if len(by_name[name]) > 1 else ''
        print(f"[{name or '(no ALBUM tag)'}]  by {who or '(no artist)'}  - {len(g)} tracks{dup}")
        for e in g:
            flag = '' if e['ext'] in VORBIS else '  [unverified format]'
            dt = f"{e['disc']}-{e['track']:02d}" if e['disc'] else f"{e['track']:02d}"
            print(f"    {dt:>5}  {e['title']}{'' if e['has_title'] else '   <- no TITLE, file name shown'}{flag}")
        folders = {os.path.dirname(e['path']).split(os.sep)[0] for e in g}
        artists = {primary(e['artist']) for e in g}
        if not g[0]['has_aa'] and len(artists) > 1:
            print("    !! track artists differ and there is no ALBUMARTIST: this album splits into", len(artists), "entries")
        if len(folders) > 1 and len({os.path.dirname(e['path']) for e in g}) > 1 and not any(
                re.search(r'(?i)(cd|disc|disk)\s*\d', e['path']) for e in g):
            print("    !! tracks come from different folders:", ', '.join(sorted({os.path.dirname(e['path']) for e in g})))
        tn = [(e['disc'], e['track']) for e in g]
        if any(t == 0 for _, t in tn):
            print("    !! some tracks have no TRACKNUMBER (they sort first, by file name)")
        if len(set(tn)) < len(tn):
            print("    !! duplicate disc/track numbers (order falls back to file name)")
        if len(k) >= 126:
            print("    !! album + album artist is longer than 126 characters (key is cut)")
        if any(e['compilation'] for e in g) and not g[0]['has_aa']:
            print("    !! COMPILATION is set but the player ignores it: set ALBUMARTIST (e.g. Various Artists)")
        print()
    if len(E) > LIMIT:
        print(f"!! {len(E)} tracks: the player stops scanning at {LIMIT}; the rest are skipped")


def rebuild(spec, out):
    from mutagen.flac import FLAC
    tmpl = os.path.join(out, '.silence.flac')
    os.makedirs(out, exist_ok=True)
    subprocess.check_call(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                           '-t', '2', '-c:a', 'flac', tmpl])
    for rel, tags in spec:
        dst = os.path.join(out, os.path.splitext(rel)[0] + '.flac')
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(tmpl, 'rb') as a, open(dst, 'wb') as b:
            b.write(a.read())
        f = FLAC(dst)
        for k, v in tags.items():
            if not k.startswith('_'):
                f[k] = v
        f.save()
    os.remove(tmpl)
    print(f"rebuilt {len(spec)} tracks under {out}")


if __name__ == '__main__':
    a = sys.argv[1:]
    if a[:1] == ['--rebuild'] and len(a) == 3:
        rebuild(json.load(open(a[1], encoding='utf-8')), a[2])
    elif len(a) == 3 and a[1] == '--dump':
        json.dump(scan(a[0]), open(a[2], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f"wrote {a[2]}")
    elif len(a) == 1:
        report(scan(a[0]))
    else:
        raise SystemExit(__doc__)
