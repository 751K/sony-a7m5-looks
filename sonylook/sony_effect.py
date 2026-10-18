#!/usr/bin/env python3
"""Reference port of ZcTaskEffect (Imaging Edge Edit 4.0.00.10312), the BW / Sepia stage.

Builder FUN_1000b85e0 inserts it after YCC2RGB (and Marble) when look+0x34 & ~1 == 0x10, i.e.
look id 0x10 (BW) or 0x11 (Sepia). Process = 0x10013a9b8 (vtable 0x1002c6c70 slot 7).

    1. render+0x44 == 0: FUN_1000dd41c   sRGB-encoded RGB -> gamma-2.2 working RGB
       render+0x44 == 1: FUN_1000dd7cc   gamma-2.2 RGB    -> gamma-2.2 working RGB
    2. BW (0x10): R = G = B = (2449 R + 4809 G + 934 B) >> 13          (BT.601, /8192)
       Sepia (0x11): luma L = BT.709 (double) plus a tint (tR, tB) that fades out in the
       deep shadows and near white; G is pulled down by 0.509 tR + 0.194 tB.
    3. Sepia only: FUN_1000dd5f4 (mode 0) / FUN_1000dd9a4 (mode 1) back to the output space.
       BW skips the way back: its grey stays in the working encoding.

Tint: look+0x9a (R) / look+0x98 (B) << 6. Both come from SR2SubIFD tag 0x74c5 (int16[2] = B, R;
FUN_1000a31c8; the look IFDs do not carry it), unless SonyModelID (MakerNote 0xb001,
look+0x2c) is < 3 or one of 0x100, 0x102..0x105, which use the built-in (-1152, 832).
A7M5: SonyModelID 407, 0x74c5 = -15 14 -> tB = -960, tR = +896.

The four 1D tables come from FUN_1000db9f4 (libm powf, as in the binary).
"""
import ctypes
from functools import lru_cache

import numpy as np

from sony_look import fma32

_libm = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libm.powf.argtypes = (ctypes.c_float, ctypes.c_float)
_libm.powf.restype = ctypes.c_float

f32 = np.float32
LOOK_BW, LOOK_SEPIA = 0x10, 0x11

# 3x3 matrices (Q13) of the colour-space helpers, rows = output R, G, B
M_FWD = {0: ((0x13b2, 0xca2, -0x54), (0x2ec, 0x1abd, 599), (0x73, 0x253, 0x1d39)),      # dd41c
         1: ((0x1b89, 0x4ce, -0x57), (0x416, 0x197a, 0x270), (0xa1, 0xe4, 0x1e7b))}     # dd7cc
M_INV = {0: ((0x37e2, -0x1aa4, 0x2c3), (-0x613, 0x2976, -0x363), (-0x60, -0x2e3, 0x2345)),  # dd5f4
         1: ((0x263e, -0x73f, 0x102), (-0x613, 0x2975, -0x362), (-0x9d, -0x110, 0x21ac))}    # dd9a4


def _bits(u):
    return np.array([u], np.uint32).view(np.float32)[0]


def _q14(x):
    """fcvtzs w, s, #14 / fcvtzs after *16384, then the int16 saturation of FUN_1000db9f4."""
    v = int(np.trunc(np.float64(f32(x)) * 16384.0)) if np.isfinite(x) else 0
    return min(v, 0x7fff)


@lru_cache(maxsize=1)
def tables():
    """{name: int16[16384]} for input codes 0..16383 (FUN_1000db9f4 entries 0x8000..0xbfff)."""
    try:
        import fused_rust as FR
        if FR.available():
            return FR.effect_tables()
    except Exception:
        pass
    g_dec, g_enc, s_enc, s_dec = (np.zeros(16384, np.int16) for _ in range(4))
    k_g, k_ge = _bits(0x400cc000), _bits(0x3ee8cf59)          # 2.19921875, 1/2.19921875
    k_s = _bits(0x3ed55555)                                     # 1/2.4
    for v in range(16384):
        x = f32(v) * f32(6.1035156e-05)
        if x > 0:
            g_dec[v] = _q14(f32(_libm.powf(x, k_g)))
            g_enc[v] = _q14(f32(_libm.powf(x, k_ge)))
        if x > f32(0.0031308):
            s_enc[v] = _q14(fma32(f32(_libm.powf(x, k_s)), _bits(0x3f870a3d), _bits(0xbd6147ae)))
        else:
            s_enc[v] = _q14(f32(v) * _bits(0x3a4eb852))
        if x > f32(0.04045):
            s_dec[v] = _q14(f32(_libm.powf(fma32(f32(v), _bits(0x3872a76f), _bits(0x3d55891a)), _bits(0x4019999a))))
        else:
            s_dec[v] = _q14(f32(v) * _bits(0x369e8391))
    return dict(gamma_dec=g_dec, gamma_enc=g_enc, srgb_enc=s_enc, srgb_dec=s_dec)


def _two_sum(a, b):
    s = a + b
    bb = s - a
    return s, (a - (s - bb)) + (b - bb)


def _fma64(a, b, c):
    """Vectorised double fmadd: a*b split exactly (Dekker), then one rounding of ph + pl + c."""
    a, b, c = (np.asarray(x, np.float64) for x in (a, b, c))
    ph = a * b
    sp = 134217729.0
    ah = a * sp; ah = ah - (ah - a); al = a - ah
    bh = b * sp; bh = bh - (bh - b); bl = b - bh
    pl = ((ah * bh - ph) + ah * bl + al * bh) + al * bl
    s, e = _two_sum(ph, c)
    return s + (e + pl)


def _convert(planes, dec, m, enc):
    """FUN_1000dd41c family: s = dec[p]; o = (m . s) >> 13 (asr); clamp 0..16383; enc[o]."""
    s = [dec[np.asarray(p, np.int64)].astype(np.int64) for p in planes]
    out = []
    for row in m:
        o = (row[0] * s[0] + row[1] * s[1] + row[2] * s[2]) >> 13
        out.append(enc[np.clip(o, 0, 0x3fff)].astype(np.uint16))
    return out


def tint(model_id, t74c5):
    """(tR, tB) floats; t74c5 = SR2SubIFD 0x74c5 int16 pair (B, R)."""
    if (model_id - 0x100 < 6 and model_id != 0x101 and model_id >= 0x100) or model_id < 3:
        return f32(832.0), f32(-1152.0)
    return f32(int(t74c5[1]) << 6), f32(int(t74c5[0]) << 6)


def effect_apply(r, g, b, look_id, tr_tb=None, mode=0):
    """r, g, b: uint16 planes 0..16383 out of YCC2RGB (ARM64 order). Returns three uint16 planes."""
    if look_id & ~1 != 0x10:
        return r, g, b
    T = tables()
    if mode == 0:
        r, g, b = _convert((r, g, b), T["srgb_dec"], M_FWD[0], T["gamma_enc"])
    elif mode == 1:
        r, g, b = _convert((r, g, b), T["gamma_dec"], M_FWD[1], T["gamma_enc"])
    R, G, B = (np.asarray(p, np.int64) for p in (r, g, b))
    s = R * 0x991 + G * 0x12c9 + B * 0x3a6
    if look_id == LOOK_BW:
        y = np.minimum(s >> 13, 0x3fff).astype(np.uint16)
        return y, y.copy(), y.copy()
    tR, tB = tr_tb
    y13 = (s >> 13).astype(np.float32)
    lo = (y13 * f32(0.00074404763)).astype(np.float32)               # Y / 1344
    hi = fma32(y13, _bits(0xba3a2e8c), _bits(0x4139745d))            # (16320 - Y) / 1408
    wr = np.where((s >> 19) > 0x14, tR, (lo * tR).astype(np.float32))
    wb = np.where((s >> 19) > 0x14, tB, (lo * tB).astype(np.float32))
    wr = np.where(s < 0x7482000, wr, (hi * tR).astype(np.float32)).astype(np.float32)
    wb = np.where(s < 0x7482000, wb, (hi * tB).astype(np.float32)).astype(np.float32)
    d = R.astype(np.float64) * 0.2126
    d = _fma64(G, 0.7152, d)
    d = _fma64(B, 0.0722, d)
    L = np.fmax(np.fmin(d.astype(np.float32), f32(16383.0)), f32(0.0)).astype(np.float32)
    ro = np.trunc((wr + L).astype(np.float32)).astype(np.int64)
    bo = np.trunc((wb + L).astype(np.float32)).astype(np.int64)
    go = np.trunc(L).astype(np.int64) - ((np.trunc(wr).astype(np.int64) * 0x104c + np.trunc(wb).astype(np.int64) * 0x636) >> 13)
    cl = lambda v: np.clip(v, 0, 0x3fff).astype(np.uint16)
    out = [cl(ro), cl(go), cl(bo)]
    if mode == 0:
        out = _convert(out, T["gamma_dec"], M_INV[0], T["srgb_enc"])
    elif mode == 1:
        out = _convert(out, T["gamma_dec"], M_INV[1], T["gamma_enc"])
    return tuple(out)
