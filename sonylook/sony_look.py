"""Reference port of the Sony Imaging Edge Edit 4.0.00.10312 creative-look pixel path.

Covers the two stages that consume Creative Look data stored in the ARW:

* ZcTaskLinearMatrix16 (Process 0x1000acdd4): hue-indexed 1024 x 18 table built
  from tag 0x780f (see ../color_table/build_color_table.py).
* ZcTaskMainGamma (Process 0x1000e96a0): per-channel LUT built by 0x1000c7774
  from tags 0x7805 (x knots) / 0x7806 (y knots), optionally composed with the
  contrast/highlight/shadow family table built by 0x10011e174.

Float32 rounding follows the ARM64 instruction order; FMA appears exactly where
the original uses FMADD. All functions operate on numpy arrays.
"""
import struct
from pathlib import Path

import numpy as np

HUE_ATAN_TABLE = 0x100234730      # float32[512], atan ratio -> 0..128 (1/8 turn)
CONTRAST_FAMILY = 0x100246790     # int32[37][1025], curve 18 is identity

f32 = np.float32


def _read_va(va, n):
    # constant tables are read from the user's own Edit install (edit_binary.py)
    import edit_binary
    return edit_binary.read_va(va, n)


def load_static_tables():
    atan = np.frombuffer(_read_va(HUE_ATAN_TABLE, 2048), "<f4").copy()
    family = np.frombuffer(_read_va(CONTRAST_FAMILY, 37 * 0x1004), "<i4").reshape(37, 1025).copy()
    return atan, family


# ---------------------------------------------------------------- float helpers

def fma32(a, b, c):
    """Single-rounding float32 fused multiply-add, vectorised.

    a*b of two float32 is exact in float64. The float64 sum may round to a
    float32 midpoint; only those (rare) lanes need the TwoSum residual that
    picks the side, so the residual is computed for the candidates alone.
    The sum is accumulated in place to save a full-size temporary; the
    midpoint path recomputes the exact product for its few candidates.
    """
    s = np.multiply(a, b, dtype=np.float64)
    try:
        s += c
    except ValueError:                 # c broadcasts to a larger shape than a*b
        s = np.add(s, c, dtype=np.float64)
    r = s.astype(np.float32)
    bits = s.view(np.uint64)
    mid = (bits & np.uint64((1 << 29) - 1)) == np.uint64(1 << 28)
    if mid.any():
        exp = bits & np.uint64(0x7ff0000000000000)
        mid &= exp != np.uint64(0x7ff0000000000000)       # finite only
        idx = np.flatnonzero(mid.reshape(-1))
        if idx.size:
            shape = s.shape if s.ndim else (1,)
            ii = np.unravel_index(idx, shape)
            # gather first, then widen: converting a whole plane just to read a
            # handful of candidates would cost more than the fma itself
            aa = np.broadcast_to(a, shape)[ii].astype(np.float64)
            bb0 = np.broadcast_to(b, shape)[ii].astype(np.float64)
            pp = aa * bb0                  # exact: float32 x float32 in float64
            ss = s.reshape(-1)[idx]
            cc = np.broadcast_to(c, shape)[ii].astype(np.float64)
            bb = ss - pp
            err = (pp - (ss - bb)) + (cc - bb)
            ok = err != 0
            if ok.any():
                rr = r.reshape(-1)         # r is a fresh array from astype
                sm = ss[ok]
                lo = sm.astype(np.float32)
                lo = np.where(lo.astype(np.float64) > sm,
                              np.nextafter(lo, np.float32(-np.inf)), lo)
                hi = np.nextafter(lo, np.float32(np.inf))
                rr[idx[ok]] = np.where(err[ok] > 0, hi, lo)
                r = rr.reshape(s.shape)
    return r


# ---------------------------------------------------------------- 0x1000abaf0

def blur_rows(p):
    """Horizontal [1 8 14 8 1]/32 low-pass with Sony's edge handling (width >= 5)."""
    p = np.asarray(p, np.float32)
    o = np.empty_like(p)
    k = f32(1 / 32)
    o[:, 0] = fma32(p[:, 1], 8, fma32(p[:, 0], 23, p[:, 2])) * k
    o[:, 1] = fma32(p[:, 2], 8, fma32(p[:, 1], 14, fma32(p[:, 0], 9, p[:, 3]))) * k
    c = p[:, 2:-2]
    s6 = p[:, 3:-1] + p[:, 1:-3]
    s5 = fma32(c, 14, p[:, 0:-4])
    s5 = s5 + p[:, 4:]
    o[:, 2:-2] = fma32(s6, 8, s5) * k
    n = p.shape[1]
    s6 = p[:, n - 1] + p[:, n - 3]
    s5 = fma32(p[:, n - 2], 14, p[:, n - 4]) + p[:, n - 1]
    o[:, n - 2] = fma32(s6, 8, s5) * k
    o[:, n - 1] = fma32(p[:, n - 1], 23, fma32(p[:, n - 2], 8, p[:, n - 3])) * k
    return o.astype(np.float32)


# ---------------------------------------------------------------- 0x1000abe38

def hue_index(y_rgb, c_rgb, atan, scale_flag=0):
    """Hue in 1/1024-turn units from (R-Y, B-Y); Y = (109R+366G+37B)/512.

    y_rgb supplies R,G,B for luma, c_rgb supplies R and B for the differences
    (both are the blurred planes when look controls 0x91998/0x9199a are 1).
    """
    r, g, b = (np.asarray(x, np.float32) for x in y_rgb)
    y = ((r.astype(np.float64) * 109.0 + g.astype(np.float64) * 366.0)
         + b.astype(np.float64) * 37.0) * (1.0 / 512.0)
    y = y.astype(np.float32)
    a = (np.asarray(c_rgb[0], np.float32) - y).astype(np.float32)
    bb = (np.asarray(c_rgb[2], np.float32) - y).astype(np.float32)
    if scale_flag:
        a = (a * f32(0.875)).astype(np.float32)
        bb = (bb * f32(0.5)).astype(np.float32)
    fa, fb = np.abs(a), np.abs(bb)
    big_a = fa > fb
    a_pos = ~(a < 0)          # fcmp ... ge treats -0.0 as non-negative
    b_pos = ~(bb < 0)
    off = np.where(big_a,
                   np.where(a_pos, np.where(b_pos, -256.0, 256.0), np.where(b_pos, 768.0, -768.0)),
                   np.where(a_pos, np.where(b_pos, 0.0, -512.0), np.where(b_pos, -1024.0, 512.0)))
    num = np.where(big_a, fb, fa).astype(np.float64)
    den = np.where(big_a, np.where(a == 0, f32(1), fa), np.where(bb == 0, f32(1), fb)).astype(np.float64)
    idx = np.trunc(num * 512.0 / den + 0.5).astype(np.int64)
    idx = np.where(idx < 0x1ff, idx, 0x1ff)
    return np.abs(atan[idx] + off.astype(np.float32)).astype(np.float32)


# ---------------------------------------------------------------- 0x1000ac6b0

def apply_lm16(rgb, hue, table, thresholds=(0.0, 0.0, 0.0), quadratic=True):
    """Apply the 1024 x 18 table: out = B.x + dead-zone(N.(x*x)/16384), clipped to [0,16383].

    `table` is the (1024,18) float32 array from build_color_table.build_table.
    quadratic=False reproduces the 0x1000ac1b4 branch (look control 0x91bba == 0).
    """
    r, g, b = (np.asarray(x, np.float32) for x in rgb)
    h = np.asarray(hue, np.float32)
    i = np.trunc(h).astype(np.int64)
    i = np.where((i < 0) | (i > 0x3fe), 0x3ff, i)   # unsigned compare after fcvtzs
    t = np.asarray(table, np.float32)[i]
    sq = [(r * r).astype(np.float32), (g * g).astype(np.float32), (b * b).astype(np.float32)]
    out = []
    for row in range(3):
        B = t[..., 3 * row:3 * row + 3]
        lin = fma32(B[..., 2], b, fma32(B[..., 1], g, (B[..., 0] * r).astype(np.float32)))
        if quadratic:
            N = t[..., 9 + 3 * row:9 + 3 * row + 3]
            q = fma32(N[..., 2], sq[2], fma32(N[..., 1], sq[1], (N[..., 0] * sq[0]).astype(np.float32)))
            q = (q * f32(6.1035156e-05)).astype(np.float32)
            thr = f32(thresholds[row])
            s = (q + lin).astype(np.float32)
            v = np.where(q < 0, lin,
                         np.where(q >= thr, (s - thr).astype(np.float32),
                                  np.where(q <= -thr, (s + thr).astype(np.float32), lin)))
        else:
            v = lin
        v = np.where(v < 0, f32(0), np.where(v > f32(16383), f32(16383), v))
        out.append(v.astype(np.float32))
    return out


# ---------------------------------------------------------------- 0x10011e174

def contrast_table(family, contrast, highlight, shadow):
    """1025-entry second-stage curve; (0,0,0) gives the identity curve 18 (64*i)."""
    out = np.empty(1025, np.int64)
    for lo, hi, f in ((0, 471, float(f32((contrast - shadow) * f32(0.2) + f32(18.0)))),
                      (471, 1025, float(f32((highlight + contrast) * f32(0.2) + f32(18.0))))):
        fc = min(max(f, 0.0), 35.0)
        k = int(fc)
        t0 = family[k, lo:hi].astype(np.float32)
        t1 = family[k + 1, lo:hi].astype(np.float32)
        v = fma32(f32(f - float(f32(k))), (t1 - t0).astype(np.float32), (t0 + f32(0.5)).astype(np.float32))
        v = np.minimum(v, f32(65535.0))
        v = np.where(v <= 0, f32(0), v)
        out[lo:hi] = np.trunc(v).astype(np.int64)
    return out


# ---------------------------------------------------------------- 0x1000c7774

def build_main_gamma(x_knots, y_knots, count, second=None):
    """Return int16 LUT of 65536 entries; index 32768 is input 0 (signed domain).

    Input domain: 14-bit linear value v; knot x is v*128 (0x7805 max 2^21).
    Output: (0x7806 interpolated) >> 4, i.e. 14-bit encoded, before YCC.
    """
    X = [int(v) for v in x_knots]
    Y = [int(v) for v in y_knots]
    n = int(count)
    lut = np.zeros(0x10000, np.int64)        # lut[0x8000 + i]
    x_prev = y_prev = 0
    x_cur, y_cur = X[0], Y[0]
    idx = 0
    for i in range(1, 0x8000):
        x = i * 0x80
        if x > x_cur:
            clamp = False
            if idx < n and X[idx] <= x:
                while True:
                    if idx == n - 1:
                        clamp = True
                        break
                    if X[idx + 1] > x:
                        idx += 1
                        break
                    idx += 1
            elif idx >= n:
                clamp = True
            if clamp:
                x_prev = x_cur = X[n - 1]
                y_prev = y_cur = Y[n - 1]
            else:
                x_prev, x_cur = x_cur, X[idx]
                y_prev, y_cur = y_cur, Y[idx]
        if x_cur - x_prev != 0:
            v = f32(f32(f32(y_prev) - f32(y_cur)) * f32(x_cur - x))
            v = f32(f32(v / f32(x_cur - x_prev)) + f32(y_cur))
            y = int(np.trunc(v))
        else:
            y = y_prev
        u = (y >> 2) & 0xffffffff
        if second is not None:
            if y < 0x3ff00:
                t0, t1 = int(second[y >> 8]), int(second[(y >> 8) + 1])
            else:
                t0 = t1 = int(second[1023])
            frac = ((y >> 2) + (y >> 8) * -0x40) & 0xffffffff
            frac = frac - (1 << 32) if frac >> 31 else frac
            v = fma32(f32(f32(frac) * f32(0.015625)), f32(f32(t1) - f32(t0)), f32(t0))
            u = int(np.trunc(v)) & 0xffffffff
        lut[0x8000 + i] = np.int16(np.uint16((u >> 2) & 0xffff))
    for k in range(0x8000):
        lut[k] = np.int16(np.uint16((-int(lut[0xffff - k])) & 0xffff))
    return lut.astype(np.int16)


# ---------------------------------------------------------------- 0x1000e96a0

def apply_main_gamma(plane, lut):
    """Float-input path: linear interpolation into the signed LUT, result truncated to int16."""
    v = np.asarray(plane, np.float32)
    i = np.trunc(v).astype(np.int64)
    frac = (v - np.floor(v)).astype(np.float32)
    t0 = lut[0x8000 + i].astype(np.float32)
    t1 = lut[0x8000 + i + 1].astype(np.float32)
    out = fma32((t1 - t0).astype(np.float32), frac, t0)
    return np.trunc(out).astype(np.int32).astype(np.int16)


# ---------------------------------------------------------------- whole look

def render_look(rgb_linear14, table, gamma_lut, atan, controls=None):
    """rgb_linear14: three float32 planes, linear, 0..16383, in the working RGB
    the table was built for (sRGB primaries for output_mode 0 / param8 0).
    Returns three int16 planes after LinearMatrix16 + MainGamma (pre-YCC)."""
    controls = controls or dict(blur_luma=1, blur_chroma=1, scale=0, quadratic=1,
                                thresholds=(0.0, 0.0, 0.0))
    planes = [np.asarray(p, np.float32) for p in rgb_linear14]
    blurred = [blur_rows(p) for p in planes] if 1 in (controls["blur_luma"], controls["blur_chroma"]) else None
    ysrc = blurred if controls["blur_luma"] == 1 else planes
    csrc = blurred if controls["blur_chroma"] == 1 else planes
    hue = hue_index(ysrc, csrc, atan, controls["scale"])
    mixed = apply_lm16(planes, hue, table, controls["thresholds"], bool(controls["quadratic"]))
    return [apply_main_gamma(p, gamma_lut) for p in mixed]
