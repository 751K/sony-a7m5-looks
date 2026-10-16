"""Reference port of the chroma stage that consumes tags 0x7841-0x7848.

* FUN_1000a74bc: at parse time, divide gain entries [4..7] of each 8-short array
  by the saturation factor table 0x1002e6950[sat+9] (sat in -9..9).
* FUN_1000ed9b4: choose the effective 8 parameters from the arrays by mode
  (render/dev field +0x1e8); for this file every common mode gives 0x7842+0x7844.
* ZcTaskRGB2YCC::Process 0x1000ee0cc: Y = (c0 R + c1 G + c2 B) >> 13 with
  c = tag 0x7801 << 7; chroma = quadrant-wise shear + gain of (R-G, B-G).
* ZcTaskYCC2RGB::Process 0x1000fb500: fixed BT.601 inverse in 1/10000 integers.

Array layout (8 int16): [0] shear of Cr by (B-G) when B>=G, [1] shear of Cb by (R-G)
when R>=G, [2]/[3] same for the negative side, [4]/[6] Cb gain for Cb>=0/<0,
[5]/[7] Cr gain for Cr>=0/<0. Shear = sign-extended (v>>2)&0x1ff / 256,
gain = ((v>>3)&0xff)/64, chroma additionally scaled by 0.5.
"""
import numpy as np

from sony_look import fma32, _read_va

SAT_TABLE = 0x1002e6950

f32 = np.float32


def i16(v):
    v = int(v) & 0xffff
    return v - 0x10000 if v & 0x8000 else v


def sat_table():
    return np.frombuffer(_read_va(SAT_TABLE, 19 * 8), "<f8").copy()


def descale_gains(arr, sat, table=None):
    """FUN_1000a74bc applied to one 8-short array (returns a new list)."""
    table = sat_table() if table is None else table
    out = [i16(v) for v in arr]
    if ((sat - 10) & 0xffff) >= 0xffed:
        for k in (4, 5, 6, 7):
            out[k] = i16(int(np.trunc(np.float64(out[k]) / table[sat + 9])))
    return out


def _mix(base, sets, weights):
    """base + ((w0*s0 + w1*s1 + w2*s2 + w3*s3) >> 10), int16 wrap-around."""
    out = []
    for k in range(8):
        acc = sum(int(w) * int(s[k]) for w, s in zip(weights, sets)) & 0xffffffff
        out.append(i16(base[k] + i16(acc >> 10)))
    return out


def select_params(look, mode, use_7841=False, sensor_white=0x3fff):
    """FUN_1000ed9b4. `look` maps '0x7841'..'0x7848' to int lists (already descaled)."""
    a41, a42, a43, a44, a45, a46 = (look[t] for t in ("0x7841", "0x7842", "0x7843", "0x7844", "0x7845", "0x7846"))
    w47, w48 = look["0x7847"], look["0x7848"]
    add = lambda extra: [i16(x + y) for x, y in zip(extra, a42)]
    if mode == 1:
        if use_7841:
            return [i16(v) for v in a41]
        return _mix(a42, (a43, a44, a45, a46), w48)
    if 2 <= mode <= 6:
        return add(a44)
    if 7 <= mode <= 10:
        return add(a45)
    if mode == 0xb:
        return add(a43)
    if mode == 0xc:
        nonzero = any(a46[:8])
        if (sensor_white == 0x3fff and nonzero) or (sensor_white != 0x3fff and sensor_white < 0x3233 and nonzero):
            return add(a46)
        return _mix(a42, (a43, a44, a45, a46), w47)
    if mode == 0xd:
        return _mix(a42, (a43, a44, a45, a46), w47)
    return add(a44)


def _shear(v):
    s = (int(v) & 0xffff) >> 2 & 0x1ff
    return float(s) if s < 0x100 else float(((s - 1) ^ 0xfffffe00) + 1 - (1 << 32))


def rgb2ycc(r, g, b, params, luma, clamp=True):
    """r,g,b: int16 planes after MainGamma. Returns (Y uint16, Cr' uint16, Cb' uint16)."""
    r = np.asarray(r, np.int64); g = np.asarray(g, np.int64); b = np.asarray(b, np.int64)
    c0, c1, c2 = (int(v) << 7 & 0xffff for v in luma)
    c0, c1, c2 = (i16(v) for v in (c0, c1, c2))
    y = ((r * c0 + g * c1 + b * c2) & 0xffffffff) >> 13
    y = (y & 0xffff).astype(np.uint16)
    p = [i16(v) for v in params]
    d8, d9, d10, d11 = (_shear(p[1]), _shear(p[3]), _shear(p[0]), _shear(p[2]))
    rg = r - g
    bg = b - g
    s3 = rg.astype(np.float32)
    s4 = bg.astype(np.float32)
    k_cb = np.where(rg < 0, f32(d9 * 0.00390625), f32(d8 * 0.00390625))
    cb = fma32(k_cb, s3, s4)
    k_cr = np.where(bg < 0, f32(d11 * 0.00390625), f32(d10 * 0.00390625))
    cr = fma32(k_cr, s4, s3)
    g_cr = np.where(cr < 0, f32(((p[7] & 0xffff) >> 3 & 0xff) * 0.015625), f32(((p[5] & 0xffff) >> 3 & 0xff) * 0.015625))
    g_cb = np.where(cb < 0, f32(((p[6] & 0xffff) >> 3 & 0xff) * 0.015625), f32(((p[4] & 0xffff) >> 3 & 0xff) * 0.015625))
    cr = ((cr * f32(0.5)).astype(np.float32) * g_cr).astype(np.float32)
    cb = ((cb * f32(0.5)).astype(np.float32) * g_cb).astype(np.float32)
    if clamp:
        cr = np.maximum(np.minimum(cr, f32(8191.0)), f32(-8192.0))
        cb = np.maximum(np.minimum(cb, f32(8191.0)), f32(-8192.0))
    def pack(v):
        i = np.trunc((v + f32(32768.0)).astype(np.float32)).astype(np.int64)
        i = np.minimum(i, 0xffff)
        return np.maximum(i, 0).astype(np.uint16)
    return y, pack(cr), pack(cb)


def _div10000(x):
    q = np.abs(x) // 10000
    return np.where(x < 0, -q, q)


def ycc2rgb(y, cr, cb):
    y = np.asarray(y, np.int64)
    cr = np.asarray(cr, np.int64) - 0x8000
    cb = np.asarray(cb, np.int64) - 0x8000
    clip = lambda v: np.clip(v, 0, 0x3fff).astype(np.int16)
    r = clip(_div10000(cr * 0x36c4 + y * 10000))
    g = clip(_div10000(cr * -0x1be5 + y * 10000 + cb * -0xd71))
    b = clip(_div10000(cb * 0x4538 + y * 10000))
    return r, g, b
