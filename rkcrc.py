"""Rockchip 'RKCRC32': CRC-32/MPEG-2-like (poly 0x04C10DB7, MSB-first, init 0, no xorout)."""
_T = []
for i in range(256):
    c = i << 24
    for _ in range(8):
        c = ((c << 1) ^ 0x04C10DB7) if c & 0x80000000 else (c << 1)
    _T.append(c & 0xffffffff)

def rkcrc32(data, crc=0):
    T = _T
    for b in data:
        crc = ((crc << 8) & 0xffffffff) ^ T[((crc >> 24) ^ b) & 0xff]
    return crc
