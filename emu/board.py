"""ECHO NANO board: memory map, firmware loading, main loop."""
import struct, time, collections
from unicorn.arm_const import *
from .core import Machine, EXC_SYSTICK, SharedRAM
from .scs import SCS
from .periph import DWUart, SarADC, DWGpio, RKTimer, DWMMCStub, CRU, Mailbox, DWDmac
from .coreb import CoreB
from .fwprofile import resolve
from .i2c import BitBangI2C
from .ssd1306 import SSD1306
from .storage import DiskImage, VirtualEMMC
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from fwmem import FW


class EchoNano:
    BOOT_SECTIONS = (0, 1, 2, 3, 4)

    def __init__(self, img, disk=None, trace_mmio=False, verbose=True, log_emmc=False, i2c_log=False, cpi=5, sd=None, coreb=True):
        self.fw = FW(img)
        self.cpi = cpi        # virtual cycles per emulated instruction (speed/accuracy knob)
        self.m = m = Machine(trace_mmio=trace_mmio)
        self.verbose = verbose
        # RAM is shared with the second core (host buffers mapped into both)
        self.ram = SharedRAM([(0x00000000, 0x00100000), (0x01000000, 0x00100000),
                              (0x03000000, 0x00100000)])
        self.ram.attach(m.uc)
        for base in range(0x40000000, 0x70000000, 0x01000000):
            m.map_mmio(base, 0x01000000)
        m.map_mmio(0xE0000000, 0x00100000)
        m.add_device(SCS(m))
        self.uart = m.add_device(DWUart(m, 0x400C0000))
        self.overlay_hist = []
        self.timers = []
        self.adc = m.add_device(SarADC(m, 0x400D0000))
        self.gpio = [m.add_device(DWGpio(m, 0x40160000, port=0)),
                     m.add_device(DWGpio(m, 0x40170000, port=1)),
                     m.add_device(DWGpio(m, 0x50030000, port=2))]
        self.cru = m.add_device(CRU(m, 0x40180000))
        self.cru.on_reset = self._on_reset
        self.mbox = Mailbox(irq_a_base=9, irq_b_base=9)
        self.mbox.ma = m
        m.add_device(self.mbox.view(m, 'A'))
        self.dma = m.add_device(DWDmac(m, 0x60000000, irq=6, sink=self._audio_sink))
        self.dma.pace = self._i2s_seconds
        self.timers.append(self.dma)
        self.audio_log = []
        self.audio_rate = 44100       # I2S LRCK assumed 44.1 kHz (MCLK = 256 fs)
        self.audio_out = None         # wave.Wave_write, see capture_audio()
        self.coreb = None
        self.enable_coreb = coreb
        self.log_coreb = False
        self.mmc = [m.add_device(DWMMCStub(m, 0x60020000)), m.add_device(DWMMCStub(m, 0x60030000))]
        self.gpio[2].ext &= ~(1 << 5)          # power/play key (active high) released
        self.timer = m.add_device(RKTimer(m, 0x400E0000, irqs=(19, None)))
        self.timers.append(self.timer)
        # bit-banged I2C on GPIO0: pin24 = SDA, pin25 = SCL
        self.i2c = BitBangI2C(log=i2c_log)
        self.oled = SSD1306(128, 32)
        self.i2c.add(self.oled)
        g0 = self.gpio[0]
        g0.watchers.append(lambda g: self.i2c.set_lines(g.level(25), g.level(24) if (g.regs.get(4, 0) >> 24 & 1) else 1))
        g0.input_hooks[24] = lambda: self.i2c.sda
        self.addr = resolve(self.fw, log=print if verbose else (lambda *a: None))
        A = self.addr
        # HLE of the OLED bit-bang byte writer (hot path)
        if A['i2c_write_byte']:
            self.hle(A['i2c_write_byte'], self._hle_i2c_write_byte)
        # HLE busy-wait delays: skip the loop, advance virtual time instead
        if A['delay_ms']:
            self.hle(A['delay_ms'], lambda uc: self._hle_delay(uc, self.m.cpu_hz // 1000))
        if A['delay_us']:
            self.hle(A['delay_us'], lambda uc: self._hle_delay(uc, self.m.cpu_hz // 1000000))
        if A['delay_loop']:
            self.hle(A['delay_loop'], lambda uc: self._hle_delay(uc, 3))
        self.next_systick = 0
        self.emmc = VirtualEMMC(self, DiskImage(disk), log=log_emmc,
                                sd=DiskImage(sd) if sd else None) if disk else None
        self.set_sd_inserted(bool(sd))
        self.load_boot()

    # ------------------------------------------------------------------ keys
    # key ladder on SAR-ADC channel 1 + two GPIO keys (decoded from the
    # firmware's key scan at 0x03065DF4). Names are by observed function.
    KEYS = {
        'prev':  ('adc', 0x020),      # fw key code 0x0008
        'next':  ('adc', 0x080),      # 0x0004
        'ok':    ('adc', 0x100),      # 0x0040  enter / select
        'menu':  ('adc', 0x16a),      # 0x0002  options / settings
        'back':  ('adc', 0x1f0),      # 0x1000
        'power': ('gpio', 2, 5, 1),   # 0x0001  short: screen off/on, long: power off
        'ok2':   ('gpio', 2, 1, 0),   # 0x0040  second "enter" input (GPIO2.1, active low)
    }

    def key(self, name, down):
        k = self.KEYS[name]
        if k[0] == 'adc':
            self.adc.ch[1] = k[1] if down else 0x3ff
        else:
            _, port, pin, active = k
            self.gpio[port].set_input(pin, active if down else 1 - active)

    def vtime(self):
        """virtual seconds since reset (SysTick clock: 144 MHz)"""
        return self.m.cycles / self.m.cpu_hz

    def set_sd_inserted(self, present):
        """microSD card-detect is GPIO0 pin 11, active low"""
        self.mmc[0].card_present = present
        self.gpio[0].set_input(11, 0 if present else 1)

    # ------------------------------------------------------------------ audio
    I2S_TXDR = 0x40030024

    def _i2s_bytes_per_frame(self):
        vdw = (self.m.generic.get(0x40030000, 0x1f) & 0x1f) + 1        # TXCR valid data width
        return 2 * (4 if vdw > 16 else 2)

    def _i2s_seconds(self, nbytes):
        return nbytes / (self._i2s_bytes_per_frame() * self.audio_rate)

    def _audio_sink(self, addr, data):
        if len(self.audio_log) < 100:
            self.audio_log.append((hex(addr), len(data)))
        if addr == self.I2S_TXDR and self.audio_out is not None:
            if self._i2s_bytes_per_frame() == 8:      # 32-bit slots, keep the top 16 bits
                data = b''.join(data[i + 2:i + 4] for i in range(0, len(data) - 3, 4))
            self.audio_out.writeframes(data)

    def capture_audio(self, path):
        """write everything the firmware sends to the I2S DAC into a WAV file"""
        import wave
        w = wave.open(path, 'wb')
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(self.audio_rate)
        self.audio_out = w
        return w

    def _on_reset(self, rid, asserted):
        if rid != 0x32 or not self.enable_coreb:
            return
        if asserted:
            if self.coreb:
                self.coreb.alive = False
            self.coreb = None
        elif self.coreb is None:
            self.coreb = CoreB(self, log=self.log_coreb)

    def hle(self, addr, fn):
        from unicorn import UC_HOOK_CODE
        self.m.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, u: fn(uc), begin=addr, end=addr)

    def _hle_delay(self, uc, cycles_per_unit):
        n = uc.reg_read(UC_ARM_REG_R0)
        self.m.cycles += n * cycles_per_unit
        uc.reg_write(UC_ARM_REG_R0, 0)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def _hle_i2c_write_byte(self, uc):
        b = uc.reg_read(UC_ARM_REG_R0) & 0xff
        self.i2c.fast_write_byte(b)
        g0 = self.gpio[0]
        g0.regs[0] = (g0.regs.get(0, 0) | (1 << 24)) & ~(1 << 25)   # SDA=1, SCL=0
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def load_section(self, i, data=True, bss=True):
        fw, uc = self.fw, self.m.uc
        s = fw.secs[i]
        pa, code = fw.code(i)
        if code:
            uc.mem_write(pa, code)
        if data:
            pa, d = fw.data(i)
            if d:
                uc.mem_write(pa, d)
        if bss and s['bss_sz']:
            uc.mem_write(s['bss_pa'], b'\0' * s['bss_sz'])
        self.overlay_hist.append(i)

    def load_boot(self):
        for i in self.BOOT_SECTIONS:
            self.load_section(i)
        sp, rst = struct.unpack('<II', bytes(self.m.uc.mem_read(0x03050000, 8)))
        self.m.vtor = 0x03050000
        self.m.uc.reg_write(UC_ARM_REG_MSP, sp)
        self.m.uc.reg_write(UC_ARM_REG_PC, rst)

    # ------------------------------------------------------------------
    def systick_due(self):
        m = self.m
        return (m.systick_ctrl & 1)

    SLICE = 40000      # instructions per slice (~1 cycle each)

    def step(self):
        m = self.m
        m.poll.clear()
        m.try_take_exception()
        ok = m.run_slice(self.SLICE)
        if not ok or m.stop_reason in ('unmapped', 'bkpt', 'reset') or (
                m.stop_reason and m.stop_reason.startswith('intr')):
            print("[emu] crash:", m.stop_reason, self.regs())
            print("[emu] stack code ptrs:", ' '.join(f"{x:#x}" for x in m.backtrace()))
            return False
        # advance virtual time
        if m.halted:
            nxt = self.next_event()
            m.cycles = max(m.cycles + self.SLICE * self.cpi, nxt)
        else:
            m.cycles += self.SLICE * self.cpi
        self.update_timers()
        m.ticks += 1
        m.try_take_exception()
        if self.coreb is not None:
            self.coreb.step(self.SLICE * 2)      # the HiFi core is clocked ~2x faster
        if self.verbose and m.poll:
            (a, pc), n = m.poll.most_common(1)[0]
            if n > 20000:
                print(f"[emu] hot poll: addr={a:#x} pc={pc:#x} x{n}")
        return True

    def next_event(self):
        ev = [self.m.cycles + 10 * self.SLICE]
        if self.m.systick_ctrl & 1:
            ev.append(self.next_systick)
        for t in self.timers:
            ev.append(t.next_event())
        return min(ev)

    def update_timers(self):
        m = self.m
        m.update_systick()
        self.next_systick = m.next_systick
        for t in self.timers:
            t.update(m.cycles)

    def run(self, seconds=5.0):
        t0 = time.time()
        while time.time() - t0 < seconds:
            if not self.step():
                print("[emu] stopped:", self.m.stop_reason)
                return False
        return True

    def regs(self):
        uc = self.m.uc
        names = ['R0', 'R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8', 'R9', 'R10', 'R11',
                 'R12', 'SP', 'LR', 'PC']
        ids = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4,
               UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
               UC_ARM_REG_R10, UC_ARM_REG_R11, UC_ARM_REG_R12, UC_ARM_REG_SP,
               UC_ARM_REG_LR, UC_ARM_REG_PC]
        return ' '.join(f"{n}={uc.reg_read(i):08x}" for n, i in zip(names, ids))

    def screenshot(self, path, scale=6, color=(255, 255, 255)):
        from PIL import Image
        rows = self.oled.pixels()
        h, w = len(rows), len(rows[0])
        k = self.oled.contrast / 255 * 0.6 + 0.4
        on = tuple(int(c * k) for c in color)
        img = Image.new('RGB', (w, h))
        img.putdata([on if v else (0, 0, 0) for row in rows for v in row])
        img = img.resize((w * scale, h * scale), Image.NEAREST)
        img.save(path)
        return img
