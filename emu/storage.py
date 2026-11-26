"""Virtual eMMC for the ECHO NANO.

The firmware's SD/MMC stack keeps a card table at CARD_TABLE (one pointer per
slot, slot 1 = internal eMMC).  Instead of emulating the DesignWare MMC host
controller we HLE the card-identify routine: it fills the card struct with a
capacity and read/write callbacks that point at tiny stubs which we hook.

Physical eMMC layout used by the firmware (sectors of 512 bytes):
    0x0000..0x2000   boot area, ID block at 0x40 + 0x400*n (2 sectors)
    0x2000..         "sub 0" system area, fw1 at LBA 0, fw2 1MB-aligned after it
    ...              "sub 2" user area (FAT, exposed over USB)
"""
import os, struct
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import *

SECTOR = 512


class DiskImage:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'r+b')
        self.f.seek(0, 2)
        self.sectors = self.f.tell() // SECTOR
        self.reads = self.writes = 0

    def read(self, lba, n):
        self.reads += 1
        self.f.seek(lba * SECTOR)
        d = self.f.read(n * SECTOR)
        return d + b'\0' * (n * SECTOR - len(d))

    def write(self, lba, data):
        self.writes += 1
        self.f.seek(lba * SECTOR)
        self.f.write(data)

    def flush(self):
        self.f.flush()


class VirtualEMMC:
    STUB_BASE = 0x0E000000

    def __init__(self, board, disk, log=False, sd=None):
        self.b, self.disk, self.log = board, disk, log
        self.CARD_TABLE = board.addr['card_table']
        self.FN_CARD_IDENTIFY = board.addr['card_identify']
        if self.FN_CARD_IDENTIFY is None:
            raise SystemExit("card-identify routine not found: storage HLE impossible for this build")
        self.disks = {1: disk}
        if sd is not None:
            self.disks[0] = sd
        m = board.m
        uc = m.uc
        uc.mem_map(self.STUB_BASE, 0x1000)
        # stub 0: read, stub 1: write  ->  "bx lr"
        uc.mem_write(self.STUB_BASE, struct.pack('<HH', 0x4770, 0x4770))
        self.stub_r, self.stub_w = self.STUB_BASE, self.STUB_BASE + 2
        uc.hook_add(UC_HOOK_CODE, self._identify, begin=self.FN_CARD_IDENTIFY,
                    end=self.FN_CARD_IDENTIFY)
        uc.hook_add(UC_HOOK_CODE, self._rw, begin=self.STUB_BASE, end=self.STUB_BASE + 3)
        self.identified = []

    def _ret(self, uc, val):
        uc.reg_write(UC_ARM_REG_R0, val)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def _identify(self, uc, addr, size, u):
        slot = uc.reg_read(UC_ARM_REG_R0)
        m = self.b.m
        self.identified.append(slot)
        disk = self.disks.get(slot)
        if disk is None:
            self._ret(uc, 0x10)          # no card in this slot
            return
        card = m.r32(self.CARD_TABLE + 4 * slot)
        if not card:
            self._ret(uc, 0x10)
            return
        m.w32(card + 0x00, slot)
        m.w32(card + 0x04, 0x84)        # high capacity, block addressed
        m.w32(card + 0x10, disk.sectors)
        m.w32(card + 0x14, 512)
        uc.mem_write(card + 0x18, b'\0')
        m.w32(card + 0x20, self.stub_r | 1)
        m.w32(card + 0x24, self.stub_w | 1)
        if self.log:
            print(f"[emmc] identify slot {slot}: {disk.sectors} sectors")
        self._ret(uc, 0)

    def _rw(self, uc, addr, size, u):
        slot, lba, n, buf = (uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                                                       UC_ARM_REG_R2, UC_ARM_REG_R3))
        disk = self.disks.get(slot)
        if disk is None:
            uc.reg_write(UC_ARM_REG_R0, 4)
            return
        if addr == self.stub_r:
            uc.mem_write(buf, disk.read(lba, n))
            if self.log:
                print(f"[emmc] R slot{slot} lba={lba:#x} n={n} -> {buf:#x}")
        else:
            disk.write(lba, bytes(uc.mem_read(buf, n * SECTOR)))
            if self.log:
                print(f"[emmc] W slot{slot} lba={lba:#x} n={n}")
        uc.reg_write(UC_ARM_REG_R0, 0)
