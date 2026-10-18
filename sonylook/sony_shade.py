#!/usr/bin/env python3
"""ZcTaskShadeCorrection: Edit's Peripheral Illumination correction.

The coefficient table is built by FUN_1000c4ee8 from settings 0x8030 (amount),
0x8031 (central radius) and 0x8032 (central strength); the develop struct keeps
it at +0x170 (11 doubles at +0x18).  The task processes the three RGB planes
after ToneCurve (pipeline order: YCC2RGB -> Marble -> Effect -> ToneCurve ->
ShadeCorrection).

Gain at a pixel (FUN_1000c5410): normalised distance from the image centre,
sampled from the 11 values (0.1 radius steps, linear interpolation).  The
original derives the centre size from the render mode and, when settings
0x900e == 1, applies a focal-length/projection scale (FUN_1000aedb8); both are
reproduced here.

The task also rebuilds RGB around the corrected luminance (same YCbCr
coefficients as ToneCurve) and clamps to 14 bits.
"""
from __future__ import annotations

import math
import struct

import numpy as np

from sony_ycc_stages import fma64

# constants from FUN_1000c4ee8
_C_AMOUNT = struct.unpack("<d", struct.pack("<Q", 0x3F847AE147AE147B))[0]   # 0.01
_C_ANGLE = struct.unpack("<d", struct.pack("<Q", 0x3F1496B7C54245B6))[0]   # 7.853981634500001e-05
_C_ANGLE_I = struct.unpack("<d", struct.pack("<Q", 0x3F49BC65B692D724))[0]  # 0.0007853981634500001
_C_TENTH = struct.unpack("<d", struct.pack("<Q", 0x3FB999999999999A))[0]   # 0.1
_C_PER_MILLI_DEG = struct.unpack("<d", struct.pack("<Q", 0x3EF24D1521193474))[0]
_PI = math.pi


def _fma(a, b, c):
    return fma64(a, b, c)


def coefficients(amount, radius, strength):
    """11 radial gains at 0.1 steps (FUN_1000c4ee8)."""
    d8 = float(amount) * _C_AMOUNT
    c = math.cos(float(amount) * _C_ANGLE * float(radius))
    c2 = c * c
    c4 = c2 * c2
    d9 = (float(strength) * _C_AMOUNT) * c4
    d10 = fma64(c2, c2, -d9)          # FNMSUB: c²*c² - d9, one rounding
    d11 = float(radius) * _C_ANGLE_I
    with np.errstate(divide="ignore"):
        d12 = float(np.float64(1.0) / (np.float64(1.0) - float(amount) * _C_AMOUNT))
    out = []
    for i in range(11):
        d0 = float(i) * _C_TENTH
        if d0 < d8:
            c = math.cos(d11 * float(i))
            out.append((c * c) * (c * c))
        else:
            if amount == 100:
                out.append(d10)
            else:
                out.append(_fma(_fma(-d0, d9, d9), d12, d10))
    return out


def _angle_scale(flag, angle_units, w, h):
    """FUN_1000aedb8: projection scale for the peripheral model."""
    if flag != 1:
        return 1.0
    d12 = float(angle_units) * _C_PER_MILLI_DEG
    d11 = float(w) * 0.5
    d13 = float(h) * 0.5
    d10 = math.sqrt(_fma(d13, d13, d11 * d11))
    d9 = math.atan(d13 / d11)
    d8 = math.atan(d11 / d13)
    s2d8 = d8 + d8
    if d12 > 0.0 and d12 < s2d8:
        d14 = d13 / math.sin(d12 + d9)
    else:
        d1 = -2.0 * d9
        if d1 < d12 and d12 < 0.0:
            d14 = d11 / math.sin(d12 + d9)
        elif s2d8 > d12 or d12 > _PI:
            d14 = d10
        else:
            d14 = d11 / math.sin(d12 + d9)
    return _second_half(d12, d8, d9, d11, d13, d10, d14)


def _second_half(d12, d8, d9, d11, d13, d10, d14):
    d0 = -2.0 * d8
    d1 = 2.0 * d9
    if d0 < d12 and d12 < 0.0:
        v = d13 / math.cos(d12 + d8)
    elif d1 <= d12 and d12 <= _PI:
        v = d13 / math.cos(d12 + d8)
    elif d12 <= 0.0:
        v = d10 if (d12 < -_PI or d12 > d0) else d11 / math.sin(d12 + d8)
    elif d12 < d1:
        v = d11 / math.sin(d12 + d8)
    else:
        v = d10 if (d12 < -_PI or d12 > d0) else d11 / math.sin(d12 + d8)
    return min(d14, v) / d10


def gains(coeffs, w, h, width, height, offx=0, offy=0):
    """Per-pixel gain array for a `height` x `width` region."""
    xs = np.arange(width, dtype=np.float64) + float(offx)
    ys = np.arange(height, dtype=np.float64) + float(offy)
    cx, cy = float(w) * 0.5, float(h) * 0.5
    rmax2 = _fma(cy, cy, cx * cx)
    dx = cx - xs[None, :]
    dy = cy - ys[:, None]
    r = np.sqrt((dx * dx + dy * dy) / rmax2)
    t = r * 10.0
    idx = np.trunc(t).astype(np.int64)
    idx_c = np.clip(idx, 0, 10)
    frac = t - idx
    lo = np.asarray(coeffs, np.float64)[idx_c]
    hi = np.asarray(coeffs, np.float64)[np.minimum(idx_c + 1, 10)]
    g = lo + frac * (hi - lo)
    return np.where(idx >= 10, float(coeffs[10]), g)


def apply(r, g, b, coeffs, w, h, flag=0, angle=0, offx=0, offy=0,
          scale_w=None, scale_h=None):
    """ShadeCorrection on three uint16 planes 0..16383.

    w/h are the centre dimensions (the region size in render mode 2, otherwise
    the image size); scale_w/scale_h are the image rect used by the projection
    scale when settings 0x900e == 1 (they default to w/h).
    """
    if flag == 1:
        sw = w if scale_w is None else scale_w
        sh = h if scale_h is None else scale_h
        s = _angle_scale(1, angle, sw, sh)
        w, h = int(w / s), int(h / s)
    gain = gains(coeffs, w, h, r.shape[1], r.shape[0], offx, offy)
    if np.all(gain == 1.0):
        return r, g, b
    R = np.asarray(r, np.int64)
    G = np.asarray(g, np.int64)
    B = np.asarray(b, np.int64)
    Y = (R * 0x991 + G * 0x12C9 + B * 0x3A6) >> 11
    with np.errstate(divide="ignore", invalid="ignore"):
        q = Y / gain
    # fcvtzs: NaN -> 0, ±inf saturates to int32, then the task clamps to u16.
    q = np.where(np.isnan(q), 0.0, q)
    q = np.clip(q, -2147483648.0, 2147483647.0)
    Yq = np.trunc(q).astype(np.int64)
    Yq = np.minimum(np.maximum(Yq, 0), 0xFFFF)
    C1 = (R * -0x567 + G * -0xA9A + B * 0x1000) >> 11
    C2 = (G * -0xD66 + R * 0x1000 + B * -0x29A) >> 11
    Y8 = Yq * 0x2000
    # The task leaves a pixel untouched when its gain is exactly 1.0; the
    # YCC round trip would otherwise truncate it by a unit.
    keep = gain == 1.0
    out = []
    for v, src in (((Y8 + C2 * 0x2CDD) >> 15, R),
                   ((Y8 + C1 * -0xB03 + C2 * -0x16DA) >> 15, G),
                   ((Y8 + C1 * 0x38B4 + C2) >> 15, B)):
        v = np.where(keep, src, np.minimum(np.maximum(v, 0), 0x3FFF))
        out.append(v.astype(np.uint16))
    return out
