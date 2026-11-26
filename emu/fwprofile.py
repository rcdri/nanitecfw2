"""Firmware-specific addresses the emulator hooks (HLE), found by byte signature.

Addresses are for ECHO NANO V2.0.0. For other builds each function is searched
for by its signature in the resident code segment; if a signature is not found
(e.g. you patched that function) the hook is skipped with a warning.
"""
import struct

# name: (V2.0.0 address, signature bytes at the function entry)
SIGS = {
    'card_identify':  (0x0306208E, bytes.fromhex('022824bf04207047f0b585b00f460646')),
    'card_valid':     (0x0306156A, bytes.fromhex('0228')),          # resolved via card_identify
    'i2c_write_byte': (0x030682F6, bytes.fromhex('70b50546002415f0800f05d00122182100')),
    'delay_ms':       (0x030691F4, bytes.fromhex('044940f6b8328969b1fbf2f14843')),
    'delay_us':       (0x030691D0, bytes.fromhex('0d490e4a82b08969b1fbf2f148430c49')),
    'delay_loop':     (0x030691C8, bytes.fromhex('401efdd87047')),
}


def resolve(fw, log=print):
    """return {name: address or None} for the image `fw` (fwmem.FW)"""
    pa, code = fw.code(0)
    out = {}
    for name, (addr, sig) in SIGS.items():
        if name == 'card_valid':
            continue
        off = addr - pa
        if 0 <= off and code[off:off + len(sig)] == sig:
            out[name] = addr
            continue
        hits = []
        i = code.find(sig)
        while i >= 0 and len(hits) < 2:
            if i % 2 == 0:
                hits.append(i)
            i = code.find(sig, i + 1)
        if len(hits) == 1:
            out[name] = pa + hits[0]
            log(f"[fwprofile] {name}: relocated {addr:#x} -> {pa + hits[0]:#x}")
        else:
            out[name] = None
            log(f"[fwprofile] {name}: signature not found, HLE disabled")
    # card table: literal loaded by "ldr r1, [pc, #imm]" in the card-valid
    # helper, which in V2.0.0 sits 0xB24 bytes before card_identify
    out['card_table'] = 0x03004678
    if out.get('card_identify'):
        cv = out['card_identify'] - 0xB24
        o = cv - pa
        if code[o:o + 2] == b'\x02\x28':
            ins = struct.unpack_from('<H', code, o + 4)[0]
            if ins >> 11 == 0b01001:                     # LDR Rt, [PC, #imm8*4]
                lit = ((cv + 4 + 4) & ~3) + (ins & 0xff) * 4
                out['card_table'] = struct.unpack_from('<I', code, lit - pa)[0]
    return out
