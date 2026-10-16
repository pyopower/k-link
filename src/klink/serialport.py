"""Virtual serial port for PTT/COS control: /dev/ttyKLINK0.

Many radio applications key the transmitter with a serial port's RTS or
DTR line and read the squelch (COS) from CTS/DCD/DSR. This module creates
a character device that behaves like such a port, using CUSE (character
devices in user space, part of the stock Linux kernel): no kernel module,
no extra packages. Data written to the port is discarded; only the control
lines matter.

Speaks the kernel's FUSE/CUSE wire protocol directly on /dev/cuse, which
must be accessible to this user (`k-link setup-serial` arranges it).
"""
import asyncio
import errno
import logging
import os
import struct

log = logging.getLogger("k-link")

# FUSE/CUSE kernel ABI (include/uapi/linux/fuse.h)
IN_HDR = struct.Struct("<IIQQIIIHH")         # len opcode unique nodeid uid..
OUT_HDR = struct.Struct("<IiQ")              # len error unique
CUSE_INIT, FUSE_OPEN, FUSE_READ, FUSE_WRITE = 4096, 14, 15, 16
FUSE_RELEASE, FUSE_FSYNC, FUSE_FLUSH = 18, 20, 25
FUSE_INTERRUPT, FUSE_IOCTL, FUSE_POLL = 36, 39, 40
CUSE_UNRESTRICTED_IOCTL = 1
FOPEN_DIRECT_IO, FOPEN_NONSEEKABLE = 1, 4
FUSE_IOCTL_RETRY = 4
MAX_IO = 4096

# modem lines (TIOCM_*)
LE, DTR, RTS, ST, SR, CTS, CAR, RNG, DSR = (
    1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x100)
PINS = {"rts": RTS, "dtr": DTR, "cts": CTS, "dcd": CAR, "dsr": DSR,
        "ri": RNG}

# tty ioctls (identical on arm64 and x86-64)
TCGETS, TCSETS, TCSETSW, TCSETSF = 0x5401, 0x5402, 0x5403, 0x5404
TCGETS2, TCSETS2, TCSETSW2, TCSETSF2 = (
    0x802C542A, 0x402C542B, 0x402C542C, 0x402C542D)
TIOCMGET, TIOCMBIS, TIOCMBIC, TIOCMSET = 0x5415, 0x5416, 0x5417, 0x5418
TIOCMIWAIT, TIOCGICOUNT = 0x545C, 0x545D
TIOCOUTQ, FIONREAD, TIOCINQ = 0x5411, 0x541B, 0x541B
TIOCGSERIAL, TIOCSSERIAL, TIOCGWINSZ, TIOCSWINSZ = (
    0x541E, 0x541F, 0x5413, 0x5414)
TIOCGEXCL = 0x80045440
# ioctl -> (bytes read from the caller, bytes returned to it); anything
# not listed takes its argument by value and just succeeds
IOCTL_IO = {
    TCGETS: (0, 36), TCSETS: (36, 0), TCSETSW: (36, 0), TCSETSF: (36, 0),
    TCGETS2: (0, 44), TCSETS2: (44, 0), TCSETSW2: (44, 0),
    TCSETSF2: (44, 0),
    TIOCMGET: (0, 4), TIOCMBIS: (4, 0), TIOCMBIC: (4, 0), TIOCMSET: (4, 0),
    TIOCOUTQ: (0, 4), FIONREAD: (0, 4), TIOCGICOUNT: (0, 80),
    TIOCGSERIAL: (0, 72), TIOCSSERIAL: (72, 0), TIOCGWINSZ: (0, 8),
    TIOCSWINSZ: (8, 0), TIOCGEXCL: (0, 4),
}
# 9600 8N1, raw: what a freshly opened USB serial adapter reports
B9600, CS8, CREAD, HUPCL, CLOCAL = 0o15, 0o60, 0o200, 0o2000, 0o4000
DEFAULT_TERMIOS = struct.pack("<IIIIB19sII", 0, 0,
                              B9600 | CS8 | CREAD | HUPCL | CLOCAL, 0, 0,
                              bytes(19), 9600, 9600)


class VirtualSerial:
    """A serial port whose RTS/DTR is PTT and whose CTS/DCD is COS."""

    def __init__(self, name="ttyKLINK0", ptt_pin="rts", cos_pins="cts,dcd",
                 on_ptt=None):
        self.name = name
        self.ptt_mask = PINS[ptt_pin]
        self.cos_mask = 0
        for p in cos_pins.replace(" ", "").split(","):
            if p:
                self.cos_mask |= PINS[p]
        self.on_ptt = on_ptt or (lambda on: None)
        self.fd = None
        self.out_lines = 0           # set by the application (RTS/DTR)
        self.cos = False
        self.termios = bytearray(DEFAULT_TERMIOS)
        self.opened = 0
        self.held = {}               # unique -> kind of request left pending
        self.iwait = {}              # unique -> line mask (TIOCMIWAIT)
        self.icount = [0] * 4        # cts dsr rng dcd transitions

    @property
    def path(self):
        return "/dev/" + self.name

    def ptt_mask_name(self):
        return next(k for k, v in PINS.items() if v == self.ptt_mask).upper()

    @property
    def ptt(self):
        return bool(self.out_lines & self.ptt_mask)

    def lines(self):
        """Modem status as the application sees it."""
        v = self.out_lines | DSR          # DSR: "device present"
        if self.cos:
            v |= self.cos_mask
        return v

    # --- lifecycle
    async def start(self):
        self.fd = os.open("/dev/cuse", os.O_RDWR)
        os.set_blocking(self.fd, False)
        asyncio.get_running_loop().add_reader(self.fd, self._readable)

    async def stop(self):
        if self.fd is not None:
            asyncio.get_running_loop().remove_reader(self.fd)
            os.close(self.fd)          # the device disappears
            self.fd = None
        if self.ptt:
            self.out_lines = 0
            self.on_ptt(False)

    def set_cos(self, on):
        if on == self.cos:
            return
        before = self.lines()
        self.cos = on
        self._lines_changed(before)

    # --- protocol
    def _readable(self):
        while True:
            try:
                msg = os.read(self.fd, MAX_IO + 4096)
            except BlockingIOError:
                return
            except OSError as e:
                if e.errno in (errno.EINTR, errno.ENOENT):
                    continue          # request aborted by the kernel
                log.error("serial port: /dev/cuse read failed: %s", e)
                asyncio.get_running_loop().remove_reader(self.fd)
                return
            try:
                self._handle(msg)
            except Exception:
                log.exception("serial port: bad request")

    def _reply(self, unique, data=b"", error=0):
        try:
            os.write(self.fd, OUT_HDR.pack(OUT_HDR.size + len(data),
                                           -error, unique) + data)
        except OSError as e:          # the caller went away (interrupted)
            if e.errno != errno.ENOENT:
                log.debug("serial port: reply failed: %s", e)

    def _handle(self, msg):
        _, op, unique, _, _, _, _, _, _ = IN_HDR.unpack_from(msg)
        body = msg[IN_HDR.size:]
        if op == CUSE_INIT:
            major, minor, _, _ = struct.unpack_from("<IIII", body)
            out = struct.pack("<IIIIIIII40x", 7, min(minor, 31), 0,
                              CUSE_UNRESTRICTED_IOCTL, MAX_IO, MAX_IO, 0, 0)
            self._reply(unique, out + f"DEVNAME={self.name}\0".encode())
            log.info("serial port %s ready", self.path)
        elif op == FUSE_OPEN:
            self.opened += 1
            self._reply(unique, struct.pack(
                "<QII", 0, FOPEN_DIRECT_IO | FOPEN_NONSEEKABLE, 0))
        elif op == FUSE_RELEASE:
            self.opened = max(0, self.opened - 1)
            if not self.opened:       # last close: drop PTT, like hangup
                self._set_out(0)
                self.termios = bytearray(DEFAULT_TERMIOS)
            self._reply(unique)
        elif op in (FUSE_FLUSH, FUSE_FSYNC):
            self._reply(unique)
        elif op == FUSE_READ:
            # nothing ever arrives on the data lines
            flags = struct.unpack_from("<QQIIQI", body)[5]
            if flags & os.O_NONBLOCK:
                self._reply(unique, error=errno.EAGAIN)
            else:
                self.held[unique] = "read"
        elif op == FUSE_WRITE:
            size = struct.unpack_from("<QQI", body)[2]
            self._reply(unique, struct.pack("<II", size, 0))
        elif op == FUSE_POLL:
            events = struct.unpack_from("<QQII", body)[3]
            self._reply(unique, struct.pack("<II", events & 0x0104, 0))
        elif op == FUSE_IOCTL:
            self._ioctl(unique, body)
        elif op == FUSE_INTERRUPT:
            target = struct.unpack_from("<Q", body)[0]
            if self.held.pop(target, None) or \
                    self.iwait.pop(target, None) is not None:
                self._reply(target, error=errno.EINTR)
        else:
            self._reply(unique, error=errno.ENOSYS)

    def _ioctl(self, unique, body):
        _, _, cmd, arg, in_size, out_size = struct.unpack_from(
            "<QIIQII", body)
        data = body[32:32 + in_size]
        need_in, need_out = IOCTL_IO.get(cmd, (0, 0))
        if (need_in or need_out) and (in_size < need_in or
                                      out_size < need_out):
            # unrestricted ioctl: tell the kernel which memory to copy
            iov = []
            if need_in:
                iov.append(struct.pack("<QQ", arg, need_in))
            if need_out:
                iov.append(struct.pack("<QQ", arg, need_out))
            return self._reply(unique, struct.pack(
                "<iIII", 0, FUSE_IOCTL_RETRY, 1 if need_in else 0,
                1 if need_out else 0) + b"".join(iov))
        out = b""
        if cmd in (TCGETS, TCGETS2):
            out = bytes(self.termios[:need_out])
        elif cmd in (TCSETS, TCSETSW, TCSETSF, TCSETS2, TCSETSW2, TCSETSF2):
            self.termios[:len(data)] = data
        elif cmd == TIOCMGET:
            out = struct.pack("<i", self.lines())
        elif cmd in (TIOCMBIS, TIOCMBIC, TIOCMSET):
            v = struct.unpack("<i", data[:4])[0] & (DTR | RTS)
            self._set_out(self.out_lines | v if cmd == TIOCMBIS else
                          self.out_lines & ~v if cmd == TIOCMBIC else v)
        elif cmd == TIOCMIWAIT:
            self.iwait[unique] = arg   # answered when a line changes
            return
        elif cmd == TIOCGICOUNT:
            cts, dsr, rng, dcd = self.icount
            out = struct.pack("<11i36x", cts, dsr, rng, dcd, 0, 0, 0, 0,
                              0, 0, 0)
        elif need_out:
            out = bytes(need_out)      # zeros: queues empty, defaults
        self._reply(unique, struct.pack("<iIII", 0, 0, 0, 0) + out)

    # --- lines
    def _set_out(self, v):
        was = self.ptt
        before = self.lines()
        self.out_lines = v
        self._lines_changed(before)
        if self.ptt != was:
            self.on_ptt(self.ptt)

    def _lines_changed(self, before):
        changed = before ^ self.lines()
        if not changed:
            return
        for i, bit in enumerate((CTS, DSR, RNG, CAR)):
            if changed & bit:
                self.icount[i] += 1
        for unique, mask in list(self.iwait.items()):
            if changed & mask:
                del self.iwait[unique]
                self._reply(unique, struct.pack("<iIII", 0, 0, 0, 0))
