"""RKNanoD peripheral models (reverse engineered from firmware behaviour)."""
import sys, struct
from .core import MMIODevice


class DWUart(MMIODevice):
    """Synopsys DesignWare APB UART (debug console)."""
    name = "uart"

    def __init__(self, m, base, out=None, tag='uart'):
        super().__init__(m, base, 0x10000)
        self.tag = tag
        self.line = bytearray()
        self.out = out or sys.stdout
        self.lines = []

    def read(self, off, size):
        if off == 0x7c:   # USR: TFNF | TFE
            return 0x06
        if off == 0x14:   # LSR: THRE | TEMT
            return 0x60
        return self.regs.get(off, 0)

    def write(self, off, size, val):
        if off == 0x00 and not (self.regs.get(0x0c, 0) & 0x80):
            c = val & 0xff
            if c == 10:
                s = self.line.decode('utf-8', 'replace').rstrip('\r')
                self.lines.append(s)
                print(f"\x1b[36m[{self.tag}]\x1b[0m {s}", file=self.out, flush=True)
                self.line.clear()
            else:
                self.line.append(c)
            return
        self.regs[off] = val


class SarADC(MMIODevice):
    """Rockchip SAR-ADC (10 bit).
    +0 data, +4 status (bit0 busy), +8 ctrl: bits0-2 channel, bit3 power,
    bit4 start, bit5 irq enable, bit6 irq status (w0 to clear)."""
    name = "adc"

    def __init__(self, m, base, irq=20):
        super().__init__(m, base, 0x10000)
        # ch0 = battery: mV = adc*2*2500/1024  -> 0x333 ~= 4.0 V
        # ch1 = key ladder (0x3ff = no key)
        self.ch = [0x333, 0x3ff, 0x3ff, 0x3ff, 0x3ff, 0x3ff, 0x3ff, 0x3ff]
        self.irq = irq
        self.regs[8] = 0

    def read(self, off, size):
        if off == 0x0:
            return self.ch[self.regs.get(8, 0) & 7]
        if off == 0x4:
            return 0
        return self.regs.get(off, 0)

    def write(self, off, size, val):
        old = self.regs.get(off, 0)
        self.regs[off] = val
        if off == 0x8 and (val & 0x10) and not (old & 0x10):
            # conversion finishes "instantly"
            self.regs[8] = val | 0x40
            if val & 0x20:
                self.m.set_irq(self.irq)


class DWGpio(MMIODevice):
    """Synopsys DesignWare APB GPIO (one 32-bit port)."""
    name = "gpio"

    def __init__(self, m, base, irq=None, port=0):
        super().__init__(m, base, 0x10000)
        self.irq, self.port = irq, port
        self.ext = 0xffffffff            # external levels of input pins (pull-ups)
        self.raw = 0
        self.watchers = []               # callables(dr_effective, ddr)
        self.input_hooks = {}            # pin -> callable() returning level

    def level(self, pin):
        ddr = self.regs.get(4, 0)
        if ddr >> pin & 1:
            return self.regs.get(0, 0) >> pin & 1
        if pin in self.input_hooks:
            return self.input_hooks[pin]()
        return self.ext >> pin & 1

    def read(self, off, size):
        if off == 0x50:
            ddr = self.regs.get(4, 0)
            v = (self.regs.get(0, 0) & ddr) | (self.ext & ~ddr)
            for pin, fn in self.input_hooks.items():
                if not (ddr >> pin & 1):
                    v = (v & ~(1 << pin)) | (fn() << pin)
            return v & 0xffffffff
        if off == 0x40:
            return self.raw & self.regs.get(0x30, 0) & ~self.regs.get(0x34, 0)
        if off == 0x44:
            return self.raw
        return self.regs.get(off, 0)

    def write(self, off, size, val):
        if off == 0x4c:                  # EOI (edge interrupts)
            self.raw &= ~val
            self._update_irq()
            return
        self.regs[off] = val
        if off in (0x00, 0x04):
            for w in self.watchers:
                w(self)
        if off in (0x30, 0x34, 0x38, 0x3c):
            self._eval_levels()

    def _eval_levels(self):
        lvl = ~self.regs.get(0x38, 0) & self.regs.get(0x30, 0)
        pol = self.regs.get(0x3c, 0)
        for pin in range(32):
            if lvl >> pin & 1:
                v = self.ext >> pin & 1
                if v == (pol >> pin & 1):
                    self.raw |= 1 << pin
                else:
                    self.raw &= ~(1 << pin)
        self._update_irq()

    def set_input(self, pin, level):
        old = self.ext >> pin & 1
        self.ext = (self.ext & ~(1 << pin)) | ((level & 1) << pin)
        en = self.regs.get(0x30, 0) >> pin & 1
        if en:
            edge = self.regs.get(0x38, 0) >> pin & 1
            pol = self.regs.get(0x3c, 0) >> pin & 1
            if edge:
                if old != level and level == pol:
                    self.raw |= 1 << pin
            else:
                if level == pol:
                    self.raw |= 1 << pin
                else:
                    self.raw &= ~(1 << pin)
        self._update_irq()

    def _update_irq(self):
        if self.irq is None:
            return
        if self.raw & self.regs.get(0x30, 0) & ~self.regs.get(0x34, 0):
            self.m.set_irq(self.irq)


class RKTimer(MMIODevice):
    """Rockchip timer block, 2 channels at +0x00/+0x20.
    +0 LOAD_COUNT lo, +4 LOAD_COUNT hi, +8 CUR lo, +0xc CUR hi, +0x10 CTRL, +0x18 INTSTATUS."""
    name = "timer"

    def __init__(self, m, base, irqs=(19, None), clk_hz=24_000_000):
        super().__init__(m, base, 0x10000)
        self.irqs = irqs
        self.clk = clk_hz
        self.start = [0, 0]
        self.next = [None, None]

    def _ch(self, off):
        return off // 0x20, off % 0x20

    def period_cycles(self, ch):
        load = self.regs.get(ch * 0x20, 0) | (self.regs.get(ch * 0x20 + 4, 0) << 32)
        load = max(load, 1)
        return max(1, load * self.m.cpu_hz // self.clk)

    def read(self, off, size):
        ch, r = self._ch(off)
        if ch < 2 and r in (0x8, 0xc):
            ctrl = self.regs.get(ch * 0x20 + 0x10, 0)
            if ctrl & 1:
                ticks = (self.m.cycles - self.start[ch]) * self.clk // self.m.cpu_hz
                load = self.regs.get(ch * 0x20, 0) | (self.regs.get(ch * 0x20 + 4, 0) << 32)
                if load:
                    ticks %= (load + 1)
                return (ticks >> 32) if r == 0xc else ticks & 0xffffffff
            return 0
        return self.regs.get(off, 0)

    def write(self, off, size, val):
        ch, r = self._ch(off)
        if ch < 2 and r == 0x18:
            self.regs[off] = self.regs.get(off, 0) & ~val
            return
        self.regs[off] = val
        if ch < 2 and r == 0x10:
            if val & 1:
                self.start[ch] = self.m.cycles
                self.next[ch] = self.m.cycles + self.period_cycles(ch)
            else:
                self.next[ch] = None

    def next_event(self):
        ev = [n for n in self.next if n is not None]
        return min(ev) if ev else 1 << 62

    def update(self, cycles):
        for ch in range(2):
            n = self.next[ch]
            if n is None or cycles < n:
                continue
            ctrl = self.regs.get(ch * 0x20 + 0x10, 0)
            per = self.period_cycles(ch)
            while self.next[ch] <= cycles:
                self.next[ch] += per
            if not (ctrl & 2):          # free-running mode keeps going; user mode stops
                pass
            if ctrl & 4:
                self.regs[ch * 0x20 + 0x18] = 1
                irq = self.irqs[ch]
                if irq is not None:
                    self.m.set_irq(irq)


class DWMMCStub(MMIODevice):
    """DesignWare MMC host: just enough to stop the driver from spinning.
    Card I/O is HLE'd in storage.py."""
    name = "dwmmc"
    card_present = False

    def read(self, off, size):
        if off == 0x2c:               # CMD: start_cmd bit clears immediately
            return self.regs.get(off, 0) & ~0x80000000
        if off == 0x44:               # RINTSTS: command done
            return 0x4
        if off == 0x48:               # STATUS: fifo empty, not busy
            return 0x4
        if off == 0x50:               # CDETECT: 0 = card present
            return 0 if self.card_present else 1
        return self.regs.get(off, 0)

    def write(self, off, size, val):
        if off == 0x00:               # CTRL reset bits self-clear
            val &= ~0x7
        self.regs[off] = val


class CRU(MMIODevice):
    """Clock/reset unit at 0x40180000 (register file; PLL reports locked).
    Soft-reset registers at +0xC0.. use Rockchip hi-word write masks;
    reset id 0x32 (reg 0xCC bit 2) holds the second ("HiFi") core in reset."""
    name = "cru"
    on_reset = None                   # callback(reset_id, asserted)

    def read(self, off, size):
        v = self.regs.get(off, 0)
        if off == 0x04:
            v |= 0x400                # PLL locked
        return v

    def write(self, off, size, val):
        if 0xc0 <= off < 0xe0:
            mask, bits = val >> 16, val & 0xffff
            old = self.regs.get(off, 0)
            new = (old & ~mask) | (bits & mask)
            self.regs[off] = new
            if self.on_reset:
                for b in range(16):
                    if mask >> b & 1 and ((old ^ new) >> b & 1 or off == 0xcc):
                        self.on_reset((off - 0xc0) // 4 * 16 + b, bool(new >> b & 1))
            return
        self.regs[off] = val


class Mailbox:
    """Rockchip inter-core mailbox at 0x40110000 (shared by both cores).
    +0x00 A2B_INTEN  +0x04 A2B_STATUS(w1c)  +0x08+8n A2B_CMDn  +0x0c+8n A2B_DATn
    +0x28 B2A_INTEN  +0x2c B2A_STATUS(w1c)  +0x30+8n B2A_CMDn  +0x34+8n B2A_DATn
    Channel n of B2A raises core-A IRQ 9+n (seen in the core-A vector table)."""

    def __init__(self, irq_a_base=9, irq_b_base=None):
        self.regs = {}
        self.ma = self.mb = None
        self.irq_a_base = irq_a_base
        self.irq_b_base = irq_b_base
        self.log = []
        self.halves = {}
        self.queue = {}
        self.latched = {}

    def view(self, m, side):
        mb = self

        class View(MMIODevice):
            name = 'mbox' + side

            def read(self, off, size):
                return mb.read(off)

            def write(self, off, size, val):
                mb.write(side, off, val)
        return View(m, 0x40110000, 0x10000)

    def read(self, off):
        if off in self.latched:
            return self.latched[off]
        return self.regs.get(off, 0)

    def write(self, side, off, val):
        """A message is CMD+DAT; it is delivered once both halves are written.
        Messages queue while the receiver hasn't acked (w1c) the previous one.
        When the codec core (B) sends, it is stopped so core A gets to run
        promptly (the cores otherwise only interleave at slice boundaries).
        Core A is NOT stopped when it sends: on hardware it keeps running, and
        its audio path posts "decode next" and only then reads the codec's
        buffer pointer. Stopping A there let B finish a fast decode and
        overwrite that pointer first (dropped/duplicated 512-frame blocks, and
        Tremor's buffer pool getting corrupted)."""
        r = self.regs
        if off in (0x04, 0x2c):
            r[off] = r.get(off, 0) & ~val
            base = 0x08 if off == 0x04 else 0x30
            for ch in range(4):
                q = self.queue.get((off, ch))
                if val >> ch & 1 and q and not r[off] >> ch & 1:
                    self._deliver(off, base + 8 * ch, ch, *q.pop(0))
            return
        r[off] = val
        if 0x08 <= off < 0x28 or 0x30 <= off < 0x50:
            a2b = off < 0x28
            ch = (off - (0x08 if a2b else 0x30)) // 8
            key = ('A2B' if a2b else 'B2A', ch)
            half = self.halves.get(key, 0) | (2 if off & 4 else 1)
            if half != 3:
                self.halves[key] = half
                return
            self.halves[key] = 0
            cmd_off = off & ~4
            cmd, dat = r.get(cmd_off, 0), r.get(cmd_off | 4, 0)
            if len(self.log) < 400:
                self.log.append((key[0], ch, cmd, dat))
            st = 0x04 if a2b else 0x2c
            if r.get(st, 0) >> ch & 1:
                self.queue.setdefault((st, ch), []).append((cmd, dat))
            else:
                self._deliver(st, cmd_off, ch, cmd, dat)
            if not a2b and self.mb is not None:
                self.mb.uc.emu_stop()
        elif off == 0x00:
            self._irq_b()
        elif off == 0x28:
            self._irq_a()

    def _deliver(self, st, cmd_off, ch, cmd, dat):
        self.latched[cmd_off], self.latched[cmd_off | 4] = cmd, dat
        self.regs[st] = self.regs.get(st, 0) | (1 << ch)
        (self._irq_b if st == 0x04 else self._irq_a)()

    def _irq_a(self):
        pend = self.regs.get(0x2c, 0) & self.regs.get(0x28, 0)
        for ch in range(4):
            if pend >> ch & 1 and self.ma:
                self.ma.set_irq(self.irq_a_base + ch)

    def _irq_b(self):
        pend = self.regs.get(0x04, 0) & self.regs.get(0x00, 0)
        if self.mb is None or self.irq_b_base is None:
            return
        for ch in range(4):
            if pend >> ch & 1:
                self.mb.set_irq(self.irq_b_base + ch)


class DWDmac(MMIODevice):
    """Synopsys DesignWare AHB DMA controller at 0x60000000 (8 channels,
    stride 0x58). Transfers run instantly (memory-to-memory) or are paced
    by `pace(ch, nbytes)` for peripheral destinations. Completion raises
    RawTfr/RawBlock and IRQ `irq` when unmasked."""
    name = "dmac"
    CH = 0x58

    def __init__(self, m, base, irq=6, sink=None):
        super().__init__(m, base, 0x10000)
        self.irq = irq
        self.sink = sink                   # callable(dst_addr, bytes) for peripheral writes
        self.raw = {0x2c0: 0, 0x2c8: 0, 0x2d0: 0, 0x2d8: 0, 0x2e0: 0}   # Tfr, Block, SrcTran, DstTran, Err
        self.mask = {0x310: 0, 0x318: 0, 0x320: 0, 0x328: 0, 0x330: 0}
        self.pending = []                  # (due_cycles, ch)
        self.log = []

    def read(self, off, size):
        if off in self.raw:
            return self.raw[off]
        if 0x2e8 <= off <= 0x308 and (off - 0x2e8) % 8 == 0:      # Status* = raw & mask
            k = 0x2c0 + (off - 0x2e8)
            return self.raw[k] & self.mask[0x310 + (off - 0x2e8)]
        if off in self.mask:
            return self.mask[off]
        if off == 0x360:                                          # StatusInt
            v = 0
            for i, k in enumerate((0x2c0, 0x2c8, 0x2d0, 0x2d8, 0x2e0)):
                if self.raw[k] & self.mask[0x310 + 8 * i]:
                    v |= 1 << i
            return v
        return self.regs.get(off, 0)

    def write(self, off, size, val):
        if off in self.mask:                                      # write-enable in hi byte
            we, bits = (val >> 8) & 0xff, val & 0xff
            self.mask[off] = (self.mask[off] & ~we) | (bits & we)
            self._irq()
            return
        if 0x338 <= off <= 0x358 and (off - 0x338) % 8 == 0:     # Clear*
            self.raw[0x2c0 + (off - 0x338)] &= ~val
            return
        if off == 0x3a0:                                          # ChEnReg
            we, bits = (val >> 8) & 0xff, val & 0xff
            old = self.regs.get(0x3a0, 0)
            new = (old & ~we) | (bits & we)
            self.regs[0x3a0] = new
            for ch in range(8):
                if new >> ch & 1 and not old >> ch & 1:
                    self._run(ch)
            return
        self.regs[off] = val

    def _r(self, ch, o):
        return self.regs.get(ch * self.CH + o, 0)

    def _block(self, sar, dar, ctl, ctlh):
        """one DMA block; returns (bytes moved, went to a peripheral)"""
        m = self.m
        sw = 1 << ((ctl >> 4) & 7)
        n = (ctlh & 0xfff) * sw
        if not n:
            return 0, False
        sinc, dinc = (ctl >> 9) & 3, (ctl >> 7) & 3
        data = bytes(m.uc.mem_read(sar, n)) if sinc == 0 else bytes(m.uc.mem_read(sar, sw)) * (n // sw)
        if len(self.log) < 50:
            self.log.append((hex(sar), hex(dar), n))
        if dinc == 0:
            try:
                m.uc.mem_write(dar, data)
                return n, False
            except Exception:
                pass
        if self.sink:
            self.sink(dar, data)
        return n, True

    def _run(self, ch):
        """Run a channel: the register block first, then the LLI chain
        ({SAR, DAR, LLP, CTL_LO, CTL_HI} in memory) while LLP_*_EN is set."""
        m = self.m
        sar, dar, llp = self._r(ch, 0x00), self._r(ch, 0x08), self._r(ch, 0x10)
        ctl, ctlh = self._r(ch, 0x18), self._r(ch, 0x1c)
        llp_en = ctl & (3 << 27)
        if llp_en and llp and not (ctlh & 0xfff):
            sar, dar, llp, ctl, ctlh = struct.unpack('<5I', bytes(m.uc.mem_read(llp & ~3, 20)))
        total, periph = 0, False
        for _ in range(4096):
            n, p = self._block(sar, dar, ctl, ctlh)
            total += n
            periph |= p
            if not (ctl & (3 << 27)) or not llp:
                break
            sar, dar, llp, ctl, ctlh = struct.unpack('<5I', bytes(m.uc.mem_read(llp & ~3, 20)))
        if periph and self.pace:
            due = self.m.cycles + int(self.pace(total) * self.m.cpu_hz)
            self.pending.append((due, ch))
        else:
            self.done(ch)

    pace = None                     # callable(nbytes) -> seconds the peripheral needs

    def next_event(self):
        return min((d for d, _ in self.pending), default=1 << 62)

    def update(self, cycles):
        if not self.pending:
            return
        due = [p for p in self.pending if p[0] <= cycles]
        if due:
            self.pending = [p for p in self.pending if p[0] > cycles]
            for _, ch in due:
                self.done(ch)

    def done(self, ch):
        self.regs[0x3a0] = self.regs.get(0x3a0, 0) & ~(1 << ch)
        self.raw[0x2c0] |= 1 << ch
        self.raw[0x2c8] |= 1 << ch
        self._irq()

    def _irq(self):
        if (self.regs.get(0x398, 0) & 1) and any(
                self.raw[k] & self.mask[0x310 + 8 * i]
                for i, k in enumerate((0x2c0, 0x2c8, 0x2d0, 0x2d8, 0x2e0))):
            self.m.set_irq(self.irq)
