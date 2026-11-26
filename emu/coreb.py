"""Second Cortex-M3 ("HiFi"/codec core). Runs the codec image the system core
copies to 0x01000000 (segments 73-82), shares RAM with core A and talks to it
through the mailbox at 0x40110000."""
import struct
from unicorn.arm_const import *
from .core import Machine, MMIODevice
from .scs import SCS
from .periph import DWUart, CRU


class CoreB:
    def __init__(self, board, log=False):
        self.b = board
        self.m = m = Machine(name='B')
        # core B executes its image from 0x00000000, which aliases the
        # 0x01000000 RAM that core A loads it into
        board.ram.attach(m.uc, aliases={0x00000000: 0x01000000}, skip=(0x00000000,))
        for base in range(0x40000000, 0x70000000, 0x01000000):
            m.map_mmio(base, 0x01000000)
        m.map_mmio(0xE0000000, 0x00100000)
        m.add_device(SCS(m))
        self.uart = m.add_device(DWUart(m, 0x400C0000, out=board.uart.out, tag='uartB'))
        m.add_device(CRU(m, 0x40180000))
        m.add_device(board.mbox.view(m, 'B'))
        board.mbox.mb = m
        m.cycles = board.m.cycles
        m.cpu_hz = board.m.cpu_hz
        sp, pc = struct.unpack('<II', bytes(m.uc.mem_read(0x01000000, 8)))
        m.vtor = 0x01000000
        m.uc.reg_write(UC_ARM_REG_MSP, sp)
        m.uc.reg_write(UC_ARM_REG_PC, pc)
        self.alive = True
        if log:
            print(f"[coreB] released: sp={sp:#x} pc={pc:#x}")

    def step(self, count):
        m = self.m
        if not self.alive:
            return
        m.try_take_exception()
        ok = m.run_slice(count)
        if not ok or m.stop_reason in ('unmapped', 'bkpt') or (
                m.stop_reason and str(m.stop_reason).startswith('intr')):
            print("[coreB] crash:", m.stop_reason, f"pc={m._pc():#x}",
                  ' '.join(f"{x:#x}" for x in m.backtrace()))
            self.alive = False
            return
        m.cycles = self.b.m.cycles
        m.update_systick()
        m.try_take_exception()
