#!/usr/bin/env python3
"""ZcTaskToneCurve: Edit's tone-curve tool (Y / R / G / B).

Settings 0x9000+k hold the x control points of curve k and 0x9004+k the y
points, both on a 0..511 scale (511 = 1.0).  The four curve objects live in the
develop struct at +0x10, +0x68, +0xc0, +0x118 and are used for R, G, B and Y
respectively (see the process function 0x1000fae94).

Sony turns the control points into a smooth cubic Bézier spline
(FUN_1000c3a0c) and samples it into a 65536-entry uint16 LUT
(FUN_1000c4b80, type 0x10).  `build_lut` reproduces both bit for bit; the
process (`apply`) runs the same YCbCr recombination as the original and is
verified against the ARM64 code in analysis/reverse/pixel_pipeline.
"""
from __future__ import annotations

import struct

import numpy as np

from sony_ycc_stages import fma64

# double 1/511 used by FUN_1000aac90; 0x3ddb7cdfd9d7bdbb is the 1e-10 clamp
POINT_SCALE = struct.unpack("<d", struct.pack("<Q", 0x3F60080402010080))[0]
_TINY = struct.unpack("<d", struct.pack("<Q", 0x3DDB7CDFD9D7BDBB))[0]
_ONE_THIRD = struct.unpack("<d", struct.pack("<Q", 0x3FD5555555555555))[0]
_NEG_ONE_THIRD = struct.unpack("<d", struct.pack("<Q", 0xBFD5555555555555))[0]

CHANNELS = ("R", "G", "B", "Y")          # settings pairs 0x9000..0x9003 / 0x9004..0x9007


def _fdiv(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        return float(np.float64(a) / np.float64(b))


def _sqrt(x):
    with np.errstate(invalid="ignore"):
        return float(np.sqrt(np.float64(x)))


def _handles(prev, cur, nxt):
    """Incoming/outgoing Bézier handles at `cur` (FUN_1000c3a0c)."""
    dxi, dyi = prev[0] - cur[0], prev[1] - cur[1]
    dxo, dyo = nxt[0] - cur[0], nxt[1] - cur[1]
    li = _sqrt(fma64(dyi, dyi, dxi * dxi))
    lo = _sqrt(fma64(dyo, dyo, dxo * dxo))
    ro, ri = _fdiv(lo, li), _fdiv(li, lo)
    # FNMSUB: Sn*Sm - Sa
    vx = fma64(ri, dxo, -(ro * dxi))
    vy = fma64(ri, dyo, -(ro * dyi))
    vl = _sqrt(fma64(vy, vy, vx * vx))
    sa = _fdiv(li * _NEG_ONE_THIRD, vl)
    ax, ay = sa * vx, sa * vy
    if abs(ax) > _TINY and abs(ax) > abs(dxi):
        s = _fdiv(abs(dxi), abs(ax))
        ax, ay = ax * s, ay * s
    sb = _fdiv(lo * _ONE_THIRD, vl)
    bx, by = sb * vx, sb * vy
    if abs(bx) > _TINY and abs(bx) > abs(dxo):
        s = _fdiv(abs(dxo), abs(bx))
        bx, by = bx * s, by * s
    return (ax, ay), (bx, by)


def _dense(points):
    """Control polygon with two Bézier handles per segment (FUN_1000c3a0c)."""
    out = [list(points[0]), list(points[0])]
    for i in range(1, len(points) - 1):
        a, b = _handles(points[i - 1], points[i], points[i + 1])
        cur = points[i]
        out.append([cur[0] + a[0], cur[1] + a[1]])
        out.append(list(cur))
        out.append([cur[0] + b[0], cur[1] + b[1]])
    out.append(list(points[-1]))
    out.append(list(points[-1]))
    return out


def control_points(xs, ys, scale=1.0):
    """Replay FUN_1000aac90's curve construction from the two settings arrays."""
    pts = [[0.0, 0.0], [1.0, 1.0]]
    w21 = w27 = 0
    for xv, yv in zip(xs, ys):
        x, y = float(xv) * scale, float(yv) * scale
        if xv == 0x1FF:
            idx = next((i for i in range(len(pts) - 1, -1, -1) if pts[i][0] == 1.0), None)
            if idx is None:
                w21 = 1
            else:
                pts[idx] = [1.0, 1.0] if y == 1.0 else [1.0, y]
                if y == 1.0:
                    w27 = 1
                w21 = 1
        elif x == 0.0:
            pts[0] = [0.0, y]
            w27 = 1
        else:
            # AddPoint keeps the vector sorted by x; duplicate x is not produced
            # by Edit's tone curve (it averages handles instead).
            i = 0
            while i < len(pts) and pts[i][0] < x:
                i += 1
            if i < len(pts) and pts[i][0] == x:
                pts[i] = [x, y]
            else:
                pts.insert(i, [x, y])
    # FUN_1000aac90 removes the constructor's default endpoints when the
    # settings curve did not explicitly contain them.
    i = len(pts) + 1
    while len(pts) > 2:
        idx = i - 2
        if idx < 0:
            break
        x = pts[idx][0]
        if not (w27 or x != 0.0) or not (w21 or x != 1.0):
            del pts[idx]
        i -= 1
        if i < 2:
            break
    return pts


def is_identity(points):
    """FUN_1000c48c0: exactly two points (0,0) and (1,1)."""
    return (len(points) == 2 and points[0][0] == 0.0 and points[0][1] == 0.0
            and points[1][0] == 1.0 and points[1][1] == 1.0)


def _bezier(p0, p1, p2, p3, t):
    """One sample of the cubic Bézier (FUN_1000c4b80 inner loop)."""
    u = t * 0.03125
    om = fma64(-t, 0.03125, 1.0)
    d0 = fma64(-u, om, om)              # (1-u)^2
    c0 = fma64(-u, d0, d0)              # (1-u)^3
    d24 = u * u
    c1 = (t * 0.09375) * d0             # 3u(1-u)^2
    c2 = fma64(-u, 3.0, 3.0) * d24      # 3(1-u)u^2
    c3 = d24 * u                        # u^3
    x = fma64(p1[0], c1, p0[0] * c0)
    x = fma64(p2[0], c2, x)
    x = fma64(p3[0], c3, x)
    y = fma64(p1[1], c1, p0[1] * c0)
    y = fma64(p2[1], c2, y)
    y = fma64(p3[1], c3, y)
    return x, y


def _trunc(x):
    if not np.isfinite(x):
        return 0
    return int(np.trunc(x))


def build_lut(points, n=0x10000):
    """Sample the smooth curve into the uint16 LUT of FUN_1000c4b80."""
    dense = _dense(points)
    lut = np.zeros(n, np.uint16)
    px = py = 0.0
    for seg in range(len(points) - 1):
        p0, p1, p2, p3 = dense[3 * seg:3 * seg + 4]
        for t in range(1, 33):
            x, y = _bezier(p0, p1, p2, p3, t)
            i0 = _trunc(px * 65535.0)
            i1 = _trunc(x * 65535.0)
            if i1 >= i0:
                i0c, i1c = max(i0, 0), min(i1, n - 1)
                if i1c >= i0c:
                    d23 = _fdiv(1.0, (x - px) * 65535.0)
                    for k in range(i1c - i0c + 1):
                        v = fma64((y - py) * float(i0c - i0 + k), d23, py)
                        v *= 65535.0
                        lut[i0c + k] = min(max(_trunc(v), 0), 0xFFFF)
            px, py = x, y
    if px < 1.0:
        i0 = max(_trunc(px * 65535.0), 0)
        if i0 < n:
            lut[i0:] = min(max(_trunc(py * 65535.0), 0), 0xFFFF)
    return lut


def build_from_settings(xs, ys):
    """(points, LUT or None) for one curve; None means identity."""
    pts = control_points(xs, ys, POINT_SCALE)
    if is_identity(pts):
        return pts, None
    return pts, build_lut(pts)


def apply(r, g, b, luts):
    """ZcTaskToneCurve on three uint16 planes 0..16383; luts = (R, G, B, Y)."""
    R = np.asarray(r, np.int64)
    G = np.asarray(g, np.int64)
    B = np.asarray(b, np.int64)
    Y = (R * 0x991 + G * 0x12C9 + B * 0x3A6) >> 11
    C1 = (R * -0x567 + G * -0xA9A + B * 0x1000) >> 11
    C2 = (G * -0xD66 + R * 0x1000 + B * -0x29A) >> 11
    clamp = lambda v: np.minimum(np.where(v < 0, 0, v), 0xFFFF)
    y8 = luts[3][clamp(Y)].astype(np.int64) * 0x2000
    rr = (y8 + C2 * 0x2CDD) >> 13
    gg = (y8 + C1 * -0xB03 + C2 * -0x16DA) >> 13
    bb = (y8 + C1 * 0x38B4 + C2) >> 13
    out = []
    for v, lut in ((rr, luts[0]), (gg, luts[1]), (bb, luts[2])):
        out.append(((lut[clamp(v)].astype(np.int64) >> 2) & 0x3FFF).astype(np.uint16))
    return out
