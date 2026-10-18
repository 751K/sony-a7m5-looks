#!/usr/bin/env python3
"""Develop settings Edit appends to an exported ARW (not in the camera's original file).

At the end of an Edit-saved ARW there is a small TIFF-like block (1630 bytes for
4.0.00.10312) whose first IFD has three entries:

    0xd000  LONG 1  format version (1)
    0xd001  LONG 2  offset (relative to the block) of two uint32 IFD offsets
    0xd002  LONG 1  ...

The two IFDs are two snapshots.  The first (offset order) carries the capture
timestamp (its 0xd100 string) and the camera-side settings; the second has the
later Edit-session timestamp and is the state Edit used for the export -- the
reference TIFFs match it (NR off, DRO off, CA off), not the first.  Each entry
is {setting id, type, count, value}; the ids are what FUN_1000aac90 reads, and
the serializer is around 0x10010ebxx (type 3 = SHORT, 9 = SLONG, 4 = LONG).

read_settings(arw) returns the Edit-session settings (second IFD) as
{id: int | tuple | str}; an original (unsaved) ARW returns {} and callers keep
their defaults.  read_settings_pair() returns (camera, edit).
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

# Creative Look sliders.  FUN_1000aac90 maps each id to a dev field; the names are
# the key strings Edit's own settings loader uses (0x1001f52xx..0x1001f5dxx):
#   Saturation  -> +0x1fc (HueSaturation, 1 + 0.01*s)
#   Contrast    -> +0x200 (MainGamma curve family index)
#   Hue         -> +0x204 (HueSaturation, 0.35/0.7 deg per step)
#   BlackLevel  -> +0x2ac, WhiteLevel -> +0x2b0 (YGamma knee and slope)
#   HighlightLevel -> +0x2b4, ShadowLevel -> +0x2b8 (MainGamma curve family halves)
#   Fineness    -> +0x2c4 (Sharpness)
SATURATION, CONTRAST, HUE = 0x8016, 0x8017, 0x8019
BLACK_LEVEL, WHITE_LEVEL = 0x9018, 0x9017
HIGHLIGHT_LEVEL, SHADOW_LEVEL = 0x9019, 0x901A
SHARPNESS_FINENESS = 0x901D
SHARPNESS_GAIN, SHARPNESS_OVERSHOOT = 0x801A, 0x801B
SHARPNESS_UNDERSHOOT, SHARPNESS_THRESHOLD = 0x801C, 0x801D

# Tone curve tool: curve k (R, G, B, Y) uses x points 0x9000+k and y points
# 0x9004+k, both on a 0..511 scale.
TONE_CURVE_X = (0x9000, 0x9001, 0x9002, 0x9003)     # R, G, B, Y
TONE_CURVE_Y = (0x9004, 0x9005, 0x9006, 0x9007)
# ShadeCorrection / peripheral illumination.
SHADE_AMOUNT, SHADE_RADIUS, SHADE_STRENGTH = 0x8030, 0x8031, 0x8032
SHADE_FLAG, SHADE_ANGLE = 0x900E, 0x900F


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
    """(camera, edit) snapshots from an Edit-saved ARW; ({}, {}) when absent.

    The camera snapshot has the capture-time 0xd100 timestamp; the edit snapshot
    is the later Edit session and matches what the export used.
    """
    data = Path(arw).read_bytes()
    base = _block_base(data)
    if base is None:
        return {}, {}
    off2 = struct.unpack_from("<I", data, base + 22)[0]
    cam, edit = struct.unpack_from("<2I", data, base + off2)
    return _ifd(data, base, cam), _ifd(data, base, edit)


def read_settings(arw) -> dict:
    """Edit-session develop settings; {} for an ARW the camera wrote itself."""
    cam, edit = read_settings_pair(arw)
    return edit if edit else cam


LOOK_SELECT = 0x8000                      # dev+0x1e0: 1 = as shot, else a look id (vtable+0xe0)
DRO_LEVEL, FADE = 0x8023, 0x901B          # dev+0x23c, dev+0x2bc (10 x Fade)


def _lens_switch_default(h):
    """0x1000a7100: a lens tag's u16 -> dev+0x278/+0x27c (-1 auto, 1 on, 2 off, 0 none)."""
    h &= 0xFFFF
    if h >= 0x100:
        return 0
    lo = h & 0xFF
    return -1 if lo == 0x11 else 2 if lo == 0 else 1


def camera_defaults(arw) -> dict:
    """The settings Edit 4.0 starts from for an ARW it has never saved (FUN_1000a6e9c).

    Same ids as read_settings().  Only the entries the decoder uses are filled:
      0x8000 = 1 (as shot); 0x8002 = 1 (as-shot WB);
      0x801e = NR: SR2 0x78a0 != 0 (look+0x1016), else 0x78a1 != 0 (look+0x1017);
      0x8022/0x8023 = DRO from MakerNote 0xb025 (format codes 0x3400..0x3ffe, i.e. the A7M5):
               2 or 3 (auto) -> 1, 0 -> 0, anything else -> 2 with level 15*v - 215;
      0x8040 = distortion from SR2 0x7983 (look+0x12fc/0x12fe, else 0x7981 at look+0x12d0);
      0x900d = CA from SR2 0x797f (look+0x12d2); both through _lens_switch_default and
               zero when the lens table count (look+0x12c0, here 0x7982) is absent;
      0x901c = clarity, 0x901b = fade: Edit's per-look default table (vtable+0x178).
    The camera snapshot Edit writes into a saved ARW has these same values (DSC03591).
    """
    import struct
    import arw_looks as AL
    import decoder_params as DP
    import sony_marble as MB
    import sony_render as R
    import sr2

    tags = {k: (v[1] if isinstance(v[1], list) else [v[1]]) for k, v in sr2.sr2_subifd(str(arw)).items()}
    one = lambda t, d=0: int(tags[t][0]) if t in tags else d

    buf = Path(arw).read_bytes()
    ifd0 = DP._ifd(buf, struct.unpack_from("<I", buf, 4)[0])
    exif = DP._ifd(buf, DP._get(buf, ifd0, 0x8769))
    maker, mbase = DP._maker_ifd(buf, exif)
    dro_tag = DP._get(buf, maker, 0xB025, 0, mbase)
    if dro_tag in (2, 3):
        dro, level = 1, 0
    elif dro_tag == 0:
        dro, level = 0, 0
    else:
        dro, level = 2, 15 * dro_tag - 215

    nr = 1 if one(0x78A0) else (1 if one(0x78A1) else 0)

    if 0x7982 in tags:                                   # look+0x12c0 != 0
        m = tags.get(0x7983, [-1, -1])
        if m[0] != -1:
            src = m[0]
        elif len(m) > 1 and m[1] != -1:
            src = m[0]
        else:
            src = one(0x7981, 0)
        dist, ca = _lens_switch_default(src), _lens_switch_default(one(0x797F, 0))
    else:
        dist, ca = 0, 0

    name = str(AL.main_tags(str(arw)).get("0x7770", "Standard")).strip()
    look_id = R.LOOK_IDS.get(name, 0)
    fade = 10 * R.CAMERA_FADE.get(name, 0) if look_id not in (0x16, 0x17) else 0
    return {LOOK_SELECT: 1, WB_MODE: 1, NR_ON: nr, DRO_ON: dro, DRO_LEVEL: level,
            DISTORTION: dist, CA_CORRECTION: ca, CLARITY: MB.clarity_default(look_id), FADE: fade,
            SHARPNESS_FINENESS: 50, SHADE_AMOUNT: 80, SHADE_RADIUS: 0, SHADE_STRENGTH: 0,
            SHADE_FLAG: 0, SHADE_ANGLE: 0}


def effective_settings(arw) -> dict:
    """read_settings() for an Edit-saved ARW, else camera_defaults()."""
    st = read_settings(arw)
    return st if st else camera_defaults(arw)


def _s32(v) -> int:
    """A 32-bit setting as Edit's consumers read it (`*(int *)(dev+off)`).

    The block stores these as LONG or SLONG; read_settings() honours the type, so a
    tampered LONG 0xFFFFFFFB arrives as 4294967291 here but is -5 to Edit.
    """
    v = int(v) & 0xFFFFFFFF
    return v - (1 << 32) if v >= (1 << 31) else v


def fade_dev(settings) -> int:
    """dev+0x2bc (setting 0x901b, 10 x the 0..9 Fade slider) as Edit sees it.

    Like Clarity, the setting is copied into the dev struct unchecked (FUN_1000aac90)
    and consumed as a YGamma level.  Every level >= 0 is deterministic in Edit
    (non-multiples of 10 interpolate, > 90 extrapolates with q = 8; verify_ycc_stages
    checks 15, 37, 95 and 200 against the emulator), so those pass through unchanged.
    A negative level makes FUN_1000d71fc's q = level / 10 negative and read below its
    10-entry gain table -- undefined in Edit -- and raises ValueError.
    """
    level = _s32(settings.get(FADE, 0))
    if level < 0:
        raise ValueError(f"Fade level {level} (setting 0x901b) < 0: not defined in Edit")
    return level


def marble_dev(settings) -> dict:
    """`dev` dict for sony_marble.marble: NR on (d218), strength (d224), clarity (d2c0).

    Edit's slider is 0..100 (GfDocument::ModifyClarity clamps before it writes dev+0x2c0),
    but the settings block is copied into the dev struct without a check (FUN_1000aac90:
    get 0x901c -> str dev+0x2c0), so a saved value reaches Marble as is.  0..109 indexes
    the 11-level strength tables (q = lum / 10) and is reproduced exactly; a negative
    value or >= 110 reads outside them in Edit (undefined) and raises ValueError.
    """
    clarity = _s32(settings.get(CLARITY, 0))
    if not 0 <= clarity <= 109:
        raise ValueError(f"Clarity {clarity} (setting 0x901c) outside 0..109: not defined in Edit")
    return dict(d218=int(settings.get(NR_ON, 0)),
                d224=int(settings.get(NR_STRENGTH, 0)),
                d2c0=clarity)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        cur, init = read_settings_pair(p)
        print(Path(p).name, len(cur), "settings")
        for k in sorted(cur):
            print(f"  {k:#06x} = {cur[k]}")
