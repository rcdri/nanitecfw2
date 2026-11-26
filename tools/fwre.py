#!/usr/bin/env python3
"""Small reverse-engineering helpers for RKnanoFW images.

  python3 tools/fwre.py IMG segs                       list code/data segments
  python3 tools/fwre.py IMG dis ADDR [N] [SEG]         disassemble N insns at ADDR (in segment SEG, default 0)
  python3 tools/fwre.py IMG refs VALUE [SEGS]          code that loads/branches to VALUE (ldr =, adr, bl/b)
  python3 tools/fwre.py IMG str TEXT                   where a string lives and who references it
Needs capstone (pip install capstone).
"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from fwmem import FW
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from capstone.arm import ARM_OP_IMM

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
md.detail = True


def sweep(fw, k):
    pa, c = fw.code(k)
    pos, out = 0, []
    while pos < len(c):
        for ins in md.disasm(c[pos:], pa + pos):
            pos = ins.address - pa + ins.size
            val = None
            if ins.mnemonic.startswith('ldr') and '[pc' in ins.op_str and len(ins.operands) > 1:
                la = ((ins.address + 4) & ~3) + ins.operands[1].mem.disp
                if pa <= la < pa + len(c) - 3:
                    val = struct.unpack_from('<I', c, la - pa)[0]
            elif ins.mnemonic.startswith('adr'):
                val = ((ins.address + 4) & ~3) + ins.operands[1].imm
            elif ins.mnemonic in ('bl', 'b', 'b.w', 'blx') and ins.operands and ins.operands[0].type == ARM_OP_IMM:
                val = ins.operands[0].imm
            out.append((ins.address, ins.mnemonic, ins.op_str, val))
        if pos < len(c):
            pos += 2
    return out


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__ or "usage: fwre.py IMG segs|dis ADDR [N [SEG]]|refs VALUE|str TEXT")
    fw = FW(sys.argv[1])
    cmd, a = sys.argv[2], sys.argv[3:]
    if cmd == 'segs':
        for k, s in enumerate(fw.secs):
            if s['code_sz'] or s['data_sz']:
                print(f"{k:3d} code {s['code_pa']:#010x}+{s['code_sz']:#07x}  data {s['data_pa']:#010x}"
                      f"+{s['data_sz']:#07x}  bss {s['bss_pa']:#010x}+{s['bss_sz']:#x}")
    elif cmd == 'dis':
        addr, n = int(a[0], 16) & ~1, int(a[1]) if len(a) > 1 else 30
        k = int(a[2]) if len(a) > 2 else 0
        pa, c = fw.code(k)
        for i, ins in enumerate(md.disasm(c[addr - pa:], addr)):
            extra = ''
            if ins.mnemonic.startswith('ldr') and '[pc' in ins.op_str and len(ins.operands) > 1:
                la = ((ins.address + 4) & ~3) + ins.operands[1].mem.disp
                if pa <= la < pa + len(c) - 3:
                    extra = f"  ; ={struct.unpack_from('<I', c, la - pa)[0]:#x}"
            print(f"{ins.address:08x}: {ins.mnemonic:8s} {ins.op_str}{extra}")
            if i + 1 >= n:
                break
    elif cmd == 'refs':
        v = int(a[0], 16)
        ks = [int(x) for x in a[1].split(',')] if len(a) > 1 else range(len(fw.secs))
        for k in ks:
            if fw.secs[k]['code_sz']:
                for ad, mn, op, val in sweep(fw, k):
                    if val == v:
                        print(f"seg{k:02d} {ad:#010x}: {mn} {op}")
    elif cmd == 'str':
        needle = a[0].encode()
        st = fw.stage
        i = st.find(needle)
        while i >= 0:
            while i > 0 and st[i - 1] not in (0,):
                i -= 1
            va = fw.base + i
            for k, s in enumerate(fw.secs):
                for kind in ('code', 'data'):
                    if s[kind + '_sz'] and s[kind + '_va'] <= va < s[kind + '_va'] + s[kind + '_sz']:
                        p = s[kind + '_pa'] + va - s[kind + '_va']
                        print(f"seg{k:02d} {kind} {p:#010x}: {st[i:i + 60].split(b'\\0')[0]!r}")
                        for kk in range(len(fw.secs)):
                            if fw.secs[kk]['code_sz']:
                                for ad, mn, op, val in sweep(fw, kk):
                                    if val == p:
                                        print(f"   <- seg{kk:02d} {ad:#010x}: {mn} {op}")
            i = st.find(needle, i + len(needle) + 1)
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
