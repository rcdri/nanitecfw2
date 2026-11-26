"""Core of the ECHO NANO (Rockchip RKNanoD) emulator.

Cortex-M3 system core emulated with Unicorn. Unicorn has no NVIC, so exception
entry/exit, NVIC, SysTick and SCB are modelled here in Python.
"""
import struct, time, sys, collections, ctypes
from unicorn import *
from unicorn.arm_const import *


class SharedRAM:
    """Host buffers mapped into several Unicorn instances (dual-core RAM)."""

    def __init__(self, regions):
        self.bufs = {base: (ctypes.c_ubyte * size)() for base, size in regions}

    def attach(self, uc, aliases=None, skip=()):
        """aliases: {alias_base: region_base} extra mappings of a region"""
        for base, buf in self.bufs.items():
            if base in skip:
                continue
            uc.mem_map_ptr(base, ctypes.sizeof(buf), UC_PROT_ALL, ctypes.addressof(buf))
        for alias, base in (aliases or {}).items():
            buf = self.bufs[base]
            uc.mem_map_ptr(alias, ctypes.sizeof(buf), UC_PROT_ALL, ctypes.addressof(buf))

EXC_SVC, EXC_PENDSV, EXC_SYSTICK = 11, 14, 15


class MMIODevice:
    """Generic register-file device. Subclass and override read/write."""
    name = "dev"

    def __init__(self, m, base, size=0x10000):
        self.m, self.base, self.size = m, base, size
        self.regs = {}

    def read(self, off, size):
        return self.regs.get(off, 0)

    def write(self, off, size, val):
        self.regs[off] = val


class Machine:
    def __init__(self, trace_mmio=False, name='A'):
        self.name = name
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.trace_mmio = trace_mmio
        self.devices = {}          # page (addr>>16) -> device
        self.generic = {}          # unknown regs
        self.unknown_hits = collections.Counter()
        self.poll = collections.Counter()
        self.mmio_log = collections.deque(maxlen=4000)
        # NVIC state
        self.vtor = 0
        self.nvic_en = [False] * 256
        self.nvic_pend = [False] * 256
        self.prio = [0] * 272          # by exception number
        self.sys_pend = set()          # pending system exceptions (11,14,15)
        self.active = []               # stack of active exception numbers
        self.systick_ctrl = 0
        self.systick_load = 0
        self.icsr = 0
        self.scb = {}
        self.insn_budget = 0
        self.halted = False
        self.stop_reason = None
        self.hooks_pc = {}             # pc -> callback (HLE)
        self.ticks = 0
        self.cycles = 0
        self.cpu_hz = 144_000_000      # SysTick LOAD=0x15F8FF -> 10 ms tick
        self.uc.hook_add(UC_HOOK_INTR, self._intr)
        self.uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._unmapped)

    # ---------------- memory ----------------
    def map_ram(self, base, size):
        self.uc.mem_map(base, size)

    def map_mmio(self, base, size):
        self.uc.mmio_map(base, size, self._mmio_r, base, self._mmio_w, base)

    def add_device(self, dev):
        for p in range(dev.base >> 16, (dev.base + dev.size + 0xffff) >> 16):
            self.devices[p] = dev
        return dev

    def _pc(self):
        return self.uc.reg_read(UC_ARM_REG_PC)

    def _mmio_r(self, uc, off, size, base):
        a = base + off
        dev = self.devices.get(a >> 16)
        if dev is not None:
            v = dev.read(a - dev.base, size)
        else:
            v = self.generic.get(a, 0)
            self.unknown_hits[a] += 1
        if self.trace_mmio:
            self.mmio_log.append(('R', a, v, self._pc()))
            self.poll[(a, self._pc())] += 1
        return v & ((1 << (8 * size)) - 1)

    def _mmio_w(self, uc, off, size, val, base):
        a = base + off
        dev = self.devices.get(a >> 16)
        if dev is not None:
            dev.write(a - dev.base, size, val)
        else:
            self.generic[a] = val
            self.unknown_hits[a] += 1
        if self.trace_mmio:
            self.mmio_log.append(('W', a, val, self._pc()))

    def _unmapped(self, uc, typ, addr, size, val, u):
        print(f"[emu{self.name}] UNMAPPED access type={typ} addr={addr:#x} size={size} "
              f"val={val:#x} pc={self._pc():#x} lr={uc.reg_read(UC_ARM_REG_LR):#x}")
        self.stop_reason = 'unmapped'
        return False

    def r32(self, a):
        return struct.unpack('<I', self.uc.mem_read(a, 4))[0]

    def w32(self, a, v):
        self.uc.mem_write(a, struct.pack('<I', v & 0xffffffff))

    def cstr(self, a, n=256):
        b = bytes(self.uc.mem_read(a, n))
        return b.split(b'\0')[0].decode('latin1')

    # ---------------- exceptions ----------------
    def exc_prio(self, n):
        return self.prio[n]

    def current_prio(self):
        p = 256
        for n in self.active:
            p = min(p, self.exc_prio(n))
        bp = self.uc.reg_read(UC_ARM_REG_BASEPRI) & 0xff
        if bp:
            p = min(p, bp)
        return p

    def pending_exc(self):
        """return highest priority pending & enabled exception number or None"""
        best = None
        for n in self.sys_pend:
            if best is None or self.exc_prio(n) < self.exc_prio(best):
                best = n
        for irq in range(len(self.nvic_pend)):
            if self.nvic_pend[irq] and self.nvic_en[irq]:
                n = 16 + irq
                if best is None or self.exc_prio(n) < self.exc_prio(best):
                    best = n
        return best

    def set_irq(self, irq):
        self.nvic_pend[irq] = True

    def try_take_exception(self):
        if self.uc.reg_read(UC_ARM_REG_PRIMASK) & 1:
            # "cpsid i; wfi; cpsie i" idiom: WFI wakes on a pending IRQ even when
            # masked. Run on until the core unmasks, then take the exception.
            if self.halted and self.pending_exc() is not None:
                for _ in range(64):
                    try:
                        self.uc.emu_start(self._pc() | 1, 0xFFFFFFFF, count=1)
                    except UcError:
                        return False
                    if not self.uc.reg_read(UC_ARM_REG_PRIMASK) & 1:
                        break
                else:
                    return False
            else:
                return False
        n = self.pending_exc()
        if n is None:
            return False
        if self.exc_prio(n) >= self.current_prio():
            return False
        self.enter_exception(n)
        return True

    def enter_exception(self, n, return_pc=None):
        uc = self.uc
        if n >= 16:
            self.nvic_pend[n - 16] = False
        else:
            self.sys_pend.discard(n)
        ctrl = uc.reg_read(UC_ARM_REG_CONTROL)
        in_handler = bool(self.active)
        use_psp = (not in_handler) and (ctrl & 2)
        sp = uc.reg_read(UC_ARM_REG_SP)
        pc = self._pc() if return_pc is None else return_pc
        xpsr = uc.reg_read(UC_ARM_REG_XPSR)
        cur_ipsr = self.active[-1] if self.active else 0
        xpsr = (xpsr & ~0x1ff) | cur_ipsr | 0x01000000
        align = 0
        if sp & 4:
            align = 1 << 9
            sp -= 4
        sp -= 32
        regs = [uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                                         UC_ARM_REG_R3, UC_ARM_REG_R12, UC_ARM_REG_LR)]
        uc.mem_write(sp, struct.pack('<8I', *regs, pc & ~1, xpsr | align))
        uc.reg_write(UC_ARM_REG_SP, sp)
        if use_psp:
            exc_ret = 0xFFFFFFFD
            uc.reg_write(UC_ARM_REG_CONTROL, ctrl & ~2)   # switch to MSP
        elif in_handler:
            exc_ret = 0xFFFFFFF1
        else:
            exc_ret = 0xFFFFFFF9
        self.active.append(n)
        uc.reg_write(UC_ARM_REG_LR, exc_ret)
        vec = self.r32(self.vtor + 4 * n)
        uc.reg_write(UC_ARM_REG_PC, vec | 1)

    def exit_exception(self, excret):
        uc = self.uc
        if self.active:
            self.active.pop()
        mode = excret & 0xf
        if mode == 0xd:   # thread, PSP
            ctrl = uc.reg_read(UC_ARM_REG_CONTROL)
            uc.reg_write(UC_ARM_REG_CONTROL, ctrl | 2)
            sp = uc.reg_read(UC_ARM_REG_PSP)
        else:
            if mode == 0x9:
                ctrl = uc.reg_read(UC_ARM_REG_CONTROL)
                uc.reg_write(UC_ARM_REG_CONTROL, ctrl & ~2)
            sp = uc.reg_read(UC_ARM_REG_MSP)
        r0, r1, r2, r3, r12, lr, pc, xpsr = struct.unpack('<8I', uc.mem_read(sp, 32))
        sp += 32
        if xpsr & (1 << 9):
            sp += 4
        if mode == 0xd:
            uc.reg_write(UC_ARM_REG_PSP, sp)
        else:
            uc.reg_write(UC_ARM_REG_MSP, sp)
        for r, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                         UC_ARM_REG_R12, UC_ARM_REG_LR), (r0, r1, r2, r3, r12, lr)):
            uc.reg_write(r, v)
        uc.reg_write(UC_ARM_REG_APSR, xpsr & 0xf8000000)
        uc.reg_write(UC_ARM_REG_PC, pc | 1)
        # tail-chain
        self.try_take_exception()

    def _intr(self, uc, intno, u):
        pc = self._pc()
        if intno == 8:      # EXCP_EXCEPTION_EXIT
            self.exit_exception(pc | 1)
        elif intno == 2:    # SVC
            self.sys_pend.add(EXC_SVC)
            # SVC is synchronous: take it now regardless of current priority
            self.sys_pend.discard(EXC_SVC)
            self.enter_exception(EXC_SVC, return_pc=pc)
        elif intno == 7:    # BKPT
            print(f"[emu] BKPT at {pc:#x}")
            self.stop_reason = 'bkpt'
            uc.emu_stop()
        else:
            print(f"[emu] unhandled intr {intno} at pc={pc:#x}")
            self.stop_reason = f'intr{intno}'
            uc.emu_stop()

    # ---------------- run loop ----------------
    def run_slice(self, count=20000):
        uc = self.uc
        self.stop_reason = None
        pc = self._pc()
        try:
            uc.emu_start(pc | 1, 0xFFFFFFFF, count=count)
        except UcError as e:
            self.stop_reason = self.stop_reason or str(e)
            return False
        # detect WFI halt: previous halfword is WFI (bf30)
        try:
            self.halted = bytes(uc.mem_read((self._pc() - 2) & ~1, 2)) == b'\x30\xbf'
        except UcError:
            self.halted = False
        return True

    def update_systick(self):
        period = (self.systick_load + 1) if self.systick_load else 100000
        if self.systick_ctrl & 1:
            if not getattr(self, 'next_systick', 0):
                self.next_systick = self.cycles + period
            while self.cycles >= self.next_systick:
                self.next_systick += period
                self.systick_ctrl |= 1 << 16
                if self.systick_ctrl & 2:
                    self.sys_pend.add(EXC_SYSTICK)
        else:
            self.next_systick = 0

    def backtrace(self, n=12):
        """heuristic: code-looking words on the stack"""
        sp = self.uc.reg_read(UC_ARM_REG_SP)
        out = []
        try:
            words = struct.unpack('<256I', self.uc.mem_read(sp, 1024))
        except UcError:
            return out
        for w in words:
            if w & 1 and (0x03050000 <= w < 0x030a0000 or 0x01000000 <= w < 0x01020000):
                out.append(w)
                if len(out) >= n:
                    break
        return out
