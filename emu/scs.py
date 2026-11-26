"""Cortex-M System Control Space: NVIC, SysTick, SCB."""
from .core import MMIODevice, EXC_PENDSV, EXC_SYSTICK


class SCS(MMIODevice):
    name = "scs"

    def __init__(self, m):
        super().__init__(m, 0xE000E000, 0x1000)

    def read(self, off, size):
        m = self.m
        if off == 0x010:
            v = m.systick_ctrl
            m.systick_ctrl &= ~(1 << 16)     # COUNTFLAG clears on read
            return v
        if off == 0x014:
            return m.systick_load
        if off == 0x018:
            return m.systick_load // 2
        if off == 0x01c:
            return 0x80000000 | 0x2710
        if 0x100 <= off < 0x180:
            i = (off - 0x100) * 8
            return sum(1 << b for b in range(32) if m.nvic_en[i + b])
        if 0x180 <= off < 0x200:
            i = (off - 0x180) * 8
            return sum(1 << b for b in range(32) if m.nvic_en[i + b])
        if 0x200 <= off < 0x300:
            i = ((off - 0x200) & 0x7f) * 8
            return sum(1 << b for b in range(32) if m.nvic_pend[i + b])
        if 0x400 <= off < 0x4f0:
            n = 16 + (off - 0x400)
            return sum(m.prio[n + k] << (8 * k) for k in range(size) if n + k < len(m.prio))
        if off == 0xd00:
            return 0x412FC231          # CPUID Cortex-M3 r2p1
        if off == 0xd04:
            v = 0
            if EXC_PENDSV in m.sys_pend:
                v |= 1 << 28
            if EXC_SYSTICK in m.sys_pend:
                v |= 1 << 26
            if m.active:
                v |= m.active[-1]
            return v
        if off == 0xd08:
            return m.vtor
        if 0xd18 <= off < 0xd24:
            n = 4 + (off - 0xd18)
            return sum(m.prio[n + k] << (8 * k) for k in range(size))
        return self.regs.get(off, 0)

    def write(self, off, size, val):
        m = self.m
        if off == 0x010:
            m.systick_ctrl = val & 7
            return
        if off == 0x014:
            m.systick_load = val & 0xffffff
            return
        if off == 0x018:
            return
        if 0x100 <= off < 0x180:
            i = (off - 0x100) * 8
            for b in range(32):
                if val >> b & 1:
                    m.nvic_en[i + b] = True
            return
        if 0x180 <= off < 0x200:
            i = (off - 0x180) * 8
            for b in range(32):
                if val >> b & 1:
                    m.nvic_en[i + b] = False
            return
        if 0x200 <= off < 0x280:
            i = (off - 0x200) * 8
            for b in range(32):
                if val >> b & 1:
                    m.nvic_pend[i + b] = True
            return
        if 0x280 <= off < 0x300:
            i = (off - 0x280) * 8
            for b in range(32):
                if val >> b & 1:
                    m.nvic_pend[i + b] = False
            return
        if 0x400 <= off < 0x4f0:
            n = 16 + (off - 0x400)
            for k in range(size):
                if n + k < len(m.prio):
                    m.prio[n + k] = (val >> (8 * k)) & 0xff
            return
        if off == 0xd04:
            if val & (1 << 28):
                m.sys_pend.add(EXC_PENDSV)
            if val & (1 << 27):
                m.sys_pend.discard(EXC_PENDSV)
            if val & (1 << 26):
                m.sys_pend.add(EXC_SYSTICK)
            if val & (1 << 25):
                m.sys_pend.discard(EXC_SYSTICK)
            return
        if off == 0xd08:
            m.vtor = val & ~0x7f
            return
        if off == 0xd0c:
            if (val >> 16) == 0x05FA and val & 4:
                print("[emu] SYSRESETREQ")
                m.stop_reason = 'reset'
                m.uc.emu_stop()
            return
        if 0xd18 <= off < 0xd24:
            n = 4 + (off - 0xd18)
            for k in range(size):
                m.prio[n + k] = (val >> (8 * k)) & 0xff
            return
        self.regs[off] = val
