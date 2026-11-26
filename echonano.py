#!/usr/bin/env python3
"""SNOWSKY ECHO NANO emulator front-end.

  python3 echonano.py run  NANOV200.IMG                 # window (Tk)
  python3 echonano.py run  NANOV200.IMG --music ~/Music/test --sd sd.img
  python3 echonano.py script NANOV200.IMG demo.txt      # headless, scripted

Keys (window):  Up/Left = prev   Down/Right = next   Enter = ok
                Backspace/Esc = back   Space/P = play/power   M = key 0x40
                F2 = screenshot   F3 = start/stop GIF   F5 = insert/eject microSD
                (--audio-out FILE.wav records everything the firmware plays)
"""
import argparse, os, sys, time, struct

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from emu.board import EchoNano          # noqa: E402
import mkdisk                           # noqa: E402

KEYMAP = {
    'Up': 'prev', 'Left': 'prev', 'Down': 'next', 'Right': 'next',
    'Return': 'ok', 'KP_Enter': 'ok', 'BackSpace': 'back', 'Escape': 'back',
    'space': 'power', 'p': 'power', 'm': 'menu', 'Tab': 'menu',
}


def first_boot(firmware, disk, seconds=10.0):
    """Boot once headless so the firmware restores factory settings and formats
    the user area (it does that whenever the settings block in sub 1 is empty)."""
    print(f"[echonano] first boot: letting the firmware initialise {disk} ...")
    b = EchoNano(firmware, disk=disk)
    b.uart.out = open(os.devnull, 'w')
    while b.vtime() < seconds:
        if not b.step():
            break
    b.emmc.disk.flush()
    b.emmc.disk.f.close()
    if mkdisk.user_partition_offset(disk) is None:
        print("[echonano] warning: user area still not formatted")


def prepare_disk(args):
    disk = args.disk or os.path.splitext(args.firmware)[0] + '.nand.img'
    if not os.path.exists(disk) or args.new_disk:
        print(f"[echonano] creating virtual eMMC {disk}")
        mkdisk.build(args.firmware, disk, args.size)
        first_boot(args.firmware, disk)
    else:
        mkdisk.install_fw(disk, args.firmware)      # keep it in sync with the IMG we boot
    if args.music:
        off = mkdisk.user_partition_offset(disk)
        if off is None:
            first_boot(args.firmware, disk)
            off = mkdisk.user_partition_offset(disk)
        mkdisk.copy_tree(disk, args.music, offset=off)
    sd = None
    if args.sd:
        sd = args.sd
        if not os.path.exists(sd):
            mkdisk.build_sd(sd, args.sd_size, music=args.sd_music)
        elif args.sd_music:
            with open(sd, 'rb') as f:
                mbr = f.read(512)
            lba = struct.unpack_from('<I', mbr, 446 + 8)[0]
            mkdisk.copy_tree(sd, args.sd_music, offset=lba * 512)
    return disk, sd


def make_board(args):
    disk, sd = prepare_disk(args)
    b = EchoNano(args.firmware, disk=disk, sd=sd, cpi=args.cpi,
                 log_emmc=args.log_emmc, coreb=not args.no_coreb)
    if not args.uart:
        b.uart.out = open(os.devnull, 'w')
    if args.audio_out:
        b.capture_audio(args.audio_out)
    return b


# ---------------------------------------------------------------- recording
class GifRecorder:
    def __init__(self, board, path, scale=4):
        self.b, self.path, self.scale = board, path, scale
        self.frames = []
        self.last = None

    def poll(self):
        rows = self.b.oled.pixels()
        key = bytes(v for r in rows for v in r)
        if key != self.last:
            self.frames.append((self.b.vtime(), rows))
            self.last = key

    def save(self):
        from PIL import Image
        if not self.frames:
            return
        imgs, durs = [], []
        for i, (t, rows) in enumerate(self.frames):
            h, w = len(rows), len(rows[0])
            im = Image.new('L', (w, h))
            im.putdata([255 if v else 0 for r in rows for v in r])
            imgs.append(im.resize((w * self.scale, h * self.scale), Image.NEAREST))
            nxt = self.frames[i + 1][0] if i + 1 < len(self.frames) else t + 1.0
            durs.append(max(20, int((nxt - t) * 1000)))
        imgs[0].save(self.path, save_all=True, append_images=imgs[1:], duration=durs, loop=0)
        print(f"[echonano] wrote {self.path} ({len(imgs)} frames)")


# ---------------------------------------------------------------- headless
def run_script(b, path):
    """Script commands (one per line, '#' comments):
         wait SECONDS            run for SECONDS of emulated time
         press KEY [SECONDS]     hold KEY (prev/next/ok/back/menu/power), default 0.15 s
         shot FILE.png           save the screen
         gif FILE.gif | gif stop record the screen to an animated GIF
         sd in | sd out          insert / eject the microSD card
         dump ADDR LEN           hex dump of emulated memory
    """
    gif = None

    def advance(sec):
        end = b.vtime() + sec
        while b.vtime() < end:
            if not b.step():
                raise SystemExit("[echonano] emulation stopped")
            if gif:
                gif.poll()

    for ln, line in enumerate(open(path), 1):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        cmd, *a = line.split()
        if cmd == 'wait':
            advance(float(a[0]))
        elif cmd == 'press':
            b.key(a[0], True)
            advance(float(a[1]) if len(a) > 1 else 0.15)
            b.key(a[0], False)
            advance(0.05)
        elif cmd == 'shot':
            b.screenshot(a[0])
            print(f"[echonano] {b.vtime():7.2f}s  shot -> {a[0]}")
        elif cmd == 'gif':
            if a[0] == 'stop':
                gif.save()
                gif = None
            else:
                gif = GifRecorder(b, a[0])
        elif cmd == 'sd':
            b.set_sd_inserted(a[0] == 'in')
        elif cmd == 'dump':
            addr, n = int(a[0], 0), int(a[1], 0)
            data = bytes(b.m.uc.mem_read(addr, n))
            for i in range(0, n, 16):
                print(f"{addr + i:08x}: {data[i:i + 16].hex(' ')}")
        else:
            raise SystemExit(f"{path}:{ln}: unknown command {cmd!r}")
    if gif:
        gif.save()


# ---------------------------------------------------------------- window
def run_gui(b, scale=6, speed=1.0):
    import tkinter as tk
    W, H = 128, 32
    root = tk.Tk()
    root.title("ECHO NANO")
    root.configure(bg='#202024')
    frame = tk.Frame(root, bg='#0b0b0d', padx=18, pady=18)
    frame.pack(padx=16, pady=(16, 6))
    img = tk.PhotoImage(width=W * scale, height=H * scale)
    lbl = tk.Label(frame, image=img, bd=0, bg='black')
    lbl.pack()
    bar = tk.Frame(root, bg='#202024')
    bar.pack(pady=(0, 6))
    status = tk.StringVar()
    held = set()

    def btn(text, key):
        w = tk.Button(bar, text=text, width=6, relief='flat', bg='#33343a', fg='#ddd',
                      activebackground='#50525a')
        w.bind('<ButtonPress-1>', lambda e: key_down(key))
        w.bind('<ButtonRelease-1>', lambda e: key_up(key))
        w.pack(side='left', padx=2)


    state = {'frame': -1, 'gif': None, 'sd': b.mmc[0].card_present, 'shots': 0}
    MIN_HOLD = 0.08                     # s of emulated time a key must stay down
    pressed_at, pending_up = {}, {}

    def key_down(k):
        if k in pressed_at:
            return                      # ignore OS autorepeat; the firmware repeats itself
        pending_up.pop(k, None)
        pressed_at[k] = b.vtime()
        b.key(k, True)

    def key_up(k):
        t = pressed_at.pop(k, None)
        if t is None:
            return
        if b.vtime() - t < MIN_HOLD:
            pending_up[k] = t + MIN_HOLD   # too short for the key scanner: release later
        else:
            b.key(k, False)

    def on_key(e, down):
        k = KEYMAP.get(e.keysym)
        if k:
            (key_down if down else key_up)(k)
        elif down and e.keysym == 'F2':
            state['shots'] += 1
            p = f"echonano_{state['shots']:03d}.png"
            b.screenshot(p)
            print("[echonano] saved", p)
        elif down and e.keysym == 'F3':
            if state['gif']:
                state['gif'].save()
                state['gif'] = None
            else:
                state['gif'] = GifRecorder(b, time.strftime("echonano_%H%M%S.gif"))
        elif down and e.keysym == 'F5':
            state['sd'] = not state['sd']
            b.set_sd_inserted(state['sd'])

    for t, k in (('◀ prev', 'prev'), ('next ▶', 'next'), ('ok', 'ok'), ('back', 'back'),
                 ('menu', 'menu'), ('power', 'power')):
        btn(t, k)
    tk.Label(root, textvariable=status, bg='#202024', fg='#888', font=('TkFixedFont', 9)).pack(pady=(0, 8))

    root.bind('<KeyPress>', lambda e: on_key(e, True))
    root.bind('<KeyRelease>', lambda e: on_key(e, False))

    def draw():
        rows = b.oled.pixels()
        k = b.oled.contrast / 255 * 0.55 + 0.45
        on = '#%02x%02x%02x' % (int(235 * k), int(240 * k), int(255 * k))
        data = []
        for r in rows:
            line = '{' + ' '.join(on if v else '#000000' for v in r for _ in range(scale)) + '}'
            data.extend([line] * scale)
        img.put(' '.join(data), to=(0, 0))

    t0 = [time.time(), b.vtime()]
    anchor = [time.time(), b.vtime()]

    def tick():
        start = time.time()
        # run at (at most) real time: emulated seconds track wall-clock seconds
        target = anchor[1] + (start - anchor[0]) * speed
        if b.vtime() < target - 0.5:            # too slow to keep up: don't try to catch up
            anchor[0], anchor[1] = start, b.vtime()
            target = b.vtime() + 0.03
        while b.vtime() < target and time.time() - start < 0.03:
            if not b.step():
                status.set("emulation stopped: " + str(b.m.stop_reason))
                return
            for k, t in list(pending_up.items()):
                if b.vtime() >= t:
                    del pending_up[k]
                    b.key(k, False)
            if state['gif']:
                state['gif'].poll()
        if b.oled.frame != state['frame']:
            state['frame'] = b.oled.frame
            draw()
        now = time.time()
        if now - t0[0] > 1:
            ratio = (b.vtime() - t0[1]) / (now - t0[0])
            t0[0], t0[1] = now, b.vtime()
            status.set(f"t={b.vtime():7.1f}s  speed {ratio * 100:4.0f}%  "
                       f"sd={'in' if state['sd'] else 'out'}"
                       f"{'  ● REC' if state['gif'] else ''}")
        root.after(1, tick)

    draw()
    root.after(1, tick)
    root.mainloop()
    if state['gif']:
        state['gif'].save()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('run', 'script'):
        p = sub.add_parser(name)
        p.add_argument('firmware', help='NANOxxx.IMG')
        if name == 'script':
            p.add_argument('script', help='script file (see run_script docstring)')
        p.add_argument('--disk', help='virtual eMMC image (default: <firmware>.nand.img, created if missing)')
        p.add_argument('--new-disk', action='store_true', help='recreate the eMMC image')
        p.add_argument('--size', type=int, default=512, help='eMMC size in MB when creating')
        p.add_argument('--music', help='copy this folder into the internal storage')
        p.add_argument('--sd', help='microSD image (created if missing)')
        p.add_argument('--sd-size', type=int, default=256)
        p.add_argument('--sd-music', help='folder to copy onto a newly created microSD image')
        p.add_argument('--cpi', type=int, default=5,
                       help='emulated CPU cycles per instruction: higher = time runs faster per instruction')
        p.add_argument('--uart', action='store_true', help='show the firmware debug UART')
        p.add_argument('--log-emmc', action='store_true')
        p.add_argument('--audio-out', help='record what the firmware plays (I2S) to this WAV file')
        p.add_argument('--no-coreb', action='store_true',
                       help='do not emulate the second (codec) core; playback will stall')
        p.add_argument('--scale', type=int, default=6)
        p.add_argument('--speed', type=float, default=1.0, help='window: emulated seconds per real second')
    a = ap.parse_args()
    b = make_board(a)
    if a.cmd == 'run':
        run_gui(b, a.scale, a.speed)
    else:
        run_script(b, a.script)
    if b.emmc:
        for d in b.emmc.disks.values():
            d.flush()
    if b.audio_out is not None:
        b.audio_out.close()


if __name__ == '__main__':
    main()
