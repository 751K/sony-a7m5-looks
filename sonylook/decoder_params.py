#!/usr/bin/env python3
"""ZcARW decoder parameters read straight from the ARW (no Sony parser, no emulator).

These are the fields FUN_1000a107c writes into the ZcARW object; the mappings
come from the disassembly, not from Ghidra's flat C:

  +0x148 storage type   <- raw SubIFD tag 0x115 (SamplesPerPixel)
  +0x14c sample format  <- raw SubIFD tag 0x153 (SampleFormat), default 1
  +0x1f8 ISO            <- Exif 0x8827; if absent/0xffff, SensitivityType
                           0x8830 selects 0x8831 (types 1,4,5,6,7),
                           0x8832 (type 2) or 0x8833 (type 3)
  +0x19c..+0x1bc        <- Sony maker note 0x2004/2005/2006/2032..2036 through
                           FUN_1000a2b74 and five small lookup tables
                           (FUN_1000be0c8/be0f0/be110/be138/be158)
  +0x22780/+0x22784     <- constructor defaults 1.0 (not touched for ARW)

vtable+0xe8 returns +0x1f8 (ISO) and vtable+0xf0 returns (storage == 4), which
is what sony_marble.py calls `iso` and `f0`.
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

_TYPES = {1: ("B", 1), 3: ("H", 2), 4: ("I", 4), 8: ("h", 2), 9: ("i", 4), 13: ("I", 4)}

# Edit 4.0.00.10312 arm64: address, entry count, index of value 0.
_TABLE_SPEC = {
    "be0c8": (0x100237504, 19, -9),   # -45..45 step 5
    "be0f0": (0x1002375dc, 10, 0),    # 0..90 step 10
    "be110": (0x100237550, 19, -9),   # -55..55
    "be138": (0x10023759c, 10, 0),    # -40..50 step 10
    "be158": (0x1002375c4, 6, 0),     # 20..70 step 10
}

_MN_HEADER = b"SONY DSC \x00\x00\x00"


def _ifd(buf: bytes, off: int) -> dict:
    """Parse one TIFF IFD; values stay as (type, count, 4 raw bytes)."""
    n = struct.unpack_from("<H", buf, off)[0]
    out = {}
    for i in range(n):
        tag, typ, cnt, raw = struct.unpack_from("<HHI4s", buf, off + 2 + 12 * i)
        out[tag] = (typ, cnt, raw)
    return out


def _vals(buf: bytes, ent, base: int = 0) -> tuple:
    typ, cnt, raw = ent
    fmt, size = _TYPES.get(typ, ("B", 1))
    total = size * cnt
    data = raw[:total] if total <= 4 else buf[struct.unpack_from("<I", raw)[0] - base:][:total]
    return struct.unpack("<%d%s" % (cnt, fmt), data)


def _get(buf: bytes, ifd: dict, tag: int, default: int = 0, base: int = 0) -> int:
    ent = ifd.get(tag)
    return default if ent is None else _vals(buf, ent, base)[0]


def _raw_ifd(buf: bytes, ifd0: dict) -> dict:
    """The IFD FUN_1000a107c reads 0x115/0x153 from: a SubIFD carrying them."""
    if 0x14A in ifd0:
        for off in _vals(buf, ifd0[0x14A]):
            try:
                sub = _ifd(buf, off)
            except (struct.error, IndexError):
                continue
            if 0x115 in sub or 0x153 in sub:
                return sub
    return ifd0


def _maker_ifd(buf: bytes, exif: dict) -> tuple[dict, int]:
    """Sony maker note. 0x927c points at the IFD entry count, 12 bytes after
    the 'SONY DSC \\0\\0\\0' header; out-of-line values are relative to it."""
    if 0x927C not in exif:
        return {}, 0
    typ, cnt, raw = exif[0x927C]
    _, size = _TYPES.get(typ, ("B", 1))
    if size * cnt <= 4:               # tiny notes are inline; not seen in ARW
        return {}, 0
    off = struct.unpack_from("<I", raw)[0]
    base = off - 12 if buf[off - 12:off] == _MN_HEADER else 0
    return _ifd(buf, off), base


@lru_cache(maxsize=1)
def _tables() -> dict:
    import edit_binary as EB          # reads the user's own Edit installation
    return {name: (struct.unpack_from("<%di" % n, EB.read_va(addr, 4 * n)), base)
            for name, (addr, n, base) in _TABLE_SPEC.items()}


def _lookup(name: str, x: int) -> int:
    tab, base = _tables()[name]
    i = x - base
    return tab[i] if 0 <= i < len(tab) else 0


def decoder_params(arw) -> dict:
    """Return the ZcARW decoder values used by sony_marble / sony_rawnr.

    Keys iso/f0/d14c/d1b0/f22780/f22784 match fullres_nr.DEC; the remaining
    keys expose the rest of the +0x19c.. struct for diagnostics.
    """
    buf = Path(arw).read_bytes()
    ifd0 = _ifd(buf, struct.unpack_from("<I", buf, 4)[0])
    raw = _raw_ifd(buf, ifd0)
    exif = _ifd(buf, _get(buf, ifd0, 0x8769))
    maker, mbase = _maker_ifd(buf, exif)

    storage = _get(buf, raw, 0x115, 0)
    sample_format = _get(buf, raw, 0x153, 1)

    iso = _get(buf, exif, 0x8827, 0xFFFF)
    if iso == 0xFFFF:
        st = _get(buf, exif, 0x8830, 0)
        tag = 0x8831 if st in (1, 4, 5, 6, 7) else 0x8832 if st == 2 else 0x8833 if st == 3 else None
        iso = _get(buf, exif, tag, 0xFFFF) if tag else 0xFFFF

    m = lambda t: _get(buf, maker, t, 0, mbase)
    present = all(t in maker for t in (0x2033, 0x2032, 0x2034, 0x2006, 0x2036, 0x2005, 0x2035))
    out = dict(iso=iso, f0=1 if storage == 4 else 0, d14c=sample_format,
               d1b0=0, f22780=1.0, f22784=1.0,
               storage=storage, mode=2 if present else 1, d1a0=m(0x2004))
    if present:
        out.update(d1a4=_lookup("be0c8", m(0x2033)),
                   d1a8=_lookup("be0c8", m(0x2032)),
                   d1ac=_lookup("be0f0", m(0x2034)),
                   d1b0=_lookup("be0f0", m(0x2036)),
                   d1b4=_lookup("be138", m(0x2006)),
                   d1b8=_lookup("be158", m(0x2035)),
                   d1bc=_lookup("be110", m(0x2005)))
    else:                              # constructor defaults left in place
        out.update(d1a4=0, d1a8=0, d1ac=0, d1b0=0, d1b4=0, d1b8=50, d1bc=0)
    return out


if __name__ == "__main__":
    import json
    import sys
    for p in sys.argv[1:]:
        print(Path(p).name, json.dumps(decoder_params(p), indent=1))
