#!/usr/bin/env python3
"""cavecheck.py - prove that patched bytes are not used by any live code.

    python3 tools/cavecheck.py STOCK.IMG MOD.IMG            # check what MOD changed
    python3 tools/cavecheck.py STOCK.IMG --range 0 0x305ef1c 0x305f0c4   # vet a cave

Every byte range that differs between the images (per segment), or every
--range SEG LO HI, is checked against the *stock* code of every segment that
can be resident at the same time. Reported references:

  pcrel    PC-relative load or ADR (literal pools, jump tables, strings)
  branch   b/bl/cbz... into a range, other than to its first byte
  entry    branch or pointer to the first byte of a range (hook sites: expected)
  pointer  a 32-bit word in any code or data whose value points into a range
  movw/t   a movw/movt pair building an address in a range

Each hit is validated: the referencing instruction must lie on an instruction
boundary of a linear disassembly of its function, otherwise it is a misdecode
and dropped. The referencing function is then classified:

  !! LIVE  reachable: called, branched to, or pointed at from somewhere that is
           itself live (pointers and calls from other segments count as live)
  ok dead  no path from anything live: the reference only matters to dead code

The tool exits non-zero if any LIVE reference remains. Cave code that we wrote
(inside a changed range) is not a reference source.
"""
import argparse, os, struct, sys
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_REG_PC

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from fwmem import FW  # noqa: E402

LOAD_SIZE = {'ldrb': 1, 'ldrsb': 1, 'ldrh': 2, 'ldrsh': 2, 'ldrd': 8, 'vldr': 8, 'pld': 1, 'pli': 1}
BRANCH = {'b', 'bl', 'blx', 'cbz', 'cbnz'} | {'b' + c for c in
          ('eq', 'ne', 'hs', 'lo', 'mi', 'pl', 'vs', 'vc', 'hi', 'ls', 'ge', 'lt', 'gt', 'le', 'cs', 'cc')}

md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
md.detail = True


def diff_ranges(a, b, base):
    out, i, n = [], 0, min(len(a), len(b))
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            out.append((base + i, base + j))
            i = j
        else:
            i += 1
    if len(b) > len(a):
        out.append((base + len(a), base + len(b)))
    return out


def hit_range(lo, hi, rs):
    for a, b in rs:
        if lo < b and hi > a:
            return (a, b)
    return None


class Image:
    def __init__(self, path):
        self.fw = FW(path)
        self.segs = [k for k, s in enumerate(self.fw.secs)
                     if s['code_sz'] and s['code_pa'] >= 0x3000000]
        self._dec = {}
        self._idx = None
        self._live = {}

    def code(self, k):
        return self.fw.code(k)

    def span(self, k):
        pa, c = self.code(k)
        return pa, pa + len(c)

    def coresident(self, k1, k2):
        if k1 == k2 or k1 == 0 or k2 == 0:
            return True
        a1, b1 = self.span(k1)
        a2, b2 = self.span(k2)
        return not (a1 < b2 and a2 < b1)

    def ins_at(self, k, a):
        key = (k, a)
        if key not in self._dec:
            pa, c = self.code(k)
            try:
                self._dec[key] = next(md.disasm(c[a - pa:a - pa + 4], a, 1))
            except StopIteration:
                self._dec[key] = None
        return self._dec[key]

    def func_start(self, k, a):
        pa, _ = self.code(k)
        for x in range(a & ~1, max(pa, a - 0x3000) - 1, -2):
            ins = self.ins_at(k, x)
            if ins is not None and ins.mnemonic in ('push', 'push.w', 'stmdb') and 'lr' in ins.op_str:
                return x
        return None

    def on_boundary(self, k, f, a):
        """is `a` an instruction boundary in a linear sweep from f?"""
        x = f
        while x < a:
            ins = self.ins_at(k, x)
            x += ins.size if ins is not None else 2
        return x == a

    def valid_src(self, k, a):
        f = self.func_start(k, a)
        return f is not None and self.on_boundary(k, f, a)

    def index(self):
        """branch targets, pointer values and movw/movt constants over every segment"""
        if self._idx:
            return self._idx
        br, ptr, mov = {}, {}, {}
        for k in self.segs:
            pa, c = self.code(k)
            for off in range(0, len(c) - 1, 2):
                ins = self.ins_at(k, pa + off)
                if ins is None:
                    continue
                mn = ins.mnemonic.split('.')[0]
                if mn in BRANCH and ins.operands and ins.operands[-1].type == ARM_OP_IMM:
                    br.setdefault(ins.operands[-1].imm, []).append((k, pa + off))
                elif mn == 'movw' and len(ins.operands) == 2:
                    for j in (4, 6, 8, 10, 12, 16):
                        if off + j + 4 > len(c):
                            break
                        nx = self.ins_at(k, pa + off + j)
                        if nx is not None and nx.mnemonic == 'movt' and nx.operands[0].reg == ins.operands[0].reg:
                            v = (nx.operands[1].imm << 16) | ins.operands[1].imm
                            mov.setdefault(v & ~1, []).append((k, pa + off))
                            break
            for blob_pa, blob in (self.fw.code(k), self.fw.data(k)):
                for off in range(0, len(blob) - 3, 2):
                    v = struct.unpack_from('<I', blob, off)[0]
                    if 0x3050000 <= v < 0x30a0000:
                        ptr.setdefault(v & ~1, []).append((k, blob_pa + off))
        self._idx = (br, ptr, mov)
        return self._idx

    def refs_to_func(self, ctx, k, f):
        """references to function f (in segment k) while segment ctx is loaded"""
        br, ptr, mov = self.index()
        out = []
        for k2, a in br.get(f, []):
            if self.coresident(k, k2) and self.coresident(ctx, k2) and self.valid_src(k2, a):
                out.append(('call', k2, a))
        for k2, a in ptr.get(f, []) + mov.get(f, []):
            if self.coresident(ctx, k2):
                out.append(('ptr', k2, a))
        return out

    def live(self, ctx, k, f, stack=()):
        """can f run while segment ctx is loaded? (reachable from a pointer, from
        another coresident segment, or from a live caller)"""
        if f is None or len(stack) > 16:
            return True                      # unknown shape / too deep: assume live
        key = (ctx, k, f)
        if key in self._live:
            return self._live[key]
        if (k, f) in stack:
            return False
        res = False
        for kind, k2, a in self.refs_to_func(ctx, k, f):
            if kind == 'ptr' or k2 != k or self.live(ctx, k2, self.func_start(k2, a), stack + ((k, f),)):
                res = True
                break
        if not stack or res:
            self._live[key] = res
        return res


def literal_edit(rs, ea, txt):
    """a word load of a literal whose changed bytes all sit inside that one word:
    a deliberate constant edit (e.g. a table pointer), not a cave overwrite"""
    if not (txt.startswith('ldr ') or txt.startswith('ldr.w ')) or ea & 3:
        return False
    r = hit_range(ea, ea + 4, rs)
    return r is not None and ea <= r[0] and r[1] <= ea + 4


def check(img, k, rs, own):
    """own: ranges whose code is ours (not a reference source)"""
    br, ptr, mov = img.index()
    hits = []
    pa, c = img.code(k)                      # PC-relative refs only from the same segment
    for off in range(0, len(c) - 1, 2):
        a = pa + off
        if hit_range(a, a + 1, own):
            continue
        ins = img.ins_at(k, a)
        if ins is None or len(ins.operands) < 2:
            continue
        op = ins.operands[-1]
        mn = ins.mnemonic.split('.')[0]
        if mn == 'adr':
            ea, size = ((a + 4) & ~3) + op.imm, 1
        elif op.type == ARM_OP_MEM and op.mem.base == ARM_REG_PC:
            ea, size = ((a + 4) & ~3) + op.mem.disp, LOAD_SIZE.get(mn, 4)
        else:
            continue
        if hit_range(ea, ea + size, rs):
            hits.append(('pcrel', k, a, f'{ins.mnemonic} {ins.op_str}', ea))
    for t, srcs in br.items():
        r = hit_range(t, t + 1, rs)
        if r:
            for k2, a in srcs:
                if img.coresident(k, k2) and not (k2 == k and hit_range(a, a + 1, own)):
                    ins = img.ins_at(k2, a)
                    hits.append(('entry' if t == r[0] else 'branch', k2, a, f'{ins.mnemonic} {ins.op_str}', t))
    for table, kind in ((ptr, 'pointer'), (mov, 'movw/t')):
        for v, srcs in table.items():
            r = hit_range(v, v + 1, rs)
            if r:
                for k2, a in srcs:
                    if (k2 == k and hit_range(a, a + 1, own)) or not img.coresident(k, k2):
                        continue
                    if kind == 'movw/t' and not img.valid_src(k2, a):
                        continue
                    hits.append(('entry' if v == r[0] else kind, k2, a, f'{kind} -> {v:#x}', v))
    out, seen = [], set()
    for kind, k2, a, txt, tgt in hits:
        if (kind, k2, a) in seen:
            continue
        seen.add((kind, k2, a))
        if kind in ('pcrel', 'branch') or (kind == 'entry' and 'pointer' not in txt and 'movw' not in txt):
            if not img.valid_src(k2, a):
                continue                     # misdecode (middle of an instruction / data)
            f = img.func_start(k2, a)
            if kind == 'entry':
                verdict = 'entry'
            elif kind == 'pcrel' and literal_edit(rs, tgt, txt):
                verdict = 'lit'              # we changed this constant on purpose
            else:
                verdict = 'LIVE' if img.live(k, k2, f) else 'dead'
        else:                                # data pointer / movw-movt: liveness unknown
            f = img.func_start(k2, a) if kind == 'movw/t' else None
            if kind == 'entry':
                verdict = 'entry'
            elif kind == 'movw/t':
                verdict = 'LIVE' if img.live(k, k2, f) else 'dead'
            else:
                verdict = 'LIVE'
        out.append((verdict, kind, k2, a, txt, tgt, f))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stock')
    ap.add_argument('mod', nargs='?')
    ap.add_argument('--range', nargs=3, action='append', metavar=('SEG', 'LO', 'HI'))
    ap.add_argument('--seg', type=int, action='append')
    ap.add_argument('-v', action='store_true', help='also list dead and entry references')
    args = ap.parse_args()
    img = Image(args.stock)
    todo = {}
    if args.range:
        for sk, lo, hi in args.range:
            todo.setdefault(int(sk, 0), []).append((int(lo, 0), int(hi, 0)))
    if args.mod:
        m = FW(args.mod)
        for k in img.segs:
            if (args.seg and k not in args.seg) or k >= len(m.secs):
                continue
            pa, a = img.code(k)
            pb, b = m.code(k)
            if pa == pb:
                rs = diff_ranges(a, b, pa)
                if rs:
                    todo.setdefault(k, []).extend(rs)
    live = 0
    for k, rs in sorted(todo.items()):
        rs = sorted(rs)
        print(f'== segment {k}: {len(rs)} ranges, {sum(b - a for a, b in rs)} bytes')
        res = check(img, k, rs, rs)
        for verdict, kind, k2, a, txt, tgt, f in sorted(res, key=lambda r: (r[0] != 'LIVE', r[3])):
            if verdict == 'LIVE' or args.v:
                fs = f' (fn {f:#x})' if f else ''
                print(f"   {'!!' if verdict == 'LIVE' else '  '} {verdict:5} {kind:7} seg{k2:<2} {a:#010x}{fs}  {txt}  [{tgt:#x}]")
        n = sum(1 for r in res if r[0] == 'LIVE')
        print(f'   live references: {n}')
        live += n
    print('TOTAL live references:', live)
    return 1 if live else 0


if __name__ == '__main__':
    sys.exit(main())
