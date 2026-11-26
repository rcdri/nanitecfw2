"""RKnanoFW container parser (format per Rockbox utils/rknanoutils)."""
import struct, sys

CATS = [("stage", 4), ("font", 10), ("gbk", 5), ("strtbl", 10), ("imageres", 10), ("unk", 10)]

def parse_header(buf):
    h = {}
    y, md, mo = struct.unpack_from("<HBB", buf, 0)
    h["date"] = f"{y:x}-{mo:02x}-{md:02x}"
    h["version"] = struct.unpack_from("<HHH", buf, 4)
    h["unk6"] = buf[10:16].hex()
    h["vendor"] = buf[16:48].split(b"\0")[0].decode()
    h["model"] = buf[48:80].split(b"\0")[0].decode()
    off = 80
    for name, mx in CATS:
        n, = struct.unpack_from("<I", buf, off); off += 4
        blobs = [struct.unpack_from("<II", buf, off + 8 * i) for i in range(mx)]
        off += 8 * mx
        h[name] = blobs[:n]
        h[name + "_slot_off"] = off - 8 * mx
    h["pad"], h["size"] = struct.unpack_from("<II", buf, off); h["size_off"] = off + 4
    off += 8
    h["magic"] = buf[off:off + 8]
    h["hdr_size"] = off + 8
    return h

def parse_stage(buf):
    addr, count = struct.unpack_from("<II", buf, 0)
    secs = []
    for i in range(count):
        f = struct.unpack_from("<8I", buf, 8 + 32 * i)
        secs.append(dict(zip(["code_va", "code_pa", "code_sz", "data_va", "data_pa",
                              "data_sz", "bss_pa", "bss_sz"], f)))
    return addr, secs

if __name__ == "__main__":
    buf = open(sys.argv[1], "rb").read()
    h = parse_header(buf)
    for k, v in h.items():
        if isinstance(v, list):
            print(k, [(hex(a), hex(b)) for a, b in v])
        elif not k.endswith("_off"):
            print(k, hex(v) if isinstance(v, int) else v)
    print("file size", hex(len(buf)), "trailer", buf[h['size']:].hex() if h['size'] < len(buf) else "")
    o, s = h["stage"][3]
    addr, secs = parse_stage(buf[o:o + s])
    print("stage3 base", hex(addr), "sections", len(secs))
    for i, sc in enumerate(secs):
        print(i, " ".join(f"{k}={v:#x}" for k, v in sc.items()))
