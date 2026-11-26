#!/usr/bin/env python3
"""Build a virtual eMMC image for the ECHO NANO emulator.

    python3 mkdisk.py NANOV200.IMG nand.img --size 256 --music ~/Music/test

Layout (512-byte sectors):
    0x40      ID block ("NANO"), tells the firmware the system area size
    0x2000    "sub 0" system area: fw1 at +0, fw2 at +sys/2 (the firmware reads the
              system-area size from the ID block: [0x0300D108] = sys_mb << 11 sectors,
              [0x0300D10C] = half of that = where it writes the second copy)
    +sys      "sub 1" 200 MB raw data area (settings "SAVE" block, media DB);
              size is hard-coded in the firmware (SetSub1SizeMB(200))
    after     "sub 2" user area, FAT32 (what you'd see over USB)
"""
import argparse, os, struct, shutil, tempfile, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rkfw import parse_header

SECTOR = 512
SYS_BASE = 0x2000
SUB1_SECTORS = 200 * 2048


def fw_layout(raw, sys_mb):
    hdr = parse_header(raw)
    assert hdr['magic'] == b'RKnanoFW', "not an RKnanoFW image"
    fwsize = hdr['size']
    fw = raw[:fwsize + 0x200]          # firmware + trailing header copy
    fw2 = sys_mb * 2048 // 2           # [0x0300D10C]
    assert len(fw) <= fw2 * SECTOR, "firmware does not fit half the system area"
    return fw, fw2


def user_partition_offset(disk_path):
    """byte offset of the FAT partition inside the user area (None if not formatted yet)"""
    with open(disk_path, 'rb') as f:
        f.seek(0x41 * SECTOR + 2)
        sys_mb = struct.unpack('<H', f.read(2))[0]
        user = SYS_BASE + sys_mb * 2048 + SUB1_SECTORS
        f.seek(user * SECTOR)
        mbr = f.read(SECTOR)
    if mbr[510:512] != b'\x55\xaa':
        return None
    if mbr[0x52:0x57] == b'FAT32' or mbr[0x36:0x39] == b'FAT':
        return user * SECTOR                     # superfloppy
    lba = struct.unpack_from('<I', mbr, 446 + 8)[0]
    return (user + lba) * SECTOR if lba else None


def install_fw(disk_path, fw_path):
    """(Re)write both firmware slots of an existing disk image (like the updater does)."""
    raw = open(fw_path, 'rb').read()
    with open(disk_path, 'r+b') as f:
        f.seek(0x41 * SECTOR + 2)
        sys_mb = struct.unpack('<H', f.read(2))[0]
        fw, fw2 = fw_layout(raw, sys_mb)
        for lba in (0, fw2):
            f.seek((SYS_BASE + lba) * SECTOR)
            f.write(fw)


def build(fw_path, out, size_mb=512, sys_mb=32, music=None, label="ECHO NANO"):
    raw = open(fw_path, 'rb').read()
    hdr = parse_header(raw)
    assert hdr['magic'] == b'RKnanoFW', "not an RKnanoFW image"
    fwsize = hdr['size']
    fw = raw[:fwsize + 0x200]          # firmware + trailing header copy
    total = size_mb * 2048
    sys_sectors = sys_mb * 2048
    fw2 = sys_sectors // 2               # [0x0300D10C], see fw_layout()
    assert len(fw) <= fw2 * SECTOR, "system area too small"
    user_start = SYS_BASE + sys_sectors + SUB1_SECTORS
    user_sectors = total - user_start
    assert user_sectors > 65536 + 2048, "disk too small for FAT32 user area"

    with open(out, 'wb') as f:
        f.truncate(total * SECTOR)
        # ID block
        idb0 = bytearray(SECTOR)
        struct.pack_into('<I', idb0, 0, 0xfcdc8c3b)
        idb1 = bytearray(SECTOR)
        struct.pack_into('<H', idb1, 0x02, sys_mb)
        idb1[0x0a:0x0e] = b'NANO'
        struct.pack_into('<H', idb1, 0x16, 0x0100)
        struct.pack_into('<H', idb1, 0x20, 0x0100)
        f.seek(0x40 * SECTOR)
        f.write(idb0 + idb1)
        # two firmware copies
        for lba in (0, fw2):
            f.seek((SYS_BASE + lba) * SECTOR)
            f.write(fw)

    # The user area is left blank: on first boot the firmware finds no settings
    # block in sub 1, restores factory defaults and formats the user area itself
    # (MBR + FAT32 partition at +0x40). echonano.py does that first boot and then
    # copies music in with user_partition_offset()/copy_tree().
    print(f"{out}: {size_mb} MB, sys area {sys_mb} MB @ {SYS_BASE:#x}, "
          f"fw2 @ sys+{fw2:#x}, user area @ {user_start:#x} ({user_sectors} sectors)")


def make_fat(path_or_none, sectors, music=None, label="NO NAME"):
    """create a FAT32 volume of `sectors` sectors in a temp file, return its path"""
    from pyfatfs.PyFat import PyFat
    from pyfatfs.PyFatFS import PyFatFS
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.fat')
    tmp.close()
    with open(tmp.name, 'wb') as t:
        t.truncate(sectors * SECTOR)
    pf = PyFat()
    pf.mkfs(tmp.name, PyFat.FAT_TYPE_FAT32, size=sectors * SECTOR, label=label[:11])
    pf.close()
    if music:
        copy_tree(tmp.name, music)
    return tmp.name


def copy_tree(fat_path, music, offset=0):
    import warnings
    from pyfatfs.PyFatFS import PyFatFS
    with warnings.catch_warnings():
        # the firmware's quick format leaves FAT #2 stale; pyfatfs rewrites both
        warnings.simplefilter('ignore')
        fs = PyFatFS(fat_path, preserve_case=True, offset=offset)
    n = 0
    for root, dirs, files in os.walk(music):
        rel = os.path.relpath(root, music)
        dst_dir = '/' if rel == '.' else '/' + rel.replace(os.sep, '/')
        if dst_dir != '/':
            fs.makedirs(dst_dir, recreate=True)
        for fn in files:
            with open(os.path.join(root, fn), 'rb') as src, \
                    fs.openbin(dst_dir.rstrip('/') + '/' + fn, 'w') as dst:
                shutil.copyfileobj(src, dst)
            n += 1
    fs.close()
    print(f"copied {n} files")


def sparse_copy(src_path, dst, dst_offset):
    zero = bytes(1 << 16)
    with open(src_path, 'rb') as t:
        dst.seek(dst_offset)
        while True:
            chunk = t.read(1 << 16)
            if not chunk:
                break
            if chunk == zero[:len(chunk)]:
                dst.seek(len(chunk), 1)
            else:
                dst.write(chunk)


def build_sd(out, size_mb=256, music=None, label="SD CARD"):
    """microSD image: MBR + one FAT32 partition at sector 2048."""
    total = size_mb * 2048
    start = 2048
    part = total - start
    fat = make_fat(None, part, music, label)
    with open(out, 'wb') as f:
        f.truncate(total * SECTOR)
        mbr = bytearray(SECTOR)
        e = struct.pack('<B3sB3sII', 0x00, b'\x00\x02\x00', 0x0C, b'\xfe\xff\xff', start, part)
        mbr[446:462] = e
        mbr[510:512] = b'\x55\xaa'
        f.write(mbr)
        sparse_copy(fat, f, start * SECTOR)
    os.unlink(fat)
    print(f"{out}: {size_mb} MB microSD image (MBR + FAT32 @ {start})")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('firmware')
    ap.add_argument('out')
    ap.add_argument('--size', type=int, default=512, help='disk size in MB (default 512)')
    ap.add_argument('--sys', type=int, default=32, help='system area in MB (default 32)')
    ap.add_argument('--music', help='folder to copy into the user area')
    a = ap.parse_args()
    build(a.firmware, a.out, a.size, a.sys)
    if a.music:
        print("note: run `echonano.py run FW --disk OUT --music DIR` to do the first boot "
              "(the firmware formats the user area) and copy the music in")
