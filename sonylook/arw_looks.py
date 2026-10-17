#!/usr/bin/env python3
"""Creative Look parameters and front-end tags from a Sony ARW (A7M5, ARW 6.0), read only.

The camera stores every Creative Look as an IFD inside the encrypted SR2SubIFD (sr2.py: the
dcraw cipher); SR2SubIFD tag 0x74c0 lists their offsets and tag 0x7770 names each one.
usage: arw_looks.py ARW [OUT.json]
"""
import base64
import json
import struct
import sys
from functools import lru_cache

import sr2


@lru_cache(maxsize=4)
def sr2_block(path):
    data = open(path, "rb").read()
    assert data[:2] == b"II", "not a little-endian TIFF/ARW"
    ifd0 = sr2.ifd(data, struct.unpack_from("<I", data, 4)[0])
    priv = struct.unpack("<I", ifd0[0xc634][2])[0]
    sp = sr2.ifd(data, priv)
    off, length = sp[0x7200][1][0], sp[0x7201][1][0]
    key = struct.unpack("<I", sp[0x7221][2])[0]
    blk = sr2.decrypt(data[off:off + length], key)
    return blk, off, sr2.ifd(blk, off, base=off)


def _fmt(typ, val):
    if typ == 7:
        return "base64:" + base64.b64encode(bytes(val)).decode()
    if typ == 2:
        return val
    return val[0] if len(val) == 1 else " ".join(str(v) for v in val)


def main_tags(path):
    """SR2SubIFD tags as {'0x7310': '512 512 512 512', ...} (strings / ints, like the look IFDs)."""
    _, _, sub = sr2_block(str(path))
    return {f"0x{k:04x}": _fmt(*sub[k][:2]) for k in sorted(sub)}


def sony_model_id(path):
    """MakerNote 0xb001 SonyModelID (A7M5: 407); Edit keeps it at decoder+0xc / look+0x2c."""
    data = open(path, "rb").read()
    ifd0 = sr2.ifd(data, struct.unpack_from("<I", data, 4)[0])
    exif = sr2.ifd(data, struct.unpack("<I", ifd0[0x8769][2])[0])
    mn = struct.unpack("<I", exif[0x927c][2])[0]
    note = sr2.ifd(data, mn + 12 if data[mn:mn + 4] == b"SONY" else mn)
    return note[0xb001][1][0]


def look_ifds(path):
    """{ColorMode name: {'0x7842': ..., ..., 'ColorMode': name}} for every look stored in the file."""
    blk, off, sub = sr2_block(str(path))
    out = {}
    for o in sub[0x74c0][1]:
        t = sr2.ifd(blk, o, base=off)
        g = {f"0x{k:04x}": _fmt(*t[k][:2]) for k in sorted(t) if k != 0x7770}
        g["ColorMode"] = t[0x7770][1]
        out[g["ColorMode"]] = g
    return out


if __name__ == "__main__":
    looks = look_ifds(sys.argv[1])
    print(", ".join(looks))
    if len(sys.argv) > 2:
        json.dump(looks, open(sys.argv[2], "w"), indent=1)
