#!/usr/bin/env python3
"""Read the encrypted Sony SR2SubIFD of an ARW (read-only; same cipher as dcraw's sony_decrypt).

IFD0 tag 0xc634 (DNGPrivateData) -> SR2Private IFD with 0x7200 (offset), 0x7201 (length),
0x7221 (key). The SR2SubIFD block is XOR-decrypted and parsed as a little-endian TIFF IFD whose
value offsets are absolute file offsets.

usage: sr2.py ARW [TAG ...]   (tags in hex, e.g. 0x7984)
"""
import struct
import sys

TYPES = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 6: ("b", 1), 7: ("B", 1), 8: ("h", 2), 9: ("i", 4),
         10: ("ii", 8), 5: ("II", 8), 11: ("f", 4), 12: ("d", 8), 13: ("I", 4)}


def ifd(buf, off, base=0):
    n = struct.unpack_from("<H", buf, off - base)[0]
    out = {}
    for i in range(n):
        tag, typ, cnt, raw = struct.unpack_from("<HHI4s", buf, off - base + 2 + 12 * i)
        fmt, size = TYPES.get(typ, ("B", 1))
        total = size * cnt
        data = raw[:total] if total <= 4 else buf[struct.unpack("<I", raw)[0] - base:][:total]
        if typ == 2:
            val = data.split(b"\0")[0].decode("latin-1")
        elif typ in (5, 10):
            val = list(struct.unpack("<%d%s" % (2 * cnt, fmt[0]), data))
        else:
            val = list(struct.unpack("<%d%s" % (cnt, fmt), data))
        out[tag] = (typ, val, raw)
    return out


def decrypt(block, key):
    pad = [0] * 128
    for p in range(4):
        key = (key * 48828125 + 1) & 0xffffffff
        pad[p] = key
    pad[3] = ((pad[3] << 1) | ((pad[0] ^ pad[2]) >> 31)) & 0xffffffff
    for p in range(4, 127):
        pad[p] = (((pad[p - 4] ^ pad[p - 2]) << 1) | ((pad[p - 3] ^ pad[p - 1]) >> 31)) & 0xffffffff
    pad = [int.from_bytes(v.to_bytes(4, "little"), "big") for v in pad]      # htonl
    words = list(struct.unpack("<%dI" % (len(block) // 4), block[:len(block) // 4 * 4]))
    p = 127
    for i in range(len(words)):
        pad[p & 127] = pad[(p + 1) & 127] ^ pad[(p + 65) & 127]
        words[i] ^= pad[p & 127]
        p += 1
    return struct.pack("<%dI" % len(words), *words) + block[len(words) * 4:]


def _swap(v):
    return int.from_bytes(v.to_bytes(4, "little"), "big")


def sr2_subifd(path):
    data = open(path, "rb").read()
    assert data[:2] == b"II"
    ifd0 = ifd(data, struct.unpack_from("<I", data, 4)[0])
    priv = struct.unpack("<I", ifd0[0xc634][2])[0]
    sr2p = ifd(data, priv)
    off, length = sr2p[0x7200][1][0], sr2p[0x7201][1][0]
    key = struct.unpack("<I", sr2p[0x7221][2])[0]          # dcraw: key = get4() (II order)
    block = decrypt(data[off:off + length], key)
    return ifd(block, off, base=off)


if __name__ == "__main__":
    tags = sr2_subifd(sys.argv[1])
    want = [int(t, 16) for t in sys.argv[2:]] or sorted(tags)
    for t in want:
        if t in tags:
            print(hex(t), tags[t][1])
