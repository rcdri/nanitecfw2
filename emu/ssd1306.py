"""SSD1306-compatible OLED controller (I2C slave at 0x3C)."""
from .i2c import I2CSlave

ARGC = {0x81: 1, 0x20: 1, 0x21: 2, 0x22: 2, 0xA8: 1, 0xD3: 1, 0xD5: 1, 0xD9: 1, 0xDA: 1,
        0xDB: 1, 0x8D: 1, 0x26: 6, 0x27: 6, 0x29: 5, 0x2A: 5, 0xA3: 2, 0xD6: 1, 0xAD: 1}


class SSD1306(I2CSlave):
    addr = 0x3C

    def __init__(self, width=128, height=32):
        self.w, self.h = width, height
        self.ram = bytearray(128 * 8)
        self.on = False
        self.contrast = 0x7f
        self.invert = False
        self.seg_remap = False
        self.com_remap = False
        self.mux = 63
        self.offset = 0
        self.start_line = 0
        self.mode = 2
        self.col = 0
        self.page = 0
        self.col_lo, self.col_hi = 0, 127
        self.page_lo, self.page_hi = 0, 7
        self.entire_on = False
        self.frame = 0             # bumps on each data write burst (dirty counter)
        self.cmd = []
        self.need = 0
        self.ctrl = None
        self.cmd_log = []
        self.offset_adj = 0      # panel wiring compensation (see notes)

    # ---- i2c ----
    def start(self, read):
        self.ctrl = None
        self.first = True

    def read_byte(self):
        return 0x00 if self.on else 0x40

    def stop(self):
        pass

    def write_byte(self, b):
        if self.ctrl is None:
            self.ctrl = b
            self.cont = not (b & 0x80)
            return True
        dc = self.ctrl & 0x40
        if dc:
            self._data(b)
        else:
            self._command(b)
        if not self.cont:
            self.ctrl = None        # Co=1: next byte is a control byte again
        return True

    # ---- controller ----
    def _command(self, b):
        if self.need:
            self.cmd.append(b)
            self.need -= 1
            if self.need == 0:
                self._exec(self.cmd)
            return
        n = ARGC.get(b, 0)
        if n:
            self.cmd, self.need = [b], n
        else:
            self._exec([b])

    def _exec(self, c):
        op = c[0]
        if len(self.cmd_log) < 400:
            self.cmd_log.append(' '.join(f'{x:02x}' for x in c))
        if op == 0xAE: self.on = False
        elif op == 0xAF: self.on = True
        elif op == 0x81: self.contrast = c[1]
        elif op == 0xA6: self.invert = False
        elif op == 0xA7: self.invert = True
        elif op == 0xA4: self.entire_on = False
        elif op == 0xA5: self.entire_on = True
        elif op == 0xA0: self.seg_remap = False
        elif op == 0xA1: self.seg_remap = True
        elif op == 0xC0: self.com_remap = False
        elif op == 0xC8: self.com_remap = True
        elif op == 0xA8: self.mux = c[1] & 0x3f
        elif op == 0xD3: self.offset = c[1] & 0x3f
        elif 0x40 <= op <= 0x7F: self.start_line = op & 0x3f
        elif op == 0x20: self.mode = c[1] & 3
        elif op == 0x21:
            self.col_lo, self.col_hi = c[1] & 0x7f, c[2] & 0x7f
            self.col = self.col_lo
        elif op == 0x22:
            self.page_lo, self.page_hi = c[1] & 7, c[2] & 7
            self.page = self.page_lo
        elif 0xB0 <= op <= 0xB7: self.page = op & 7
        elif op <= 0x0F: self.col = (self.col & 0xF0) | op
        elif 0x10 <= op <= 0x1F: self.col = (self.col & 0x0F) | ((op & 0xF) << 4)

    def _data(self, b):
        if self.col < 128:
            self.ram[self.page * 128 + self.col] = b
        self.frame += 1
        if self.mode == 2:
            self.col += 1
            if self.col > 127:
                self.col = self.col_lo
        elif self.mode == 0:
            self.col += 1
            if self.col > self.col_hi:
                self.col = self.col_lo
                self.page += 1
                if self.page > self.page_hi:
                    self.page = self.page_lo
        elif self.mode == 1:
            self.page += 1
            if self.page > self.page_hi:
                self.page = self.page_lo
                self.col += 1
                if self.col > self.col_hi:
                    self.col = self.col_lo

    # ---- render ----
    def pixels(self):
        """return list of rows (h x w) of 0/1 as seen on the glass"""
        rows = []
        nrows = min(self.h, self.mux + 1)
        for r in range(self.h):
            row = [0] * self.w
            if not self.on or r >= nrows:
                rows.append(row)
                continue
            # glass is mounted so that A1/C8 (the firmware's setting) is upright
            com = r if self.com_remap else (nrows - 1 - r)
            line = (com + self.start_line + self.offset_adj) & 63
            page, bit = line >> 3, line & 7
            base = page * 128
            for x in range(self.w):
                seg = x if self.seg_remap else (127 - x)
                v = 1 if self.entire_on else (self.ram[base + seg] >> bit) & 1
                row[x] = v ^ self.invert
            rows.append(row)
        return rows
