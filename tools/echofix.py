#!/usr/bin/env python3
"""echofix.py - rescue a SNOWSKY ECHO NANO stuck in an update loop.

Talks RockUSB (Rockchip Loader mode, 2207:262d) straight through Linux
usbfs, so it needs only Python and root: no libusb, no pip.  Works in
Termux on a rooted phone with the player on USB-OTG.

    su -c "$PREFIX/bin/python echofix.py info"     # read-only checks
    su -c "$PREFIX/bin/python echofix.py fix"      # rename NANOV*.IMG -> .BAK (-y: no prompt)
    su -c "$PREFIX/bin/python echofix.py reboot"   # leave Loader mode

`fix` only rewrites the directory sector(s) that hold the update file's
name; every sector is saved to ./echofix-backup-*.bin before it is
written, and read back after.
"""
import ctypes, errno, glob, os, random, re, struct, sys, time

SECTOR = 512
VID = 0x2207
NAME_RE = re.compile(r'^NANOV\d{3}\.IMG$', re.I)

# ---------------------------------------------------------------- usbfs
class _Bulk(ctypes.Structure):
    _fields_ = [('ep', ctypes.c_uint), ('len', ctypes.c_uint),
                ('timeout', ctypes.c_uint), ('data', ctypes.c_void_p)]

class _Ioctl(ctypes.Structure):
    _fields_ = [('ifno', ctypes.c_int), ('ioctl_code', ctypes.c_int),
                ('data', ctypes.c_void_p)]

def _IOC(d, nr, size): return (d << 30) | (size << 16) | (ord('U') << 8) | nr
USBDEVFS_BULK = _IOC(3, 2, ctypes.sizeof(_Bulk))
USBDEVFS_CLAIMINTERFACE = _IOC(2, 15, 4)
USBDEVFS_RELEASEINTERFACE = _IOC(2, 16, 4)
USBDEVFS_IOCTL = _IOC(3, 18, ctypes.sizeof(_Ioctl))
USBDEVFS_CLEAR_HALT = _IOC(2, 21, 4)
USBDEVFS_DISCONNECT = _IOC(0, 22, 0)


class UsbFs:
    """One claimed interface with a bulk IN and a bulk OUT endpoint."""

    def __init__(self, path, desc):
        try:
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.ioctl
        except (OSError, AttributeError):
            self.libc = ctypes.CDLL('libc.so', use_errno=True)
        self.libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        self.fd = os.open(path, os.O_RDWR)
        self.path = path
        self.ifno, self.ep_in, self.ep_out, self.cls = pick_interface(desc)
        n = ctypes.c_uint(self.ifno)
        if self._ioctl(USBDEVFS_CLAIMINTERFACE, ctypes.byref(n)) < 0:
            e = ctypes.get_errno()
            if e != errno.EBUSY:
                raise OSError(e, 'claim interface: ' + os.strerror(e))
            req = _Ioctl(self.ifno, USBDEVFS_DISCONNECT, None)   # detach kernel driver
            self._ioctl(USBDEVFS_IOCTL, ctypes.byref(req))
            if self._ioctl(USBDEVFS_CLAIMINTERFACE, ctypes.byref(n)) < 0:
                e = ctypes.get_errno()
                raise OSError(e, 'claim interface: ' + os.strerror(e))

    def _ioctl(self, req, arg):
        return self.libc.ioctl(self.fd, req, ctypes.cast(arg, ctypes.c_void_p))

    def _bulk(self, ep, buf, n, timeout):
        b = _Bulk(ep, n, timeout, ctypes.cast(buf, ctypes.c_void_p))
        r = self._ioctl(USBDEVFS_BULK, ctypes.byref(b))
        if r < 0:
            e = ctypes.get_errno()
            if e == errno.EPIPE:                      # stall: clear it, then fail
                c = ctypes.c_uint(ep)
                self._ioctl(USBDEVFS_CLEAR_HALT, ctypes.byref(c))
            raise OSError(e, f'bulk ep {ep:#x}: ' + os.strerror(e))
        return r

    def write(self, data, timeout=5000):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        n = self._bulk(self.ep_out, buf, len(data), timeout)
        if n != len(data):
            raise IOError(f'short bulk write {n}/{len(data)}')

    def read(self, n, timeout=5000):
        buf = ctypes.create_string_buffer(n)
        got = self._bulk(self.ep_in, buf, n, timeout)
        return buf.raw[:got]

    def close(self):
        n = ctypes.c_uint(self.ifno)
        self._ioctl(USBDEVFS_RELEASEINTERFACE, ctypes.byref(n))
        os.close(self.fd)


def parse_descriptors(raw):
    """-> (vid, pid, bcdUSB, [(ifno, class, sub, proto, [(ep, attr)])])"""
    vid, pid = struct.unpack_from('<HH', raw, 8)
    bcd = struct.unpack_from('<H', raw, 2)[0]
    itfs, i, cur = [], raw[0], None
    while i + 2 <= len(raw) and raw[i] >= 2:
        L, T = raw[i], raw[i + 1]
        if T == 4:
            cur = [raw[i + 2], raw[i + 5], raw[i + 6], raw[i + 7], []]
            itfs.append(cur)
        elif T == 5 and cur is not None:
            cur[4].append((raw[i + 2], raw[i + 3]))
        i += L
    return vid, pid, bcd, itfs


def pick_interface(desc):
    best = None
    for ifno, c, s, p, eps in desc[3]:
        bi = [e for e, a in eps if a & 3 == 2 and e & 0x80]
        bo = [e for e, a in eps if a & 3 == 2 and not e & 0x80]
        if not bi or not bo:
            continue
        score = 2 if (c, s, p) == (0xff, 6, 5) else 1 if c == 0xff else 0
        if best is None or score > best[0]:
            best = (score, ifno, bi[0], bo[0], (c, s, p))
    if best is None:
        raise SystemExit('device has no bulk interface')
    return best[1:]


def find_device():
    hits = []
    for path in sorted(glob.glob('/dev/bus/usb/*/*')):
        try:
            with open(path, 'rb') as f:
                raw = f.read(4096)
        except OSError:
            continue
        if len(raw) >= 18 and raw[1] == 1:
            d = parse_descriptors(raw)
            if d[0] == VID:
                hits.append((path, d))
    if not hits:
        if os.geteuid() != 0:
            raise SystemExit('No access to /dev/bus/usb - run this through su (see --help).')
        raise SystemExit('No Rockchip (2207:xxxx) device found. Hold the Loader key combo with '
                         'the player plugged in, then check `lsusb`.')
    if len(hits) > 1:
        print('several Rockchip devices, using', hits[0][0])
    return hits[0]

# ---------------------------------------------------------------- RockUSB
OP_READ_LBA, OP_WRITE_LBA = 0x14, 0x15
OP_READ_FLASH_INFO, OP_READ_CHIP_INFO, OP_RESET = 0x1A, 0x1B, 0xFF


class RockUSB:
    """CBW/CSW framing as in rockchip-linux/rkdeveloptool RKComm.cpp."""

    def __init__(self, io):
        self.io = io

    def _cbw(self, op, dir_in, cblen, xfer, lba=0, count=0, sub=0):
        tag = random.getrandbits(32)
        cb = struct.pack('>BBIBH7x', op, sub, lba, 0, count)
        return tag, struct.pack('<IIIBBB', 0x43425355, tag, xfer,
                                0x80 if dir_in else 0, 0, cblen) + cb

    def _csw(self, tag, timeout=5000):
        csw = self.io.read(13, timeout)
        if len(csw) != 13:
            raise IOError(f'bad CSW length {len(csw)}')
        sig, t, _res, status = struct.unpack('<IIIB', csw)
        if sig != 0x53425355 or t != tag:
            raise IOError('CSW does not match command')
        if status != 0:
            raise IOError(f'device reported failure (status {status})')

    def cmd_in(self, op, n, cblen=6, lba=0, count=0, readlen=None):
        tag, cbw = self._cbw(op, True, cblen, n, lba, count)
        self.io.write(cbw)
        data = self.io.read(readlen or n)
        self._csw(tag)
        return data

    def chip_info(self):
        return self.cmd_in(OP_READ_CHIP_INFO, 16)

    def flash_info(self):
        d = self.cmd_in(OP_READ_FLASH_INFO, 11, readlen=512)
        size, block, page, ecc, acc, manuf, cs = struct.unpack_from('<IHBBBBB', d)
        return dict(sectors=size, block=block, page=page, manuf=manuf)

    def read(self, lba, count):
        d = self.cmd_in(OP_READ_LBA, count * SECTOR, cblen=10, lba=lba, count=count)
        if len(d) != count * SECTOR:
            raise IOError(f'short read at LBA {lba}')
        return d

    def write(self, lba, data):
        assert len(data) % SECTOR == 0
        count = len(data) // SECTOR
        tag, cbw = self._cbw(OP_WRITE_LBA, False, 10, len(data), lba, count)
        self.io.write(cbw)
        self.io.write(data, timeout=20000)
        self._csw(tag, timeout=20000)

    def reset(self):
        tag, cbw = self._cbw(OP_RESET, False, 6, 0)
        self.io.write(cbw)
        try:
            self._csw(tag, timeout=2000)
        except (IOError, OSError):
            pass                                       # it may drop off the bus first

# ---------------------------------------------------------------- disk access
class Disk:
    """Sector access on top of RockUSB (or a file, for testing)."""

    def __init__(self, dev, backup_dir='.'):
        self.dev, self.backup_dir, self.cache = dev, backup_dir, {}

    def read(self, lba, n=1):
        out = b''
        while n:
            c = min(n, 16)
            out += self.dev.read(lba, c)
            lba, n = lba + c, n - c
        return out

    def write_verified(self, lba, data):
        old = self.read(lba, len(data) // SECTOR)
        if old == data:
            return
        name = os.path.join(self.backup_dir, f'echofix-backup-lba{lba}-{int(time.time())}.bin')
        with open(name, 'wb') as f:
            f.write(old)
        print(f'  backup of LBA {lba} -> {name}')
        self.dev.write(lba, data)
        if self.read(lba, len(data) // SECTOR) != data:
            raise SystemExit(f'!! read-back mismatch at LBA {lba}. The original is in {name}. '
                             'Do not reboot; send me this output.')
        print(f'  wrote + verified LBA {lba}')

# ---------------------------------------------------------------- filesystems
def is_fat32(s):
    if s[510:512] != b'\x55\xaa' or s[0] not in (0xEB, 0xE9):
        return False
    bps, spc, rsvd, nf = struct.unpack_from('<HBHB', s, 11)
    fat16, = struct.unpack_from('<H', s, 22)
    fat32, = struct.unpack_from('<I', s, 36)
    root, = struct.unpack_from('<I', s, 44)
    return (bps == 512 and spc in (1, 2, 4, 8, 16, 32, 64, 128) and rsvd >= 1
            and nf in (1, 2) and fat16 == 0 and fat32 > 0 and root >= 2)

def is_exfat(s):
    return s[3:11] == b'EXFAT   ' and s[510:512] == b'\x55\xaa' and s[108] == 9

def is_vbr(s):
    return is_fat32(s) or is_exfat(s)


def find_fs(disk, total=None):
    """User area (FAT32/exFAT) lives after the firmware slots and the fixed
    200 MB raw "sub 1" area; scan 1 MB-aligned LBAs for an MBR or a VBR."""
    start, step = 409600, 2048
    end = start + 16 * 1024 * 2048                     # 16 GB of candidates
    if total:
        end = min(end, total)
    for c in range(start, end, step):
        s = disk.read(c)
        if is_vbr(s):
            return c, s, c
        if s[510:512] == b'\x55\xaa':
            for k in range(4):
                e = s[446 + 16 * k: 462 + 16 * k]
                ptype, first, size = e[4], *struct.unpack_from('<II', e, 8)
                if ptype in (0x0B, 0x0C, 0x07, 0x0E, 0x06) and 0 < first < 1 << 22 and size:
                    v = disk.read(c + first)
                    if is_vbr(v):
                        return c + first, v, c
    return None, None, None


class Dir:
    """A directory read into memory sector by sector, written back by sector."""

    def __init__(self, disk, lbas):
        self.disk, self.lbas = disk, lbas
        self.buf = bytearray(b''.join(disk.read(l) for l in lbas))
        self.orig = bytes(self.buf)

    def entries(self):
        for i in range(0, len(self.buf), 32):
            yield i // 32, self.buf[i:i + 32]

    def commit(self):
        for k, lba in enumerate(self.lbas):
            new = bytes(self.buf[k * SECTOR:(k + 1) * SECTOR])
            if new != self.orig[k * SECTOR:(k + 1) * SECTOR]:
                self.disk.write_verified(lba, new)


def chain(disk, fat_lba, first, end_mark, mask=0xFFFFFFFF, limit=4096):
    out, c = [], first
    while 2 <= c < end_mark and len(out) < limit:
        out.append(c)
        off = c * 4
        nxt, = struct.unpack_from('<I', disk.read(fat_lba + off // SECTOR), off % SECTOR)
        c = nxt & mask
    return out


class Fat32:
    kind = 'FAT32'

    def __init__(self, disk, base, vbr):
        bps, spc, rsvd, nf = struct.unpack_from('<HBHB', vbr, 11)
        fatsz, = struct.unpack_from('<I', vbr, 36)
        root, = struct.unpack_from('<I', vbr, 44)
        self.disk, self.base, self.spc = disk, base, spc
        self.fat = base + rsvd
        self.data = base + rsvd + nf * fatsz
        clus = chain(disk, self.fat, root, 0x0FFFFFF8, 0x0FFFFFFF)
        self.dir = Dir(disk, [self.data + (c - 2) * spc + k for c in clus for k in range(spc)])

    def listing(self):
        lfn = []
        for i, e in self.dir.entries():
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = []
                continue
            if e[11] == 0x0F:
                lfn.insert(0, (i, bytes(e)))
                continue
            base = e[0:8].decode('latin1').rstrip()
            ext = e[8:11].decode('latin1').rstrip()
            if e[12] & 0x08: base = base.lower()
            if e[12] & 0x10: ext = ext.lower()
            short = base + ('.' + ext if ext else '')
            long_ = None
            if lfn:
                chk = 0
                for b in e[0:11]:
                    chk = (((chk & 1) << 7) + (chk >> 1) + b) & 0xFF
                if all(x[1][13] == chk for x in lfn):
                    # lfn[] holds the entries in sequence order 1..n (nearest the SFN first)
                    name = b''.join(x[1][1:11] + x[1][14:26] + x[1][28:32] for x in lfn)
                    long_ = name.decode('utf-16le', 'replace').split('\0')[0]
            size, = struct.unpack_from('<I', e, 28)
            yield dict(idx=i, name=long_ or short, short=short, size=size,
                       dir=bool(e[11] & 0x10), lfn=[x[0] for x in lfn], attr=e[11])
            lfn = []

    def targets(self):
        return [f for f in self.listing() if not f['dir'] and not f['attr'] & 0x08
                and NAME_RE.match(f['short'])]

    def neutralize(self, f):
        o = f['idx'] * 32
        self.dir.buf[o + 8:o + 11] = b'BAK'             # NANOV200.IMG -> NANOV200.BAK
        for i in f['lfn']:                              # stale long name would no longer match
            self.dir.buf[i * 32] = 0xE5
        self.dir.commit()


class ExFat:
    kind = 'exFAT'

    def __init__(self, disk, base, vbr):
        fat_off, fat_len, heap, count, root = struct.unpack_from('<IIIII', vbr, 80)
        self.disk, self.base = disk, base
        self.spc = 1 << vbr[109]
        self.fat = base + fat_off
        self.heap = base + heap
        clus = chain(disk, self.fat, root, 0xFFFFFFF7)
        self.dir = Dir(disk, [self.heap + (c - 2) * self.spc + k for c in clus for k in range(self.spc)])

    def listing(self):
        ents = list(self.dir.entries())
        i = 0
        while i < len(ents):
            _, e = ents[i]
            if e[0] == 0:
                break
            if e[0] == 0x85 and i + 1 < len(ents) and ents[i + 1][1][0] == 0xC0:
                n = e[1]
                stream = ents[i + 1][1]
                nlen = stream[3]
                name = b''.join(bytes(ents[i + k][1][2:32]) for k in range(2, n + 1)
                                if i + k < len(ents) and ents[i + k][1][0] == 0xC1)
                name = name.decode('utf-16le', 'replace')[:nlen]
                size, = struct.unpack_from('<Q', stream, 24)
                attr, = struct.unpack_from('<H', e, 4)
                yield dict(idx=i, n=n, name=name, short=name, size=size, dir=bool(attr & 0x10))
                i += n + 1
                continue
            i += 1

    def targets(self):
        return [f for f in self.listing() if not f['dir'] and NAME_RE.match(f['name'])]

    def neutralize(self, f):
        i, n, name = f['idx'], f['n'], f['name']
        new = name[:-3] + ''.join('BAK'[k] if name[-3 + k].isupper() else 'bak'[k] for k in range(3))
        b = self.dir.buf
        units = new.encode('utf-16le')
        for k in range(2, n + 1):                        # rewrite name entries
            o = (i + k) * 32
            if b[o] != 0xC1:
                continue
            chunk = units[(k - 2) * 30:(k - 1) * 30]
            b[o + 2:o + 2 + len(chunk)] = chunk
        h = 0                                            # NameHash over the up-cased name
        for ch in new.upper().encode('utf-16le'):
            h = (((h & 1) << 15) | (h >> 1)) + ch & 0xFFFF
        struct.pack_into('<H', b, (i + 1) * 32 + 4, h)
        s, start = 0, i * 32                             # SetChecksum
        for k in range((n + 1) * 32):
            if k in (2, 3):
                continue
            s = (((s & 1) << 15) | (s >> 1)) + b[start + k] & 0xFFFF
        struct.pack_into('<H', b, start + 2, s)
        self.dir.commit()


SUB1_SECTORS = 409600           # raw "sub 1" area after the system area (fixed 200 MB)


def open_fs(disk, total=None):
    base, vbr, area = find_fs(disk, total)
    if base is None:
        return None
    fs = (ExFat if is_exfat(vbr) else Fat32)(disk, base, vbr)
    fs.sys_sectors = area - SUB1_SECTORS          # system area = both firmware copies
    return fs

# ---------------------------------------------------------------- firmware slots
def fw_header(s):
    if s[504:512] != b'RKnanoFW':
        return None
    y, d, m = struct.unpack_from('<HBB', s, 0)
    size, = struct.unpack_from('<I', s, 500)
    return dict(date=f'{y:x}-{m:02x}-{d:02x}', size=size, slot=(size + 0x104000) & ~0xFFFFF)


def check_slots(disk, sys_sectors=None):
    """copy 1 is at LBA 0, copy 2 at half the system area (the firmware reads the
    system-area size from the ID block and keeps [0x0300D10C] = size / 2)"""
    s0 = disk.read(0)
    h = fw_header(s0)
    if not h:
        print('  copy 1 (LBA 0): no firmware header  <-- unexpected')
        return False
    ok = disk.read(h['size'] // SECTOR) == s0
    print(f"  copy 1 (LBA 0): RKnanoFW {h['date']}, size {h['size']:#x}, end copy "
          f"{'ok' if ok else 'MISSING'}")
    if sys_sectors:
        l2 = sys_sectors // 2
        s2 = disk.read(l2)
        h2 = fw_header(s2)
        if h2:
            copy2 = disk.read(l2 + h2['size'] // SECTOR) == s2
            print(f"  copy 2 (LBA {l2}): RKnanoFW {h2['date']}, end copy {'ok' if copy2 else 'MISSING'}")
        else:
            print(f'  copy 2 (LBA {l2}): no header')
    return ok

# ---------------------------------------------------------------- main
def connect():
    path, desc = find_device()
    vid, pid, bcd, _ = desc
    mode = 'Loader' if bcd & 1 else 'MaskROM'
    print(f'device {vid:04x}:{pid:04x} at {path}, {mode} mode')
    if mode != 'Loader':
        raise SystemExit('Not in Loader mode - this script only works in Loader mode.')
    rk = RockUSB(UsbFs(path, desc))
    try:
        ci = rk.chip_info()
        print('  chip info:', ci[:4].decode('latin1', 'replace'))
    except (IOError, OSError) as e:
        print('  chip info: n/a', e)
    total = None
    try:
        fi = rk.flash_info()
        total = fi['sectors']
        print(f"  flash: {total} sectors ({total // 2048} MB)")
    except (IOError, OSError) as e:
        print('  flash info: n/a', e)
    return rk, total


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'help'
    if cmd not in ('info', 'fix', 'reboot'):
        print(__doc__)
        return
    rk, total = connect()
    if cmd == 'reboot':
        rk.reset()
        print('reset sent - the player should boot now.')
        return
    disk = Disk(rk)
    fs = open_fs(disk, total)
    print('firmware:')
    slots_ok = check_slots(disk, fs.sys_sectors if fs else None)
    print('internal storage:')
    if fs is None:
        raise SystemExit('  no FAT32/exFAT filesystem found - stopping, nothing written.')
    print(f'  {fs.kind} at LBA {fs.base}; root directory:')
    for f in fs.listing():
        if f.get('attr', 0) & 0x08:
            continue                                     # volume label
        print(f"    {'<dir>' if f['dir'] else f['size']:>12}  {f['name']}")
    tg = fs.targets()
    print('  update files:', ', '.join(t['name'] for t in tg) or 'none')
    if cmd == 'info':
        return
    if not slots_ok:
        raise SystemExit('Firmware copy 1 looks damaged; not touching anything. Send me this output.')
    if not tg:
        print('Nothing to rename. If it still loops, send me this output.')
        return
    if '-y' not in sys.argv and input(
            f"Rename {', '.join(t['name'] for t in tg)} to .BAK? [y/N] ").strip().lower() != 'y':
        return
    for t in tg:
        fs = open_fs(disk, total)                        # fresh copy of the directory
        t = next(x for x in fs.targets() if x['idx'] == t['idx'])
        fs.neutralize(t)
    left = open_fs(disk, total).targets()
    if left:
        raise SystemExit('!! still found: ' + ', '.join(t['name'] for t in left))
    print('Done. Now run:  echofix.py reboot')


if __name__ == '__main__':
    main()
