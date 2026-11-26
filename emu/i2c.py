"""Bit-banged I2C bus decoder + slave devices (driven by GPIO pin levels)."""


class I2CSlave:
    addr = 0x00

    def start(self, read):        # called after address byte matched
        pass

    def write_byte(self, b):      # master -> slave, return True for ACK
        return True

    def read_byte(self):          # slave -> master
        return 0xff

    def stop(self):
        pass


class BitBangI2C:
    def __init__(self, log=False):
        self.slaves = {}
        self.scl = 1
        self.sda_m = 1          # level driven by master (1 = released)
        self.sda_s = 1          # level driven by slave
        self.state = 'idle'
        self.bits = 0
        self.nbits = 0
        self.cur = None
        self.read = False
        self.log = log
        self.txlog = []
        self.out_byte = 0xff

    def add(self, slave):
        self.slaves[slave.addr] = slave

    @property
    def sda(self):
        return self.sda_m & self.sda_s

    def set_lines(self, scl, sda_m):
        old_scl, old_sda = self.scl, self.sda
        self.sda_m = sda_m
        if scl == old_scl:
            if scl == 1 and self.sda != old_sda:
                if self.sda == 0:
                    self._start()
                else:
                    self._stop()
            return
        self.scl = scl
        if scl == 1:
            self._rise()
        else:
            self._fall()

    def _start(self):
        if self.cur is not None and self.state != 'idle':
            pass  # repeated start
        self.state = 'addr'
        self.bits = self.nbits = 0
        self.sda_s = 1

    def _stop(self):
        if self.cur is not None:
            self.cur.stop()
        if self.log and self.txlog:
            print('[i2c]', ' '.join(self.txlog))
        self.txlog = []
        self.cur = None
        self.state = 'idle'
        self.sda_s = 1

    def _rise(self):
        if self.state in ('addr', 'wdata'):
            if self.nbits < 8:
                self.bits = (self.bits << 1) | self.sda
                self.nbits += 1
        elif self.state == 'rdata':
            self.nbits += 1                 # rising edge count within byte
            if self.nbits == 9:
                self.master_ack = (self.sda_m == 0)

    def _fall(self):
        st = self.state
        if st in ('addr', 'wdata'):
            if self.nbits == 8:
                b = self.bits
                ack = False
                if st == 'addr':
                    a, rd = b >> 1, b & 1
                    s = self.slaves.get(a)
                    if self.log:
                        self.txlog.append(f"<{a:02x}{'R' if rd else 'W'}{'' if s else '?'}>")
                    if s:
                        self.cur, self.read = s, bool(rd)
                        s.start(self.read)
                        ack = True
                    else:
                        self.cur = None
                else:
                    if self.cur:
                        ack = self.cur.write_byte(b)
                    if self.log and len(self.txlog) < 40:
                        self.txlog.append(f"{b:02x}")
                self.sda_s = 0 if ack else 1
                self.nbits = 9
                return
            if self.nbits == 9:          # end of ack clock
                self.sda_s = 1
                self.bits = self.nbits = 0
                if st == 'addr':
                    if self.cur is None:
                        self.state = 'nak'
                    elif self.read:
                        self.state = 'rdata'
                        self._load_read()
                    else:
                        self.state = 'wdata'
                return
        elif st == 'rdata':
            n = self.nbits
            if n < 8:
                self.sda_s = (self.out_byte >> (7 - n)) & 1
            elif n == 8:
                self.sda_s = 1           # release for master ack
            else:                        # after ack clock
                if getattr(self, 'master_ack', False):
                    self._load_read()
                else:
                    self.sda_s = 1
                    self.state = 'nak'

    def _load_read(self):
        self.out_byte = self.cur.read_byte() if self.cur else 0xff
        if self.log and len(self.txlog) < 40:
            self.txlog.append(f"r{self.out_byte:02x}")
        self.nbits = 0
        self.sda_s = (self.out_byte >> 7) & 1

    def fast_write_byte(self, b):
        """Equivalent of the firmware's bit-banged byte write (8 data clocks + 1 ack clock)."""
        for i in range(8):
            bit = (b >> (7 - i)) & 1
            self.set_lines(0, bit)
            self.set_lines(1, bit)
            self.set_lines(0, bit)
        self.set_lines(0, 1)
        self.set_lines(1, 1)
        self.set_lines(0, 1)
