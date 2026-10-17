#!/usr/bin/env python3
"""Develop settings Edit appends to an exported ARW (not in the camera's original file).

At the end of an Edit-saved ARW there is a small TIFF-like block (1630 bytes for
4.0.00.10312) whose first IFD has three entries:

    0xd000  LONG 1  format version (1)
    0xd001  LONG 2  offset (relative to the block) of two uint32 IFD offsets
    0xd002  LONG 1  ...

The two IFDs are the "current" and "initial" settings.  Each entry is
{setting id, type, count, value}; the ids are what FUN_1000aac90 reads, and the
serializer is around 0x10010ebxx (type 3 = SHORT, 9 = SLONG, 4 = LONG).

read_settings(arw) returns the current settings as {id: int | tuple | str};
an original (unsaved) ARW returns {} and callers keep their defaults.
"""
from __future__ import annotations

import struct
from pathlib import Path

_SIG = (struct.pack("<H", 3) + struct.pack("<HHII", 0xD000, 4, 1, 1)
        + struct.pack("<HHI", 0xD001, 4, 2))
_TYPES = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8),
          6: ("b", 1), 7: ("B", 1), 8: ("h", 2), 9: ("i", 4), 10: ("ii", 8),
          11: ("f", 4), 12: ("d", 8), 13: ("I", 4)}

# Common ids (see docs/pipeline.md §10.7)
WB_MODE, WB_TEMPERATURE, WB_TINT, RB_FACTOR = 0x8002, 0x8013, 0x8014, 0x8015
EXPOSURE, NR_ON, DRO_ON, CLARITY = 0x8018, 0x801E, 0x8022, 0x901C
NR_STRENGTH = 0x8029                      # dev+0x224 (Marble d224)
DISTORTION, CA_CORRECTION = 0x8040, 0x900D
PICKER_BOX, PICKER_USE_GAINS, PICKER_GAINS = 0x8021, 0x9015, 0x9016


def _ifd(data: bytes, base: int, off: int) -> dict:
    start = base + off
    n = struct.unpack_from("<H", data, start)[0]
    if n > 200:
        raise ValueError("implausible settings IFD")
    out = {}
    for i in range(n):
        tag, typ, cnt, val = struct.unpack_from("<HHII", data, start + 2 + 12 * i)
        fmt, size = _TYPES.get(typ, ("B", 1))
        total = size * cnt
        if total <= 4:
            raw = struct.pack("<I", val)[:total]
        else:                             # out-of-line values are relative to the IFD start
            raw = data[start + val:start + val + total]
        if typ == 2:
            out[tag] = raw.split(b"\0")[0].decode("latin-1")
        elif typ in (5, 10):
            out[tag] = struct.unpack("<%d%s" % (2 * cnt, fmt[0]), raw)
        else:
            vals = struct.unpack("<%d%s" % (cnt, fmt), raw)
            out[tag] = vals[0] if cnt == 1 else vals
    return out


def _block_base(data: bytes) -> int | None:
    pos = len(data)
    while True:
        pos = data.rfind(_SIG, 0, pos)
        if pos < 0:
            return None
        try:
            off2 = struct.unpack_from("<I", data, pos + 22)[0]
            cur, init = struct.unpack_from("<2I", data, pos + off2)
            if 0 < cur < len(data) - pos and 0 < init < len(data) - pos:
                _ifd(data, pos, cur)
                _ifd(data, pos, init)
                return pos
        except (struct.error, IndexError, ValueError):
            pass


def read_settings_pair(arw) -> tuple[dict, dict]:
    """(current, initial) settings from an Edit-saved ARW; ({}, {}) when absent."""
    data = Path(arw).read_bytes()
    base = _block_base(data)
    if base is None:
        return {}, {}
    off2 = struct.unpack_from("<I", data, base + 22)[0]
    cur, init = struct.unpack_from("<2I", data, base + off2)
    return _ifd(data, base, cur), _ifd(data, base, init)


def read_settings(arw) -> dict:
    """Current develop settings; {} for an ARW the camera wrote itself."""
    return read_settings_pair(arw)[0]


def marble_dev(settings) -> dict:
    """`dev` dict for sony_marble.marble: NR on (d218), strength (d224), clarity (d2c0)."""
    return dict(d218=int(settings.get(NR_ON, 0)),
                d224=int(settings.get(NR_STRENGTH, 0)),
                d2c0=int(settings.get(CLARITY, 0)))


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        cur, init = read_settings_pair(p)
        print(Path(p).name, len(cur), "settings")
        for k in sorted(cur):
            print(f"  {k:#06x} = {cur[k]}")
