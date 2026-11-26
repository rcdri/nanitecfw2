"""Load stage3 sections of an RKnanoFW image into address->bytes maps."""
import struct
from rkfw import parse_header, parse_stage

class FW:
    def __init__(self, path):
        self.raw = open(path, 'rb').read()
        self.hdr = parse_header(self.raw)
        o, s = self.hdr['stage'][3]
        self.stage_off = o
        self.stage = self.raw[o:o + s]
        self.base, self.secs = parse_stage(self.stage)
    def code(self, i):
        s = self.secs[i]; a = s['code_va'] - self.base
        return s['code_pa'], self.stage[a:a + s['code_sz']]
    def data(self, i):
        s = self.secs[i]; a = s['data_va'] - self.base
        return s['data_pa'], self.stage[a:a + s['data_sz']]
    def va_to_pa(self, va):
        """map a link (file) address inside a section to its runtime address"""
        for i, s in enumerate(self.secs):
            if s['code_sz'] and s['code_va'] <= va < s['code_va'] + s['code_sz']:
                return i, s['code_pa'] + va - s['code_va']
        return None
