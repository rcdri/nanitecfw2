#!/usr/bin/env python3
"""settingstest: check navilib's two Settings items in the emulator.

  python3 tools/settingstest.py out/NANOV200_mod.IMG [--shots DIR] [--keep DIR]

On a fresh virtual eMMC with a few generated WAV files (a tone, a one-file rip with
a 3-track .cue, three split tracks with a .cue and no PERFORMER) it:

  1. updates the media library (CUE sheets = Off, the default): 5 songs, no cue songs;
  2. plays a song with Power saving Off: decode-core clock, main-loop sleeps, wheel timer;
  3. walks the menus: System Settings > Power saving > On, Music Settings > CUE sheets
     > Stock; checks both bytes in RAM and in the saved settings block;
  4. reboots: both settings restored; plays again with Power saving On;
  5. updates the media library with CUE sheets = Stock: the sheets' tracks are songs,
     and dbcheck finds no album that opens the wrong songs.

Exit status 0 when every check passes. Takes a few minutes (about 60 s of emulated
time). --shots saves a screenshot after each menu step; --keep keeps the disk image.
"""
import math, os, shutil, struct, subprocess, sys, tempfile, wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
sys.path.insert(0, ROOT)
from emu.board import EchoNano      # noqa: E402
import mkdisk                       # noqa: E402
from echonano import first_boot     # noqa: E402

SETTINGS = 0x0300C04C               # +0x268 CUE sheets (2 = Stock), +0x269 Power saving (1 = On)
SCREEN = 0x03004A74                 # current screen; 0x0d while the library is updated
CLOCK_SUMS = 0x0306AE48             # end of the clock sum loop: sp+4..sp+0x18 = PLL, A x3, B core, B bus
LOOP, SLEEP = 0x0306A20A, 0x0306A20E    # main loop: after the sleep test / sleep path
TIMER_SETUP = 0x03053A7E            # r2 = period (us); IRQ 0x23 is the wheel

CUE_SINGLE = '''PERFORMER "Tester"
TITLE "Single"
FILE "album.wav" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Two"
    INDEX 01 00:03:00
  TRACK 03 AUDIO
    TITLE "Three"
    INDEX 01 00:06:00
'''
CUE_SPLIT = ''.join(f'FILE "{n:02d} part.wav" WAVE\n  TRACK {n:02d} AUDIO\n    TITLE "Part {n}"\n'
                    f'    INDEX 01 00:00:00\n' for n in (1, 2, 3))   # no PERFORMER

failures = []


def check(ok, what):
    print(('  ok    ' if ok else '  FAIL  ') + what)
    if not ok:
        failures.append(what)


def tone(path, seconds, hz):
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        frames = bytearray()
        for i in range(int(44100 * seconds)):
            v = int(8000 * math.sin(2 * math.pi * hz * i / 44100))
            frames += struct.pack('<hh', v, v)
        w.writeframes(bytes(frames))


def make_music(d):
    os.makedirs(os.path.join(d, 'Tone'))
    tone(os.path.join(d, 'Tone', 'tone.wav'), 12, 440)
    os.makedirs(os.path.join(d, 'Single'))
    tone(os.path.join(d, 'Single', 'album.wav'), 9, 330)
    open(os.path.join(d, 'Single', 'album.cue'), 'w', newline='\r\n').write(CUE_SINGLE)  # the parser wants CRLF
    os.makedirs(os.path.join(d, 'Split'))
    for n in (1, 2, 3):
        tone(os.path.join(d, 'Split', f'{n:02d} part.wav'), 2, 220 * n)
    open(os.path.join(d, 'Split', 'split.cue'), 'w', newline='\r\n').write(CUE_SPLIT)


class Run:
    """one boot of the emulated player"""

    def __init__(self, img, nand, shots=None, tag=''):
        self.b = EchoNano(img, disk=nand)
        self.b.uart.out = open(os.devnull, 'w')
        self.uc = self.b.m.uc
        self.shots, self.tag, self.n = shots, tag, 0
        self.sums, self.periods = [], []
        self.loops = self.sleeps = 0
        from unicorn import UC_HOOK_CODE
        for a in (CLOCK_SUMS, LOOP, SLEEP, TIMER_SETUP):
            self.uc.hook_add(UC_HOOK_CODE, self._hook, begin=a, end=a)
        self.adv(7)

    def _hook(self, uc, addr, size, _):
        from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_R2
        if addr == CLOCK_SUMS:
            v = struct.unpack('<6I', bytes(uc.mem_read(uc.reg_read(UC_ARM_REG_SP) + 4, 24)))
            self.sums.append(v)
        elif addr == LOOP:
            self.loops += 1
        elif addr == SLEEP:
            self.sleeps += 1
        else:
            self.periods.append(uc.reg_read(UC_ARM_REG_R2))

    def adv(self, t):
        end = self.b.vtime() + t
        while self.b.vtime() < end:
            if not self.b.step():
                raise SystemExit(f'{self.tag}: emulation stopped at {self.b.vtime():.2f} s')

    def press(self, *keys, after=0.7):
        for k in keys:
            self.b.key(k, True)
            self.adv(0.12)
            self.b.key(k, False)
            self.adv(after)
            if self.shots:
                self.n += 1
                self.b.screenshot(os.path.join(self.shots, f'{self.tag}_{self.n:02d}_{k}.png'), scale=2)

    def setting_bytes(self):
        return bytes(self.uc.mem_read(SETTINGS + 0x268, 2))

    def update_library(self):
        self.press('prev', 'ok', 'prev', 'ok', after=0.6)  # main menu: Media library > Update media library
        t0 = self.b.vtime()
        while bytes(self.uc.mem_read(SCREEN, 1)) == b'\x0d' or self.b.vtime() < t0 + 8:
            self.adv(2)
        self.adv(3)

    def play(self, seconds=6):
        self.press('ok', 'ok')          # Music play > first song
        self.sums.clear()
        l0, s0, self.periods = self.loops, self.sleeps, []
        self.adv(seconds)
        return self.loops - l0, self.sleeps - s0

    def close(self):
        self.b.emmc.disk.flush()
        self.b.emmc.disk.f.close()


def db(nand):
    sys.path.insert(0, HERE)
    import dbcheck
    d = dbcheck.Dump.from_nand(nand)
    count = struct.unpack_from('<H', d.read(0, 0x40), 0x2e)[0]
    recs = dbcheck.records(d, count)
    for x in recs:                  # +0x08 = 1: the song is a .cue TRACK
        x['cue'] = struct.unpack_from('<I', d.read(dbcheck.BASE + 4 * x['i'], 12), 8)[0] != 0
    out = subprocess.run([sys.executable, os.path.join(HERE, 'dbcheck.py'), '--nand', nand],
                         capture_output=True, text=True).stdout
    problems = [ln for ln in out.splitlines() if ln.startswith('problems:')]
    return recs, (problems[0] if problems else out.strip())


def saved_bytes(nand):
    with open(nand, 'rb') as f:
        f.seek(0x41 * 512 + 2)
        sys_mb = struct.unpack('<H', f.read(2))[0]
        f.seek((mkdisk.SYS_BASE + sys_mb * 2048) * 512 + 0x268)
        return f.read(2)


def show_songs(recs):
    for r in recs:
        cue = '  (.cue track)' if r['cue'] else ''
        print(f"        {r['file']!r:22} {r['title']!r}{cue}")


def power_report(run, loops, sleeps):
    playing = [v for v in run.sums if v[4] > 48_000_000] or run.sums[-1:]
    b_core = max(v[4] for v in playing) // 1_000_000 if playing else 0
    print(f'        decode core (B) {b_core} MHz, main loop {loops} passes / {sleeps} sleeps, '
          f'wheel timer {sorted(set(run.periods)) or "not re-armed"} us')
    return b_core


def main():
    args = sys.argv[1:]
    opt = lambda name: args[args.index(name) + 1] if name in args else None
    img = [a for i, a in enumerate(args) if not a.startswith('--') and (i == 0 or not args[i - 1].startswith('--'))]
    if not img:
        raise SystemExit(__doc__)
    img, shots, keep = img[0], opt('--shots'), opt('--keep')
    if shots:
        os.makedirs(shots, exist_ok=True)
    work = keep or tempfile.mkdtemp(prefix='settingstest_')
    os.makedirs(work, exist_ok=True)
    music, nand = os.path.join(work, 'music'), os.path.join(work, 'nand.img')
    shutil.rmtree(music, ignore_errors=True)
    make_music(music)
    mkdisk.build(img, nand)
    first_boot(img, nand)
    mkdisk.copy_tree(nand, music, offset=mkdisk.user_partition_offset(nand))

    print('1. update the media library (CUE sheets Off)')
    r = Run(img, nand, shots, 'scan')
    cue, pwr = r.setting_bytes()        # 0xff after a factory reset, 0 on older blocks: both Off
    check(cue != 2 and pwr != 1, f'fresh settings: CUE/Power bytes {cue:02x}{pwr:02x} (both Off)')
    r.update_library()
    r.close()
    recs, problems = db(nand)
    show_songs(recs)
    check(len(recs) == 5 and not any(x['cue'] for x in recs),
          f'{len(recs)} songs, none from a .cue sheet (expected 5)')

    print('2. play with Power saving Off')
    r = Run(img, nand, shots, 'off')
    b_off = power_report(r, *r.play())
    check(b_off > 300 and r.sleeps == 0 and set(r.periods) <= {400}, 'stock clocks, no sleeping, 400 us wheel')
    r.close()

    print('3. Settings menus')
    r = Run(img, nand, shots, 'menu')
    # the main menu starts at Music play and comes back there: next = Music Settings, next
    # next = System Settings; the new items are last in their lists (prev wraps to them),
    # and each opens with the cursor on the current choice
    r.press('next', 'next', 'ok', 'prev', 'ok', 'next', 'ok', 'back', 'back')   # Power saving: Off -> On
    r.press('next', 'ok', 'prev', 'ok', 'prev', 'ok', 'back', 'back')           # CUE sheets: Off -> Stock
    check(r.setting_bytes() == b'\x02\x01', f'RAM: CUE/Power bytes {r.setting_bytes().hex()} (expected 0201)')
    r.adv(3)
    r.close()
    check(saved_bytes(nand) == b'\x02\x01', f'saved settings block: {saved_bytes(nand).hex()} (expected 0201)')

    print('4. reboot, play with Power saving On')
    r = Run(img, nand, shots, 'on')
    check(r.setting_bytes() == b'\x02\x01', f'restored at boot: {r.setting_bytes().hex()}')
    loops, sleeps = r.play()
    b_on = power_report(r, loops, sleeps)
    check(0 < b_on <= 288 and sleeps == loops > 0 and set(r.periods) <= {1000},
          'lower clocks, sleeps on every pass, 1000 us wheel')
    r.close()

    print('5. update the media library with CUE sheets = Stock')
    r = Run(img, nand, shots, 'stock')
    r.update_library()
    r.close()
    recs, problems = db(nand)
    show_songs(recs)
    cue = [x for x in recs if x['cue']]
    check(len(cue) >= 3, f'{len(cue)} songs from .cue sheets')
    check(problems == 'problems: 0', f'dbcheck: {problems}')

    if not keep:
        shutil.rmtree(work)
    print('\nall checks passed' if not failures else f'\n{len(failures)} check(s) failed')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
