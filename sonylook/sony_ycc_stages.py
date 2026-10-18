"""Reference ports of the YCC-domain stages of Imaging Edge Edit 4.0.00.10312.

* YGamma LUT builder FUN_1000c7120 (+ curve resampler FUN_1000c6e28) and the
  offset/gain helpers FUN_1000d70e0 / FUN_1000d71fc (tags 0x780b-0x780e).
* ZcTaskYGamma::Process 0x1000feb68.
* ZcTaskChromaSuppres::Process 0x1000e6da0.
* ZcTaskHueSaturation::Process 0x1000e5840 (+ atan approximation 0x1000e53ac,
  sin table initialised by 0x1000e56e0).
* ZcTask3DLut::Process 0x10013db80 (apply only; LUT contents built elsewhere).

Integer helpers follow ARM64 semantics (sdiv truncates, x/0 == 0).
"""
import math
import struct

import numpy as np

from sony_look import fma32, _read_va

f32 = np.float32


def i16(v):
    v = int(v) & 0xffff
    return v - 0x10000 if v & 0x8000 else v


def i32(v):
    v = int(v) & 0xffffffff
    return v - (1 << 32) if v & 0x80000000 else v


def sdiv(a, b):
    a, b = i32(a), i32(b)
    if b == 0:
        return 0
    q = abs(a) // abs(b)
    return i32(q if (a >= 0) == (b > 0) else -q)


def fcvtzs(x):
    """float -> int32 with ARM saturation, NaN -> 0."""
    x = float(x)
    if math.isnan(x):
        return 0
    if x >= 2147483647.0:
        return 2147483647
    if x <= -2147483648.0:
        return -2147483648
    return int(math.trunc(x))


import ctypes as _ct
_libm = _ct.CDLL("/usr/lib/libSystem.B.dylib")
_libm.fma.argtypes = (_ct.c_double, _ct.c_double, _ct.c_double)
_libm.fma.restype = _ct.c_double


def fma64(a, b, c):
    """IEEE double fused multiply-add (C99 fma from libSystem)."""
    return _libm.fma(a, b, c)


def F(x):
    return float(np.float32(x))


def ffma(a, b, c):
    """float32 fma of python floats."""
    return float(fma32(np.float32(a), np.float32(b), np.float32(c)))


# ------------------------------------------------------------- 0x1000c6e28

def resample_curve(t, p4, p5):
    """FUN_1000c6e28 on a list of 512 int32 (returns new list); follows the asm."""
    t = [i32(v) for v in t]
    last = t[511]
    n = 0x200
    k = 0x1fe
    while True:
        if k + 1 < 0:
            n = 0x1ff
            break
        same = t[k + 1] == (t[k] if k >= 0 else None)
        n -= 1
        k -= 1
        if not same:
            break
    tq = lambda v, d: (v + (d - 1 if v < 0 else 0)) >> {4: 2, 8: 3}[d]
    q3, q2, q1 = tq(3 * n, 4), tq(n, 4), tq(n, 8)
    e3 = q3 - p4
    e3 = e3 if e3 < 0x1ff else 0x1ff
    e1 = q1 - tq(p5, 4)
    e1 = e1 if e1 < 0x1ff else 0x1ff
    e1c = max(e1, 0)
    out = []

    def seg(src, num, den_, start_rel, count):
        s0 = F(num)
        s1 = F(F(1.0) / F(den_))
        for r in range(count):
            s2 = F(F(start_rel + r) * s0)
            s3 = F(s2 * s1)
            fl = math.floor(s3)
            frac = ffma(s2, s1, -F(fl))
            a = F(t[src + fl])
            b = F(t[src + fl + 1])
            out.append(fcvtzs(ffma(F(b - a), frac, a)))

    pos = 0
    if e1 >= 1:
        seg(0, q1, e1c, 0, e1c)
        pos = e1c
    e3c = max(e3, 0)
    if q2 - pos > 0:
        seg(q1, (q2 - q1) or 1, (q2 - e1c) or 1, pos - e1c, q2 - pos)
        pos = q2
    if pos < e3c:
        seg(q2, (q3 - q2) or 1, (e3c - q2) or 1, pos - q2, e3c - pos)
        pos = e3c
    if n - pos > 0:
        seg(q3, (n - q3) or 1, (n - e3c) or 1, pos - e3c, n - pos)
        pos = n
    while pos <= 0x1ff:
        out.append(last)
        pos += 1
    return out[:0x200]


# ------------------------------------------------------------- 0x1000c7120

def build_ygamma(base512, remap=False, f8a=0, f8c=0, f8e=0, knee_pos=0, knee_slope=0,
                 contrast=0, tbl1082=None, tbl1482=None, p4=0, p5=0):
    """Returns (expanded 2049-entry int16 table at render+0x80, int16 LUT[65536]
    whose index 0x8000 is input 0; YGamma reads LUT[0x8000 + Y])."""
    base = [i16(v) for v in base512]
    if not remap:
        cur = list(base)
    else:
        cur = [0] * 512
        a, c, e = i16(f8a), i16(f8c), i16(f8e)
        w20, w26, w28 = a >> 6, c >> 6, e >> 6
        if a >= 0x40:
            n = max(w20, 1) if w20 > 1 else 1
            cur[:n] = base[:n]
        if w20 < w28:
            w13 = sdiv(e - a, w26 - w20)
            w8 = i32(w20 << 14)
            w9 = i32((w20 << 6) - a)
            for k in range(w28 - w20):
                w14 = sdiv(w9, w13)
                w15 = sdiv(i32(w8 - (i32(w14 * w13 + a) << 8)), w13)
                idx = w14 + w20
                v = (i32(w15 * base[idx + 1] + (0x100 - w15) * base[idx]) & 0xffffffff) >> 8
                cur[w20 + k] = i16(v)
                w8 = i32(w8 + 0x4000)
                w9 = i32(w9 + 0x40)
        for k in range(max(w28, 0), 512):
            cur[k] = base[w26]
        cur[0] = 0
    if knee_pos:
        s0 = F(F(knee_slope & 0xffff) * F(6.1035156e-05))
        t0 = cur[(knee_pos & 0xffff) >> 5]
        for k in range(512):
            d = cur[k] - t0
            if d > 0:
                cur[k] = i16(t0 + fcvtzs(F(s0 * F(d))))
    p6 = F(contrast)
    s0 = F(p6 * F(0.01))
    s1 = F(p6 * F(-0.01))
    vals = []
    for k in range(512):
        x = F(F(k + 1) * F(64.0))
        if s0 <= 0.0:
            s5 = F(F(i16(tbl1082[k])) - x)
            s6 = s1
        else:
            s6 = F(F(i16(tbl1482[k])) - x)
            s5 = s0
        v = ffma(s6, s5, x)
        v = F(v * F(cur[k]))
        v = F(v / x)
        v = F(v + F(0.5))
        v = max(v, 0.0)
        vals.append(fcvtzs(v))
    vals = resample_curve(vals, p4, p5)
    ex = [0] * 0x801
    for k in range(511):
        a, b = vals[k], vals[k + 1]
        ex[4 * k] = i16(a)
        m = ((a + 1 + b) & 0xffffffff) >> 1
        ex[4 * k + 2] = i16(m)
        ex[4 * k + 1] = i16(((a + 1 + i16(m)) & 0xffffffff) >> 1)
        ex[4 * k + 3] = i16(((b + i16(a) + 1) & 0xffffffff) >> 1)
    for k in range(0x7fc, 0x801):
        ex[k] = ex[0x7fb]
    lut = [0] * 0x10000
    for i in range(0x800):
        w1 = ex[i]
        d = ex[i + 1] - w1
        w2 = w1 << 4
        for j in range(16):
            lut[0x8000 + 16 * i + j] = i16(w2 + j * d)
    return np.array(ex, np.int16), np.array(lut, np.int16)


# ------------------------------------------------------------- 0x1000d70e0 / 0x1000d71fc

DEFAULT_PAIRS = 0x100237c8c


def _default_pairs():
    return struct.unpack("<20H", _read_va(DEFAULT_PAIRS, 40))


def _check_level(level):
    """Fade level (dev+0x2bc): the slider gives 0..90.  FUN_1000d71fc uses q = level / 10
    (sdiv) as an index, so a negative level would read before the 10-entry table; that is
    refused here and in develop_settings.fade_dev().  Every level >= 0 is deterministic in
    Edit (between steps it interpolates, above 90 q = 8 extrapolates) and is supported."""
    if level < 0:
        raise ValueError(f"YGamma level {level} < 0: the gain table is indexed from 0..90")


def ygamma_offset(look_has_780e, tbl_780b, level):
    """FUN_1000d70e0 -> int (stored as int16 at render+0x91962)."""
    _check_level(level)
    lv = level & 0xffffffff
    if look_has_780e:
        if level == 0:
            return tbl_780b[0] & 0xffff
        if i32(level) <= 9:
            return tbl_780b[1] & 0xffff
        q = 8 if i32(level) > 0x59 else lv // 10
        a, b = tbl_780b[q] & 0xffff, tbl_780b[q + 1] & 0xffff
    else:
        if level == 0:
            return 0
        if i32(level) < 10:
            return 0x2980
        q = 8 if i32(level) > 0x59 else lv // 10
        pairs = _default_pairs()
        a, b = pairs[2 * q], pairs[2 * (q + 1)]
    frac = F(F(i32(level - q * 10)) * F(0.1))
    v = ffma(frac, F(F(b) - F(a)), F(a))
    v = min(v, 16383.0)
    v = max(v, 0.0)
    return fcvtzs(v)


def ygamma_gain_raw(look_has_780e, tbl_780e, level):
    """FUN_1000d71fc -> uint16; gain = value / 16384."""
    _check_level(level)
    lv = i32(level)
    q = 8 if lv > 0x59 else sdiv(lv, 10)
    frac = F(F(i32(lv - q * 10)) * F(0.1))
    if look_has_780e:
        a, b = F(tbl_780e[q] & 0xffff), F(tbl_780e[q + 1] & 0xffff)
        v = ffma(frac, F(b - a), a)
        v = max(v, 0.0)
        return fcvtzs(v) & 0xffff
    pairs = _default_pairs()
    a, b = F(pairs[2 * q + 1]), F(pairs[2 * (q + 1) + 1])
    v = ffma(frac, F(b - a), a)
    w = (fcvtzs(v) << 5) & 0xffffffff
    if v > 16384.0:
        w = 0x4000
    if v < 0.0:
        w = 0
    return w & 0xffff


# ------------------------------------------------------------- 0x1000feb68

def ygamma_apply(y, lut, offset, gain, dev2ac=0, dev2b0=0):
    """Y plane (uint16) -> uint16. lut: int16[65536] from build_ygamma."""
    w13 = dev2ac + 3 if dev2ac < 0 else dev2ac
    nq = i16(-(w13 >> 2))
    s2 = F(F(nq) * F(63.998047))
    s3 = F(F(1.0) / F(i32(0x200 - dev2b0 - nq)))
    s0 = F(i16(offset))
    s1 = F(gain)
    v = (np.asarray(lut, np.int16).view(np.uint16)[0x8000 + np.asarray(y, np.int64)]).astype(np.float32)
    s7 = ((v - f32(s2)).astype(np.float32) * f32(512.0)).astype(np.float32)
    s6 = np.where(f32(s2) < v, s7, f32(0.0))
    s6 = fma32(s6, f32(s3), f32(-s0))           # fnmsub: s6*s3 - s0
    s6 = fma32(s6, f32(s1), f32(s0))
    s6 = np.maximum(s6, f32(0.0))
    s6 = np.minimum(s6, f32(16383.0))
    return np.trunc(s6).astype(np.int64).astype(np.uint16)


# ------------------------------------------------------------- 0x1000e6da0

CS_T1, CS_T2, CS_T3 = 0x100243f50, 0x100244000, 0x1002440b0


def chroma_suppress_params(look, render_exposure, render_18ee, tbl_89962, tbl_69962, tbl_118f6, tbl_218fc):
    """Return (w9 shadow threshold, w12 highlight threshold, w8 shadow slope, w11 highlight slope)."""
    g = {o: i16(look[o]) for o in (0xf00, 0xf02, 0xf04, 0xf06, 0xf08, 0xf0a, 0xf0c, 0xf0e)}
    fmt = look["fmt"] & 0xffffffff
    w9 = (fmt - 0x100) & 0xffffffff
    if w9 < 6 and (0x3d >> (fmt & 31)) & 1:
        d0 = 512.0
    else:
        d0 = float(g[0xf00])
    cond = w9 < 0x16 and (0x2e67ff >> (w9 & 31)) & 1
    tab = lambda va: struct.unpack("<d", _read_va(va + 8 * w9, 8))[0]
    d1 = tab(CS_T1) if cond else float(g[0xf02])
    d3 = 15360.0 if cond else float(g[0xf08])
    d4 = tab(CS_T2) if cond else float(g[0xf04])
    d5 = tab(CS_T3) if cond else float(g[0xf0a])
    w12 = g[0xf06]
    d2 = float(i32(render_exposure)) * 0.0033333333333333335
    w8, w10 = g[0xf00], w12
    if -2.0 <= d2 < -1.0:
        d16 = d2 + 2.0
        w8 = fcvtzs(fma64(d16, d1 - d4, d4))
        w10 = fcvtzs(fma64(d16, d3 - d5, d5))
    elif -1.0 <= d2 < 0.0:
        d5b = d2 + 1.0
        w8 = fcvtzs(fma64(d5b, d0 - d1, d1))
        w10 = fcvtzs(fma64(d5b, float(w12) - d3, d3))
    d0 = float(i16(render_18ee)) * 0.015625
    w11 = w10 if w10 < 0x3fff else 0x3fff
    a = i16(tbl_89962[w11])
    b = i16(tbl_69962[(a + 0x8000) & 0xffffffff])
    w11 = fcvtzs(d0 * float(b))
    w11 = w11 if w11 < 0x7fff else 0x7fff
    c = i16(tbl_118f6[w11])                    # render+0x118f6 indexed by w11
    w12 = i16(tbl_218fc[(c + 0x8000) & 0xffffffff])
    w9t = sdiv(w12 * g[0xf0e], w10) if w10 != 0 else 0
    d3 = d2 + 2.0 if (-2.0 <= d2 <= -1.0) else 0.0
    d1 = -d2 if (-1.0 <= d2 <= 0.0) else d3
    w11 = fcvtzs(float(w8) / d0)
    w8s = fcvtzs(float(g[0xf0c]) / d0)
    w11 = i32(w11 + fcvtzs(d1 * 150.0))
    w12 = i32(w12 + fcvtzs(d1 * -1200.0))
    return w9t, w12, w8s, w11


def _w32(x):
    return ((np.asarray(x, np.int64) + (1 << 31)) % (1 << 32)) - (1 << 31)


def chroma_suppress_apply(y, cr, cb, params):
    w9, w12, w8, w11 = params
    y = np.asarray(y, np.uint16).view(np.int16).astype(np.int64)
    lo = _w32(w9 - y)
    hi = _w32(y - w12)
    m = np.where(lo > 0, _w32(lo * w8), _w32(hi * w11))
    f = 0xff - (m >> 12)
    f = np.maximum(f, 0)
    f = np.where(f < 0xff, f, 0xff)
    f = np.where((lo > 0) | (hi > 0), f, 0xff)
    y_out = np.maximum(y, 0).astype(np.uint16)

    def scale(p):
        v = _w32((np.asarray(p, np.int64) - 0x8000) * f)
        v = np.where(v < 0, v + 0xff, v)
        return ((((v & 0xffffffff) >> 8) ^ 0x8000) & 0xffff).astype(np.uint16)
    return y_out, scale(cr), scale(cb)


# ------------------------------------------------------------- 0x1000e53ac / 0x1000e5840

def sin_table():
    return [F(math.sin(i * 0.012271846644580364)) for i in range(512)]


def _d(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        return float(np.float64(a) / np.float64(b))


def approx_atan2(x, y):
    """FUN_1000e53ac(x, y): polynomial atan2 in double with fused multiply-adds (asm order)."""
    k1 = struct.unpack("<d", struct.pack("<Q", 0x3fb0f9096bb98c7e))[0]
    k2 = struct.unpack("<d", struct.pack("<Q", 0x3fcf525460aa64c3))[0]
    q = struct.unpack("<d", struct.pack("<Q", 0x3fe921fb60000000))[0]
    h = struct.unpack("<d", struct.pack("<Q", 0x3ff921fb60000000))[0]
    pi = struct.unpack("<d", struct.pack("<Q", 0x400921fb60000000))[0]
    x = x + struct.unpack("<d", struct.pack("<Q", 0x3ddb7cdfe0000000))[0]

    def near(r, add):          # fma(fma(fma(|r|,k1,k2), |r|-1, -q), r, add)
        a_ = abs(r)
        return fma64(fma64(fma64(a_, k1, k2), a_ + -1.0, -q), r, add)

    def far(r, add):           # fma(fma(fma(|r|,-k1,-k2), |r|-1, q), r, add)
        a_ = abs(r)
        return fma64(fma64(fma64(a_, -k1, -k2), a_ + -1.0, q), r, add)

    def mid(r2):               # fma(r2, q, (r2 - |r2|*r2) * fma(|r2|,k1,k2))
        b_ = abs(r2)
        d16 = fma64(-b_, r2, r2)
        return fma64(r2, q, d16 * fma64(b_, k1, k2))

    if y < 0.0:
        if x < 0.0:
            return near(_d(y, x), -h) if x <= y else far(_d(x, y), -pi)
        return near(_d(y, x), h) if x >= -y else far(_d(x, y), pi)
    r, r2 = _d(y, x), _d(x, y)
    d5 = near(r, -h) if -x >= y else mid(r2)
    d1 = near(r, h) if x >= y else mid(r2)
    return d5 if x < 0.0 else d1


def hue_saturation(cr, cb, hue, sat, sin_tab=None):
    sin_tab = sin_tab or sin_table()
    if hue < 0:
        dh = float(hue) * 0.35
    elif hue == 0:
        dh = 0.0
    else:
        dh = float(hue) * 0.7
    s = float(sat) * 0.01 + 1.0
    cr = np.asarray(cr, np.int64)
    cb = np.asarray(cb, np.int64)
    o1 = np.empty(cr.shape, np.uint16)
    o2 = np.empty(cr.shape, np.uint16)
    rad = dh * 0.017453292519943302
    for idx in np.ndindex(cr.shape):
        a = int(cr[idx]) - 0x8000
        b = int(cb[idx]) - 0x8000
        if rad == 0.0:
            d18, d20 = float(a), float(b)
        else:
            r = math.sqrt(float(i32(a * a + b * b)))
            ang = approx_atan2(float(a), float(b))
            f17 = F((ang + rad) * 81.4873285954589)
            f19 = 0.5 if f17 >= 0.0 else -0.5
            u = fcvtzs(F(f19 + f17))
            i1 = (u & 0x1ff) if u >= 0 else 0x200 - ((-u) & 0x1ff)
            d18 = r * sin_tab[i1 & 0x3ff] if i1 < 512 else r * 0.0
            au = -u if u < 0 else u
            d20 = r * sin_tab[(au + 0x80) & 0x1ff]
        o1[idx] = fcvtzs(s * d18 + 32768.0) & 0xffff
        o2[idx] = fcvtzs(s * d20 + 32768.0) & 0xffff
    return o1, o2


def _dconst64(bits):
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


_AT_K1 = _dconst64(0x3fb0f9096bb98c7e)
_AT_K2 = _dconst64(0x3fcf525460aa64c3)
_AT_Q = _dconst64(0x3fe921fb60000000)
_AT_H = _dconst64(0x3ff921fb60000000)
_AT_PI = _dconst64(0x400921fb60000000)
_AT_TINY = _dconst64(0x3ddb7cdfe0000000)


def _approx_atan2_vec(x, y):
    """Vectorised approx_atan2: same branches and float64 constants.

    Products and adds are rounded separately instead of fused, which can differ
    from the scalar port by ~1 ulp.  hue_saturation_vec re-runs the exact scalar
    code for the few pixels where that could change a truncation.
    """
    k1, k2, q, h, pi = _AT_K1, _AT_K2, _AT_Q, _AT_H, _AT_PI
    x = x + _AT_TINY
    with np.errstate(divide="ignore", invalid="ignore"):
        r = y / x
        r2 = x / y

    # Expand the three fma layers: fma(fma(fma(a,k1,k2), a-1, c), r, add)
    def near_(rr, add):
        a_ = np.abs(rr)
        return ((a_ * k1 + k2) * (a_ - 1.0) + (-q)) * rr + add

    def far_(rr, add):
        a_ = np.abs(rr)
        return ((a_ * -k1 + -k2) * (a_ - 1.0) + q) * rr + add

    def mid_(rr):
        b_ = np.abs(rr)
        return rr * q + ((rr - b_ * rr) * (b_ * k1 + k2))

    xneg, yneg = x < 0, y < 0
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        d5 = np.where(-x >= y, near_(r, -h), mid_(r2))
        d1 = np.where(x >= y, near_(r, h), mid_(r2))
        pos = np.where(xneg, d5, d1)
        neg_xneg = np.where(x <= y, near_(r, -h), far_(r2, -pi))
        neg_xpos = np.where(x >= -y, near_(r, h), far_(r2, pi))
    return np.where(yneg, np.where(xneg, neg_xneg, neg_xpos), pos)


def _i32_vec(v):
    v = np.asarray(v, np.int64) & 0xFFFFFFFF
    return np.where(v >= (1 << 31), v - (1 << 32), v)


def _trunc32(x):
    """fcvtzs on an array: truncate toward zero, NaN -> 0."""
    x = np.where(np.isnan(x), 0.0, x)
    x = np.clip(x, -2147483648.0, 2147483647.0)
    return np.trunc(x).astype(np.int64)


def hue_saturation_vec(cr, cb, hue, sat, sin_tab=None, chunk=256):
    """Vectorised 0x1000e5840: same arithmetic as hue_saturation for whole planes.

    Processes at most `chunk` rows at a time to keep the float64 temporaries
    small.  A pixel whose truncation is close enough that the non-fused
    multiplies could change the result is recomputed with the scalar code.
    """
    sin = np.asarray(sin_table() if sin_tab is None else sin_tab, np.float64)
    if hue < 0:
        dh = float(hue) * 0.35
    elif hue == 0:
        dh = 0.0
    else:
        dh = float(hue) * 0.7
    s = float(sat) * 0.01 + 1.0
    rad = dh * 0.017453292519943302
    cr = np.asarray(cr, np.int64)
    cb = np.asarray(cb, np.int64)
    o1 = np.empty(cr.shape, np.uint16)
    o2 = np.empty(cr.shape, np.uint16)
    rows = cr.shape[0]
    for y0 in range(0, rows, chunk):
        y1 = min(y0 + chunk, rows)
        a = cr[y0:y1] - 0x8000
        b = cb[y0:y1] - 0x8000
        if rad == 0.0:
            f1 = s * a.astype(np.float64) + 32768.0
            f2 = s * b.astype(np.float64) + 32768.0
            o1[y0:y1] = (_trunc32(f1) & 0xFFFF).astype(np.uint16)
            o2[y0:y1] = (_trunc32(f2) & 0xFFFF).astype(np.uint16)
            continue
        with np.errstate(invalid="ignore"):
            r = np.sqrt(_i32_vec(a * a + b * b).astype(np.float64))
        ang = _approx_atan2_vec(a.astype(np.float64), b.astype(np.float64))
        f17 = (ang + rad) * 81.4873285954589
        u = _trunc32(np.where(f17 >= 0.0, 0.5, -0.5) + f17)
        pos = u >= 0
        u1 = np.where(pos, u & 0x1FF, 0x200 - ((-u) & 0x1FF))
        d18 = np.where(u1 < 512, r * sin[u1 & 0x3FF], 0.0)
        au = np.abs(u)
        d20 = r * sin[(au + 0x80) & 0x1FF]
        f1 = s * d18 + 32768.0
        f2 = s * d20 + 32768.0
        q1 = _trunc32(f1) & 0xFFFF
        q2 = _trunc32(f2) & 0xFFFF
        # Cells close to a truncation boundary: redo them with the exact scalar path.
        risky = ((np.abs(f17 - np.trunc(f17)) < 1e-6) | (np.abs(f17 - np.ceil(f17)) < 1e-6)
                 | (np.abs(f1 - np.round(f1)) < 1e-6) | (np.abs(f2 - np.round(f2)) < 1e-6))
        if risky.any():
            idx = np.flatnonzero(risky.reshape(-1))
            if idx.size <= 50000:
                ar = a.reshape(-1)
                br = b.reshape(-1)
                rr = r.reshape(-1)
                qq1, qq2 = q1.reshape(-1), q2.reshape(-1)
                for k in idx:
                    ak, bk = int(ar[k]), int(br[k])
                    rk = rr[k]
                    if math.isnan(rk):
                        rk = 0.0
                    # exact double atan2 for this pixel, then the same float32 steps
                    f = F((approx_atan2(float(ak), float(bk)) + rad) * 81.4873285954589)
                    uu = fcvtzs(F((0.5 if f >= 0.0 else -0.5) + f))
                    i1 = (uu & 0x1FF) if uu >= 0 else 0x200 - ((-uu) & 0x1FF)
                    d1_ = rk * sin[i1 & 0x3FF] if i1 < 512 else 0.0
                    auu = -uu if uu < 0 else uu
                    d2_ = rk * sin[(auu + 0x80) & 0x1FF]
                    qq1[k] = fcvtzs(s * d1_ + 32768.0) & 0xFFFF
                    qq2[k] = fcvtzs(s * d2_ + 32768.0) & 0xFFFF
                q1, q2 = qq1.reshape(q1.shape), qq2.reshape(q2.shape)
        o1[y0:y1] = q1.astype(np.uint16)
        o2[y0:y1] = q2.astype(np.uint16)
    return o1, o2


# ------------------------------------------------------------- 0x10013db80

def _dconst(bits):
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


C_37E6, C_1401, C_1771, C_135E6 = (_dconst(b) for b in (0x3f03660e51d25aab, 0x3ff66e8af81626b3,
                                                        0x3ffc5a059a73b42d, 0x3f21b1d92b7fe08b))
C_N15E6, C_N54E6, C_713, C_564 = (_dconst(b) for b in (0xbeef75104d551d69, 0xbf0c4fc1df3300de,
                                                       0x3fe6d321e603d578, 0x3fe20f14db59578a))


def lut3d_apply(y, cr, cb, lut):
    """ZcTask3DLut::Process. lut: dict(f20, f28, f2c, f30, f34, f38, shaper=int16[65536]
    (index 0x8000 == 0), data=int16[nodes*24]). Returns (Y, Cr, Cb) uint16."""
    sh, data = np.asarray(lut["shaper"], np.int64), np.asarray(lut["data"], np.int64)
    s13, s15 = 0xe - lut["f2c"], 0x10 - lut["f30"]
    m14, m17 = lut["f34"] & 0xffffffff, lut["f38"] & 0xffffffff
    yo = np.empty(np.shape(y), np.uint16); cro = np.empty_like(yo); cbo = np.empty_like(yo)
    for idx in np.ndindex(np.shape(y)):
        Y = int(y[idx]); d1 = float(int(cb[idx])) + -32768.0; d2 = float(int(cr[idx])) + -32768.0
        s3 = F(fma64(d2, C_1401, d1 * C_37E6))
        s1 = F(fma64(d2, C_135E6, d1 * C_1771))
        s3 = max(min(s3, 32767.0), -32768.0); s1 = max(min(s1, 32767.0), -32768.0)
        a = (int(sh[0x8000 + fcvtzs(s3)]) + 0x8000) & 0xffffffff
        b = (int(sh[0x8000 + fcvtzs(s1)]) + 0x8000) & 0xffffffff
        iy, ia, ib = Y >> s13, a >> s15, b >> s15
        fy, fa, fb = m14 & Y, a & m17, b & m17
        w20, w21, w22 = (m14 + 1) - fy, (m17 + 1) - fa, (m17 + 1) - fb
        w24 = i32(fa * fy) >> 3; w23 = i32(w21 * fy) >> 3; w8 = i32(fa * w20) >> 3; w30 = i32(w21 * w20) >> 3
        r = lambda p, q: i32(p * q + 0x40000) >> 19
        c9, c20, c21 = r(w23, w22), r(w24, w22), r(w8, w22)
        c22, c23, c24, c8 = r(w30, fb), r(w23, fb), r(w24, fb), r(w8, fb)
        c4 = 0x200 - c23 - (c20 + c21 + c24 + c8 + c9 + c22)
        node = i32(i32(ia * lut["f28"] + ib) * lut["f20"] + iy) * 24
        e = data[node:node + 24]
        sy = i32(c4 * e[0] + c20 * e[2] + c21 * e[3] + c9 * e[1] + c22 * e[4] + c23 * e[5] + c24 * e[6] + c8 * e[7])
        sa = i32(c9 * e[9] + c20 * e[10] + c21 * e[11] + c22 * e[12] + c23 * e[13] + c4 * e[8] + c24 * e[14] + c8 * e[15])
        sb = i32(c4 * e[16] + c9 * e[17] + c20 * e[18] + c21 * e[19] + c22 * e[20] + c23 * e[21] + c24 * e[22] + c8 * e[23])
        yo[idx] = 0 if sy < 0 else ((sy & 0xffffffff) >> 9) & 0xffff
        A, B = float(F(sa >> 9)), float(F(sb >> 9))
        o1 = fcvtzs(fma64(A, C_713, B * C_N15E6))
        o2 = fcvtzs(fma64(B, C_564, A * C_N54E6))
        clampi = lambda v: (max(min(v, 0x7fff) if v < 0x7fff else 0x7fff, -0x8000) ^ 0x8000) & 0xffff
        cro[idx] = clampi(o1); cbo[idx] = clampi(o2)
    return yo, cro, cbo


# --- inverse tone tables used by the ChromaSuppress threshold (FUN_1000d50f8) ---------------

def inverse_lut_centered(lut):
    """FUN_1000c2618: render+0x69962 from the MainGamma LUT at render+0x18f6.

    lut: 65536 int16 entries indexed by (x + 0x8000). Returns 65536 int16 entries indexed by
    (v + 0x8000): out[v] = first k >= previous with lut[k+1] >= v, minus 0x8000; odd-symmetric.
    Element 0 is never written (kept as `init`, zero in a fresh render object)."""
    lut = np.asarray(lut).astype(np.int16).astype(np.int64)
    out = np.zeros(65536, np.uint16)
    k = 0x8000
    for v in range(0x8000):
        while k <= 0xfffe and lut[k + 1] < v:
            k += 1
        out[0x8000 + v] = (k ^ 0x8000) & 0xffff
        out[0x8000 - v] = (0xffff8000 - k) & 0xffff
    return out.view(np.int16)


def inverse_lut_positive(lut):
    """FUN_1000c2690: render+0x89962 from the YGamma LUT at render+0x218fc (16384 entries)."""
    lut = np.asarray(lut).astype(np.int16).astype(np.int64)
    out = np.zeros(0x4000, np.uint16)
    k = 0x8000
    for v in range(0x4000):
        while k <= 0xfffe and lut[k + 1] < v:
            k += 1
        out[v] = (min(k, 0x17fff) ^ 0x8000) & 0xffff
    return out.view(np.int16)


def chroma_highlight_threshold(lut_mg, lut_yg, f06, render_18ee):
    """FUN_1000c2204: gammaObj+0x40 = YG[MG[min(invMG[invYG[f06]] * 18ee >> 6, 0x7fff)]]."""
    inv_mg, inv_yg = inverse_lut_centered(lut_mg), inverse_lut_positive(lut_yg)
    v = int(inv_mg[(int(inv_yg[min(i16(f06), 0x3fff)]) + 0x8000) & 0xffff]) * i16(render_18ee) >> 6
    v = min(v, 0x7fff)
    return int(np.asarray(lut_yg).astype(np.int16)[(int(np.asarray(lut_mg).astype(np.int16)[v + 0x8000]) + 0x8000) & 0xffff])


# --- AreaComp (ZcTaskAreaComp, process 0x1000b3aec; params FUN_1000b3db8 / FUN_1000b3ee8) ---

def _f32bits(u):
    return float(np.frombuffer(struct.pack("<I", u), "<f4")[0])


def area_comp_params(fmt_code, has_780f, look_id):
    """16 floats + flag. fmt_code = ARW format code (0x3600 for ARW 6.0), look_id = 0x7770 id
    (2 Standard, 8 Portrait, 0x16 FL2, 0x17 FL3, ...)."""
    if fmt_code > 0x33ff and has_780f:                       # FUN_1000b3ee8
        np_ = look_id != 8
        u = [0, 0, 0xc2900000, 0x42580000,
             0x3e800000 if np_ else 0x3e980000, 0xbf080000 if np_ else 0xbf040000,
             0x3de00000 if np_ else 0x3e000000, 0xbf540000 if np_ else 0xbf580000,
             0xbf440000, 0xbea80000, 0x3eb80000, 0x3f700000,
             0x3e700000 if np_ else 0x3e880000, 0x3e900000 if np_ else 0x3e600000, 0, 0]
        return [_f32bits(x) for x in u], 1
    raise NotImplementedError("FUN_1000b3db8 legacy parameter set")


def area_comp_apply(p1, p2, params):
    """p1 / p2: chroma planes 1 and 2 (uint16, 0x8000 centred). Flag-1 branch, asm order."""
    p, flag = params
    if flag != 1:
        raise NotImplementedError
    f = [np.float32(v) for v in p]
    a = (np.asarray(p1, np.int64) - 0x8000).astype(np.float32)
    b = (np.asarray(p2, np.int64) - 0x8000).astype(np.float32)

    def edge(c0, c1, k0, k1):          # fmadd(c1 + b, k1, (c0 + a) * k0), clamped at 0
        x0 = (f[c0] + a).astype(np.float32)
        x1 = (f[c1] + b).astype(np.float32)
        v = fma32(x1, f[k1], (x0 * f[k0]).astype(np.float32))
        return np.where(v <= 0, np.float32(0), v).astype(np.float32)

    d0 = edge(0, 1, 4, 6)              # (p0+a)*p4 + p6*(p1+b)
    d1 = edge(0, 1, 5, 7)              # p5*(p0+a) + (p1+b)*p7
    d2 = edge(2, 3, 8, 10)
    d3 = edge(2, 3, 9, 11)
    m = np.fmin(np.fmin(d3, d2), np.fmin(d1, d0)).astype(np.float32)
    o1 = a.astype(np.float64) + 32768.0 + (m * f[12]).astype(np.float32).astype(np.float64)
    o2 = b.astype(np.float64) + 32768.0 + (m * f[13]).astype(np.float32).astype(np.float64)
    cv = lambda v: (np.trunc(v).astype(np.int64) & 0xffff).astype(np.uint16)
    return cv(o1), cv(o2)
