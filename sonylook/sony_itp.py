#!/usr/bin/env python3
"""Python port of Sony ZcTaskITP (Imaging Edge Edit 4.0.00.10312).

Every filter is vectorised with NumPy.  Elementwise float32 ufuncs round
exactly like the scalar ARM instructions, so the vector forms stay bit-exact
with the scalar reference; fma() keeps the double-width intermediate that the
scalar helper used.  The only remaining Python loops iterate rows (or row
pairs), never pixels.

Verified function-by-function against Sony's own code under Unicorn by
verify_itp.py; a function is only considered ported when that check reports
0 differing bits.

Plane layout in Sony: 24-byte descriptor {int w, int h, int stride_bytes,
int total_bytes, float *data}; row stride = ((w*4 + 31) & ~31) + 32.
"""
from __future__ import annotations

import struct

import numpy as np

f32 = np.float32
F32 = np.float32


def fma(a, b, c):
    """float32 fused multiply-add (single rounding), scalar or array.

    Python has no hardware fma; the double intermediate is exact for the
    product and nearly always exact for the sum.  verify_itp.py catches the
    rare double-rounding case and this can be replaced by an exact version.
    The sum is accumulated in place to save a full-size temporary.
    """
    out = np.multiply(a, b, dtype=np.float64)
    out += c
    return out.astype(np.float32)


class Plane:
    """float32 image plane; ROI coordinates are absolute."""

    def __init__(self, w: int, h: int, data=None):
        self.w = int(w)
        self.h = int(h)
        if data is None:
            self.data = np.zeros((self.h, self.w), np.float32)
        else:
            self.data = np.array(data, np.float32, copy=True)
            assert self.data.shape == (self.h, self.w)

    def copy(self):
        return Plane(self.w, self.h, self.data)

    @property
    def shape(self):
        return self.data.shape


class ROI:
    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = int(x0), int(y0), int(x1), int(y1)

    def __iter__(self):
        return iter((self.x0, self.y0, self.x1, self.y1))

    def __repr__(self):
        return f"ROI({self.x0}, {self.y0}, {self.x1}, {self.y1})"


# ---------------------------------------------------------------------------
# Branch-free filters (vtable +0x70 ... +0x188)
# ---------------------------------------------------------------------------

K1_32 = np.array([0x3d000000], np.uint32).view(np.float32)[0]     # 1/32
K1_88 = np.array([0x3c3a2e8c], np.uint32).view(np.float32)[0]     # 1/88


def _get(src: Plane, y: int, x: int) -> np.float32:
    """Sony's getter returns 0 outside the plane (negative coords included)."""
    if 0 <= y < src.h and 0 <= x < src.w:
        return src.data[y, x]
    return f32(0.0)


def weighted_sum(src: Plane, x: int, y: int, center_dx: int) -> np.float32:
    """+0x98 / 0x1000f1ec0 (center_dx=2) and +0xa0 / 0x1000f2418 (center_dx=0).

    24 neighbours (12 at weight 1, 12 at weight 6) plus a 36*centre term.
    The summation tree mirrors the assembly (s0..s31), not the C left-to-right
    order: the compiler groups pairs and uses one fma for the 6x sum.

    Scalar reference; the hot path is _weighted_sum_row below (bit-identical).
    """
    g = lambda yy, xx: _get(src, yy, xx)
    p12 = g(y - 2, x);      p11 = g(y - 2, x + 1)
    p14 = g(y - 2, x + 3);  p13 = g(y - 2, x + 4)
    p16 = g(y - 1, x - 2);  p15 = g(y - 1, x + 2)
    p18 = g(y + 1, x - 2);  p17 = g(y + 1, x + 2)
    p21 = g(y + 2, x - 2);  p20 = g(y + 2, x - 1)
    p23 = g(y + 2, x + 1);  p22 = g(y + 2, x + 2)
    p25 = g(y - 2, x + 2);  p24 = g(y - 1, x - 1)
    p27 = g(y - 1, x);      p26 = g(y - 1, x + 1)
    p29 = g(y, x);          p28 = g(y, x + 1)
    p31 = g(y, x + 3);      p30 = g(y, x + 4)
    p33 = g(y + 1, x - 1);  p32 = g(y + 1, x)
    p35 = g(y + 1, x + 1);  p34 = g(y + 2, x)
    # s0
    s = f32(f32(p11 + p12) + f32(p14 + p13))
    t = f32(f32(p16 + p15) + p18)
    s = f32(s + t)
    t = f32(f32(f32(p17 + p21) + p20) + p23)
    s = f32(s + t)
    s = f32(s + p22)
    # s1 (weight-6 group), same tree as the assembly
    u = f32(f32(p24 + p25) + f32(p27 + p26))
    v = f32(f32(p29 + p28) + p31)
    u = f32(u + v)
    v = f32(f32(f32(p30 + p33) + p32) + p35)
    u = f32(u + v)
    u = f32(u + p34)
    s = fma(u, f32(6.0), s)
    return f32(s + f32(g(y, x + center_dx) * f32(36.0)))


def _s(sl: slice, off: int) -> slice:
    """The same step-2 range shifted by `off` (row or column)."""
    return slice(sl.start + off, sl.stop + off, sl.step)


def _weighted_sum_rows(p: np.ndarray, rs: slice, cs: slice, center_dx: int) -> np.ndarray:
    """Vector form of weighted_sum over a strided row/column block of `p` (padded by 5)."""
    def g(dy, dx):
        return p[_s(rs, 5 + dy), _s(cs, 5 + dx)]

    p12 = g(-2, 0);  p11 = g(-2, 1)
    p14 = g(-2, 3);  p13 = g(-2, 4)
    p16 = g(-1, -2); p15 = g(-1, 2)
    p18 = g(1, -2);  p17 = g(1, 2)
    p21 = g(2, -2);  p20 = g(2, -1)
    p23 = g(2, 1);   p22 = g(2, 2)
    p25 = g(-2, 2);  p24 = g(-1, -1)
    p27 = g(-1, 0);  p26 = g(-1, 1)
    p29 = g(0, 0);   p28 = g(0, 1)
    p31 = g(0, 3);   p30 = g(0, 4)
    p33 = g(1, -1);  p32 = g(1, 0)
    p35 = g(1, 1);   p34 = g(2, 0)
    s = (p11 + p12) + (p14 + p13)
    t = (p16 + p15) + p18
    s = s + t
    t = ((p17 + p21) + p20) + p23
    s = s + t
    s = s + p22
    u = (p24 + p25) + (p27 + p26)
    v = (p29 + p28) + p31
    u = u + v
    v = ((p30 + p33) + p32) + p35
    u = u + v
    u = u + p34
    s = fma(u, f32(6.0), s)
    return s + (g(0, center_dx) * f32(36.0))


def fill_candidates(dst: Plane, src: Plane, roi: ROI):
    """+0xa8 / 0x1000f2970.  Fill even/odd rows from the two weighted sums.

    Row pairs: (y, y+1), then y += 2, for y in [y0+2, y1-2).
    The two row parities are processed as strided blocks (same sum tree).
    """
    x0, y0, x1, y1 = roi
    p = np.pad(src.data, 5)                     # Sony's getter sees 0 outside
    d, sd = dst.data, src.data
    cs = slice(x0 + 2, x1 - 2, 2)
    cs1 = slice(x0 + 3, x1 - 1, 2)
    rs0 = slice(y0 + 2, y1 - 2, 2)
    rs1 = slice(y0 + 3, y1 - 1, 2)
    xs = np.arange(x0 + 2, x1 - 2, 2)
    if xs.size:
        xend = int(xs[-1]) + 2
        # even rows (first=True): both taps scaled by 1/32
        d[rs0, cs] = _weighted_sum_rows(p, rs0, cs, 0) * K1_32
        d[rs0, cs1] = _weighted_sum_rows(p, rs0, cs1, 2) * K1_32
        # odd rows (first=False): 1/88
        d[rs1, cs] = _weighted_sum_rows(p, rs1, cs, 2) * K1_88
        d[rs1, cs1] = _weighted_sum_rows(p, rs1, cs1, 0) * K1_88
    else:
        xend = x0 + 2
    for rs in (rs0, rs1):
        d[rs, x0] = sd[rs, x0]
        d[rs, x0 + 1] = sd[rs, x0 + 1]
        d[rs, xend] = sd[rs, x0 + 2]
        d[rs, xend + 1] = sd[rs, x0 + 3]


K9_A = np.array([-0.0625, 0.0, 0.25, 0.0, 0.625, 0.0, 0.25, 0.0, -0.0625], np.float32)
K9_B = np.array([0.0, -0.0556640625, 0.0, 0.5556640625, 0.0, 0.5556640625, 0.0,
                 -0.0556640625, 0.0], np.float32)


def _dot9(k, vals):
    s = f32(k[0] * vals[0])
    for i in range(1, 9):
        s = fma(k[i], vals[i], s)
    return s


def _dot9_vec(k, cols):
    """Vector form of _dot9: nine equally shaped arrays, same fma order."""
    s = (k[0] * cols[0]).astype(np.float32)
    for i in range(1, 9):
        s = fma(k[i], cols[i], s)
    return s


def nine_tap(outA: Plane, outB: Plane, src: Plane, roi: ROI,
             k1=K9_A, k2=K9_B):
    """+0xf0 / 0x1000f4838.  Separable 9-tap, two output rows per iteration.

    Row y uses source rows y-4..y+4: outB gets the vertical filter, outA the
    horizontal one; even x uses k2, odd x uses k1.
    Row y+1 uses rows y-3..y+5 with the kernels swapped.
    The two row parities are processed as strided blocks (same fma order).
    """
    x0, y0, x1, y1 = roi
    s = src.data
    A, B = outA.data, outB.data
    cs = slice(x0 + 4, x1 - 4, 2)
    cs1 = slice(x0 + 5, x1 - 3, 2)
    rs0 = slice(y0 + 4, y1 - 4, 2)
    rs1 = slice(y0 + 5, y1 - 3, 2)
    if len(np.arange(cs.start, cs.stop, 2)) == 0:
        return
    # even rows
    B[rs0, cs] = _dot9_vec(k2, [s[_s(rs0, -4 + i), cs] for i in range(9)])
    A[rs0, cs] = _dot9_vec(k2, [s[rs0, _s(cs, -4 + i)] for i in range(9)])
    B[rs0, cs1] = _dot9_vec(k1, [s[_s(rs0, -4 + i), cs1] for i in range(9)])
    A[rs0, cs1] = _dot9_vec(k1, [s[rs0, _s(cs, -3 + i)] for i in range(9)])
    # odd rows (y+1): kernels swapped
    B[rs1, cs] = _dot9_vec(k1, [s[_s(rs1, -4 + i), cs] for i in range(9)])
    A[rs1, cs] = _dot9_vec(k1, [s[rs1, _s(cs, -4 + i)] for i in range(9)])
    B[rs1, cs1] = _dot9_vec(k2, [s[_s(rs1, -4 + i), cs1] for i in range(9)])
    A[rs1, cs1] = _dot9_vec(k2, [s[rs1, _s(cs, -3 + i)] for i in range(9)])


def _pair4(a, b, c, d):
    return f32(f32(a + b) + f32(c + d))


def five_by_five(out: Plane, src: Plane, roi: ROI):
    """+0xc8 / 0x1000f31d0.  Directional 5x5 interpolation, two rows per step.

    Even output: 8 neighbours * -1/32 + 4 axial * 5/16.
    Odd output: 4 corners * -1/64 + 4 axial * -1/32 + 4 diagonals * 1/8
                + centre * 11/16.
    The two row parities are processed as strided blocks (same fma order).
    """
    x0, y0, x1, y1 = roi
    s = src.data
    d = out.data
    neg32 = f32(-0.03125)
    w516 = f32(0.3125)
    neg64 = f32(-0.015625)
    w18 = f32(0.125)
    w1116 = f32(0.6875)

    def even_at(rs, cs):
        e = [s[_s(rs, -2), _s(cs, 1)], s[_s(rs, -2), _s(cs, -1)],
             s[_s(rs, -1), _s(cs, -2)], s[_s(rs, -1), _s(cs, 2)],
             s[_s(rs, 1), _s(cs, -2)], s[_s(rs, 1), _s(cs, 2)],
             s[_s(rs, 2), _s(cs, -1)], s[_s(rs, 2), _s(cs, 1)]]
        sum8 = ((((e[0] + e[1]) + (e[2] + e[3])) +
                 ((e[4] + e[5]) + e[6])) + e[7])
        c = [s[rs, _s(cs, -1)], s[_s(rs, -1), cs], s[rs, _s(cs, 1)], s[_s(rs, 1), cs]]
        return fma(_pair4(*c), w516, (sum8 * neg32))

    def odd_at(rs, cs):
        corners = [s[_s(rs, -2), _s(cs, 2)], s[_s(rs, -2), _s(cs, -2)],
                   s[_s(rs, 2), _s(cs, -2)], s[_s(rs, 2), _s(cs, 2)]]
        axial = [s[rs, _s(cs, -2)], s[_s(rs, -2), cs], s[rs, _s(cs, 2)], s[_s(rs, 2), cs]]
        diag = [s[_s(rs, -1), _s(cs, 1)], s[_s(rs, -1), _s(cs, -1)],
                s[_s(rs, 1), _s(cs, -1)], s[_s(rs, 1), _s(cs, 1)]]
        v = _pair4(*corners) * neg64
        v = fma(_pair4(*axial), neg32, v)
        v = fma(_pair4(*diag), w18, v)
        return fma(s[rs, cs], w1116, v)

    cs = slice(x0 + 2, x1 - 2, 2)
    cs1 = slice(x0 + 3, x1 - 1, 2)
    rs0 = slice(y0 + 2, y1 - 2, 2)
    rs1 = slice(y0 + 3, y1 - 1, 2)
    if len(np.arange(cs.start, cs.stop, 2)) == 0:
        return
    d[rs0, cs] = even_at(rs0, cs)
    d[rs0, cs1] = odd_at(rs0, cs1)
    d[rs1, cs] = odd_at(rs1, cs)
    d[rs1, cs1] = even_at(rs1, cs1)


def _err3(c, a, b):
    """|a-b| + |c-a| + |(c-2a)+b|, the robust second-difference measure."""
    t = f32(abs(f32(a - b)) + abs(f32(c - a)))
    return f32(t + abs(f32(f32(c - f32(a + a)) + b)))


def _err3_vec(c, a, b):
    """Vector form of _err3."""
    t = np.abs(a - b) + np.abs(c - a)
    return t + np.abs((c - (a + a)) + b)


_DM_CHUNK = 128


def direction_map(out: Plane, src: Plane, roi: ROI, params: bytes):
    """+0xd0 / 0x1000f36a4 -> 0x1000f3724.  Direction-selection weight.

    Builds four directional error maps (horizontal, vertical, two diagonals)
    and turns them into a per-pixel weight in the output plane.
    params is the 0x1003b96ed block.
    """
    x0, y0, x1, y1 = roi
    w, h = src.w, src.h
    eh = Plane(w, h)
    ev = Plane(w, h)
    e1 = Plane(w, h)
    e2 = Plane(w, h)
    s = src.data
    H, V, D1, D2 = eh.data, ev.data, e1.data, e2.data

    # pass 1: directional errors
    if x1 - 4 > x0 + 2 and y1 - 4 > y0 + 2:
        ys = slice(y0 + 2, y1 - 4)
        H[ys, x0 + 2:x1 - 4] = _err3_vec(
            s[ys, x0 + 2:x1 - 4], s[ys, x0 + 4:x1 - 2], s[ys, x0 + 6:x1])
        V[ys, x0 + 2:x1 - 4] = _err3_vec(
            s[ys, x0 + 2:x1 - 4], s[y0 + 4:y1 - 2, x0 + 2:x1 - 4],
            s[y0 + 6:y1, x0 + 2:x1 - 4])
        D1[ys, x0 + 4:x1 - 2] = _err3_vec(
            s[ys, x0 + 4:x1 - 2], s[y0 + 3:y1 - 3, x0 + 3:x1 - 3],
            s[y0 + 4:y1 - 2, x0 + 2:x1 - 4])
        D2[ys, x0 + 2:x1 - 4] = _err3_vec(
            s[ys, x0 + 2:x1 - 4], s[y0 + 3:y1 - 3, x0 + 3:x1 - 3],
            s[y0 + 4:y1 - 2, x0 + 4:x1 - 2])

    k1 = f32(struct.unpack_from("<f", params, 0x11)[0])
    k2 = f32(struct.unpack_from("<f", params, 0x15)[0])
    k3 = f32(struct.unpack_from("<f", params, 0x1d)[0])
    k4 = f32(struct.unpack_from("<f", params, 0x21)[0])
    k5 = f32(struct.unpack_from("<f", params, 0x25)[0])
    k6 = f32(struct.unpack_from("<f", params, 0x19)[0])
    T_EVEN = np.array([0.03125, 0.0625] * 4, np.float32)
    T_ODD = np.array([0.0625, 0.03125] * 4, np.float32)
    d = out.data

    xs_all = np.arange(x0 + 4, x1 - 4)
    if xs_all.size == 0:
        return
    xb = (xs_all - x0 - 4) & 7
    te, to = T_EVEN[xb], T_ODD[xb]
    quarter = f32(0.25)
    eps = f32(0.01)
    k3f, k4f, k5f, k6f = f32(k3), f32(k4), f32(k5), f32(k6)

    for ya in range(y0 + 4, y1 - 4, _DM_CHUNK):
        yb = min(ya + _DM_CHUNK, y1 - 4)

        def at(A, dy, dx):
            """Chunk block of A shifted by (dy, dx) (plain slices, no index arrays)."""
            return A[ya + dy:yb + dy, x0 + 4 + dx:x1 - 4 + dx]

        # sum over D1: first four terms in float32, then fma(4x) and the
        # remaining four in double (assembly mixes s and d arithmetic)
        t1 = at(D1, -2, -1); t2 = at(D1, -2, -2); t3 = at(D1, -2, 0)
        t4 = at(D1, -1, -2); t5 = at(D1, -1, -1); t6 = at(D1, -1, 0)
        t7 = at(D1, 0, -2); t8 = at(D1, 0, -1); t9 = at(D1, 0, 0)
        tf = ((t1 + t2) + (t3 + t4)).astype(np.float32)
        d17 = tf.astype(np.float64) + 4.0 * t5.astype(np.float64)
        d18 = ((t6.astype(np.float64) + t7.astype(np.float64))
               + t8.astype(np.float64)) + t9.astype(np.float64)
        tt = (d17 + d18).astype(np.float32)
        f42 = fma(quarter, tt, -k1)
        f42 = np.fmax(f42, k2)

        # sum over D2
        t1 = at(D2, -2, 1); t2 = at(D2, -2, 0); t3 = at(D2, -2, 2)
        t4 = at(D2, -1, 0); t5 = at(D2, -1, 1); t6 = at(D2, -1, 2)
        t7 = at(D2, 0, 0); t8 = at(D1, 0, 1); t9 = at(D2, 0, 2)
        tf = ((t1 + t2) + (t3 + t4)).astype(np.float32)
        d17 = tf.astype(np.float64) + 4.0 * t5.astype(np.float64)
        d18 = ((t6.astype(np.float64) + t7.astype(np.float64))
               + t8.astype(np.float64)) + t9.astype(np.float64)
        tt = (d17 + d18).astype(np.float32)
        f45 = fma(quarter, tt, -k1)
        f45 = np.fmax(f45, k2)

        f42 = (f45 + f42 + eps).astype(np.float32)
        zero = f42 == 0.0

        sc = np.where((np.arange(ya, yb) & 1)[:, None] == 0,
                      te[None, :], to[None, :]).astype(np.float32)

        # The two large directional sums follow the assembly tree exactly:
        # fcvt from float32, then double adds/fmadds, one term fused in
        # float32 before widening, and a final fcvt + fnmsub.
        a_m1 = at(V, -3, -2); a_0 = at(V, -3, -1); a_p1 = at(V, -3, 0)
        a_p2 = at(V, -3, 1); a_p3 = at(V, -3, 2)
        b_m2 = at(V, -2, -2); b_0 = at(V, -2, 0)
        b_p1 = at(V, -2, 1); b_p2 = at(V, -2, 2)
        c_m2 = at(V, -1, -2); c_m1 = at(V, -1, -1); c_0 = at(V, -1, 0)
        c_p1 = at(V, -1, 1); c_p2 = at(V, -1, 2)
        t32 = fma(a_0, f32(2.0), a_m1)                 # fmadd s21, s21, #2, s22
        d20 = b_0.astype(np.float64) + a_p1.astype(np.float64)
        d20 = d20 + c_0.astype(np.float64)
        d22 = c_m1.astype(np.float64) + a_p2.astype(np.float64)
        d22 = d22 + c_p1.astype(np.float64)
        d21 = a_p3.astype(np.float64) + t32.astype(np.float64)
        d21 = b_m2.astype(np.float64) * 9.0 + d21
        d21 = b_p1.astype(np.float64) * 8.0 + d21
        d23 = b_p2.astype(np.float64) + c_m2.astype(np.float64)
        d23 = d23 + c_p2.astype(np.float64)
        d21 = d21 + d23
        d20 = d20 * 6.0 + d21
        d20 = d22 * 2.0 + d20
        f45b = fma(sc, d20.astype(np.float32), -k1)
        f45b = np.fmax(f45b, k2)

        p_m1 = at(H, -1, -3); p_0 = at(H, -1, -2); p_p1 = at(H, -1, -1)
        q_m1 = at(H, -2, -3); q_0 = at(H, -2, -2); q_p1 = at(H, -2, -1)
        s_m1 = at(H, 0, -3); s_0 = at(H, 0, -2); s_p1 = at(H, 0, -1)
        t_m1 = at(H, 1, -3); t_0 = at(H, 1, -2); t_p1 = at(H, 1, -1)
        u_m1 = at(H, 2, -3); u_0 = at(H, 2, -2); u_p1 = at(H, 2, -1)
        fq = fma(p_m1, f32(2.0), (q_0 + q_m1) + q_p1)  # fmadd s23, s26, #2, s23
        d21 = p_0.astype(np.float64) * 8.0 + fq.astype(np.float64)
        d21 = d21 + (t_0 * f32(8.0)).astype(np.float64)   # fmul s28, s28, #8
        d24 = s_0.astype(np.float64) + s_m1.astype(np.float64)
        d24 = d24 + s_p1.astype(np.float64)
        d21 = d24 * 6.0 + d21
        d22 = t_m1.astype(np.float64) + p_p1.astype(np.float64)
        d22 = d22 + t_p1.astype(np.float64)
        d23 = u_m1.astype(np.float64) + u_0.astype(np.float64)
        d23 = d23 + u_p1.astype(np.float64)
        d21 = d21 + d23
        d21 = d22 * 2.0 + d21
        f43b = fma(sc, d21.astype(np.float32), -k1)
        f43b = np.fmax(f43b, k2)

        num = ((f43b + f45b).astype(np.float64) + 0.01).astype(np.float32)
        quot = np.full(f42.shape, f32(1.0), np.float32)
        np.divide(num, f42, out=quot, where=~zero)

        v = ((k3f - quot) + k4f) * k5f
        res = np.where(v > k3f, k3f, np.fmax(v, k6f))
        d[ya:yb, x0 + 4:x1 - 4] = res


def build_candidates(src: Plane, width: int, height: int, roi: ROI,
                     table1, table2, params: bytes):
    """+0x118 / 0x1000f5674.  Build three direction-blended candidate planes."""
    p3, p5, p6, p7, p8, p9 = (Plane(width, height) for _ in range(6))
    h_second_diff(p3, p6, src, roi)
    fill_candidates(p8, p6, roi)
    v_second_diff(p5, p7, src, roi)
    fill_candidates(p9, p7, roi)
    outA, outB, outC = Plane(width, height), Plane(width, height), Plane(width, height)
    directional_weight(outA, p8, p9, table1, roi)
    directional_weight(outB, p8, p9, table2, roi)
    blend_factor(outC, p8, p9, params, roi)
    return outA, outB, outC


def refine_green(src: Plane, width: int, height: int, roi: ROI,
                 w6: Plane, w7: Plane, k1, k2, params: bytes):
    """+0x120 / 0x1000f61e0.  Refine the green plane.

    w6 = first candidate plane from +0x118 (weight for the 1-2-1/average mix),
    w7 = third candidate plane (weight for the final lerp).
    """
    mixA, mixB = Plane(width, height), Plane(width, height)
    mix_121_average(mixA, mixB, src, roi)
    tapA, tapB = Plane(width, height), Plane(width, height)
    nine_tap(tapA, tapB, src, roi, k1, k2)
    avgA, avgB = Plane(width, height), Plane(width, height)
    pair_average(avgA, avgB, mixA, mixB, tapA, tapB, roi)
    g0 = Plane(width, height)
    lerp1(g0, avgA, avgB, w6, roi, shrink=4)

    # checkerboard copy of the raw CFA: even rows keep odd x, odd rows even x
    mosaic = Plane(width, height)
    x0, y0, x1, y1 = roi
    mosaic.data[y0:y1:2, x0 + 1:x1:2] = src.data[y0:y1:2, x0 + 1:x1:2]
    mosaic.data[y0 + 1:y1:2, x0:x1:2] = src.data[y0 + 1:y1:2, x0:x1:2]
    five = Plane(width, height)
    five_by_five(five, mosaic, roi)
    dm = Plane(width, height)
    direction_map(dm, mosaic, roi, params)
    out = Plane(width, height)
    lerp2(out, g0, five, w7, dm, roi)
    return out


def _blend(base, other, a, b, w):
    """base + w * (other - (a + b)), with the assembly's fsub/fadd/fmadd order."""
    t = f32(a + b)
    u = f32(other - t)
    return fma(w, u, base)


def _soft_weight_vec(a, b, c):
    """Vector form of soft_weight."""
    d1 = np.abs(a - c)
    d2 = np.abs(b - c)
    den = d1 + d2
    out = np.full(den.shape, f32(0.5), np.float32)
    np.divide(d2, den, out=out, where=den != 0)
    return out


def chroma_candidates(h121: Plane, v121: Plane, havg: Plane, vavg: Plane, roi: ROI):
    """+0x180 / 0x1000f7b10.  Four chroma-opponent candidate planes.

    Rows come in pairs: row y in the first block, row y+1 in the second.
    Each block blends the two lines around the local average with a soft
    weight, and stores the four horizontal/vertical combinations.
    The two row parities are processed as strided blocks.
    """
    x0, y0, x1, y1 = roi
    outA, outB, outC, outD = (Plane(h121.w, h121.h) for _ in range(4))
    H, V, HA, VA = h121.data, v121.data, havg.data, vavg.data
    oA, oB, oC, oD = outA.data, outB.data, outC.data, outD.data

    cs = slice(x0 + 2, x1 - 2, 2)
    cs1 = slice(x0 + 3, x1 - 1, 2)
    cs2 = slice(x0 + 4, x1, 2)
    rs0 = slice(y0 + 2, y1 - 2, 2)
    rs1 = slice(y0 + 3, y1 - 1, 2)
    if len(np.arange(cs.start, cs.stop, 2)) == 0:
        return outA, outB, outC, outD

    # block 1, even x (row y, columns X)
    hd = HA[_s(rs0, 1), cs] - H[_s(rs0, 1), cs]
    w = _soft_weight_vec(H[_s(rs0, -1), cs], H[_s(rs0, 1), cs], HA[rs0, cs])
    oA[rs0, cs] = H[rs0, cs] - HA[rs0, cs]
    oB[rs0, cs] = V[rs0, cs] - VA[rs0, cs]
    oC[rs0, cs] = _blend(hd, HA[_s(rs0, -1), cs], H[_s(rs0, -1), cs], hd, w)
    vd = VA[rs0, cs1] - V[rs0, cs1]
    w = _soft_weight_vec(V[rs0, _s(cs, -1)], V[rs0, cs1], VA[rs0, cs])
    oD[rs0, cs] = _blend(vd, VA[rs0, _s(cs, -1)], V[rs0, _s(cs, -1)], vd, w)
    # block 1, odd x (row y, columns X1)
    hd = H[_s(rs0, 1), cs1] - HA[_s(rs0, 1), cs1]
    w = _soft_weight_vec(HA[_s(rs0, -1), cs1], HA[_s(rs0, 1), cs1], H[rs0, cs1])
    oA[rs0, cs1] = HA[rs0, cs1] - H[rs0, cs1]
    vd = V[rs0, cs2] - VA[rs0, cs2]
    wv = _soft_weight_vec(VA[rs0, cs], VA[rs0, cs2], V[rs0, cs1])
    oB[rs0, cs1] = _blend(vd, V[rs0, cs], VA[rs0, cs], vd, wv)
    oC[rs0, cs1] = _blend(hd, H[_s(rs0, -1), cs1], HA[_s(rs0, -1), cs1], hd, w)
    oD[rs0, cs1] = VA[rs0, cs1] - V[rs0, cs1]

    # block 2, even x (row y+1, columns X)
    hd = H[_s(rs1, 1), cs] - HA[_s(rs1, 1), cs]
    w = _soft_weight_vec(HA[_s(rs1, -1), cs], HA[_s(rs1, 1), cs], H[rs1, cs])
    oA[rs1, cs] = _blend(hd, H[_s(rs1, -1), cs], HA[_s(rs1, -1), cs], hd, w)
    oB[rs1, cs] = VA[rs1, cs] - V[rs1, cs]
    oC[rs1, cs] = HA[rs1, cs] - H[rs1, cs]
    vd = V[rs1, cs1] - VA[rs1, cs1]
    wv = _soft_weight_vec(VA[rs1, _s(cs, -1)], VA[rs1, cs1], V[rs1, cs])
    oD[rs1, cs] = _blend(vd, V[rs1, _s(cs, -1)], VA[rs1, _s(cs, -1)], vd, wv)
    # block 2, odd x (row y+1, columns X1)
    hd = HA[_s(rs1, 1), cs1] - H[_s(rs1, 1), cs1]
    w = _soft_weight_vec(H[_s(rs1, -1), cs1], H[_s(rs1, 1), cs1], HA[rs1, cs1])
    oA[rs1, cs1] = _blend(hd, HA[_s(rs1, -1), cs1], H[_s(rs1, -1), cs1], hd, w)
    vd = VA[rs1, cs2] - V[rs1, cs2]
    wv = _soft_weight_vec(V[rs1, cs], V[rs1, cs2], VA[rs1, cs1])
    oB[rs1, cs1] = _blend(vd, VA[rs1, cs], V[rs1, cs], vd, wv)
    oC[rs1, cs1] = H[rs1, cs1] - HA[rs1, cs1]
    oD[rs1, cs1] = V[rs1, cs1] - VA[rs1, cs1]
    return outA, outB, outC, outD


def _default_params() -> bytes:
    """The 0x1003b96ed constant block, as written by 0x1000f9528."""
    p = bytearray(0x40)
    p[0] = 1
    struct.pack_into("<f", p, 0x01, 8.0)
    struct.pack_into("<f", p, 0x05, 0.0)
    struct.pack_into("<d", p, 0x09, 2.0 ** -15)
    struct.pack_into("<d", p, 0x11, 2.0 ** -7)
    struct.pack_into("<f", p, 0x19, 0.5)
    struct.pack_into("<f", p, 0x1d, 1.0)
    struct.pack_into("<f", p, 0x21, 0.2)
    struct.pack_into("<f", p, 0x25, 0.8)
    p[0x29] = 1
    struct.pack_into("<f", p, 0x2a, 0.0625)
    struct.pack_into("<f", p, 0x2e, 8.0)
    struct.pack_into("<f", p, 0x32, 0.00390625)
    return bytes(p)


PARAMS_DEFAULT = _default_params()
TABLE_DEFAULT = np.array([1.0, 1.0, 0.125, 0.5, 0.5], np.float32)


def _itp_scale(raw, black, gains, scale_7311) -> Plane:
    """ZcTaskITP scaling (FUN_1000f13bc): float32, RGGB index order."""
    raw = np.asarray(raw)
    h, w = raw.shape
    idx = np.empty((h, w), np.int64)
    for yy in range(2):
        for xx in range(2):
            idx[yy::2, xx::2] = xx | (yy << 1)
    f17 = np.float32(np.float32(scale_7311) * np.float32(3.0517578e-05))
    k = np.array([np.float32(f17 * np.float32(g)) for g in gains], np.float32)
    d = (raw.astype(np.int64) - np.asarray(black, np.int64)[idx]).astype(np.float32)
    out = ((k[idx] * np.float32(0.0625)).astype(np.float32) * d).astype(np.float32)
    return Plane(w, h, out)


def build_chroma(src: Plane, width: int, height: int, roi: ROI, weight: Plane,
                 params: bytes = PARAMS_DEFAULT):
    """+0x198 / 0x1000f8f70.  Return the R-G and B-G planes."""
    h121 = Plane(width, height); h_121(h121, src, roi)
    v121 = Plane(width, height); v_121(v121, src, roi)
    havg = Plane(width, height); h_average(havg, src, roi)
    vavg = Plane(width, height); v_average(vavg, src, roi)
    oA, oB, oC, oD = chroma_candidates(h121, v121, havg, vavg, roi)
    rg = Plane(width, height); lerp1(rg, oA, oB, weight, roi)
    bg = Plane(width, height); lerp1(bg, oC, oD, weight, roi)
    return rg, bg


def itp_process(raw, black, gains, scale_7311, roi=None, params: bytes = PARAMS_DEFAULT,
                table1=TABLE_DEFAULT, table2=TABLE_DEFAULT, k1=K9_A, k2=K9_B):
    """ZcTaskITP Process 0x1000f95d0 for one tile.

    Returns (R, G, B) uint16 planes and the inset ROI (10 px per side).
    raw is the CFA crop; black/gains are Sony's (R,G,G,B) arrays.
    """
    h, w = np.asarray(raw).shape
    src = _itp_scale(raw, black, gains, scale_7311)
    r = roi or ROI(0, 0, w, h)
    candA, candB, candC = build_candidates(src, w, h, r, table1, table2, params)
    g = refine_green(src, w, h, r, candA, candC, k1, k2, params)
    rg, bg = build_chroma(src, w, h, r, candB, params)

    def finish(v):
        v = np.fmin(v, np.float32(16383.0))
        v = np.maximum(v, np.float32(0.0))
        return v.astype(np.uint16)

    R = finish(rg.data + g.data)
    G = finish(g.data)
    B = finish(bg.data + g.data)
    out_roi = ROI(r.x0 + 10, r.y0 + 10, r.x1 - 10, r.y1 - 10)
    return (R, G, B), out_roi


def _edge_indices(n: int, pad: int) -> np.ndarray:
    """Parity-preserving edge extension used by TileDiv at the image border.

    Left of the image the indices are 1,0,1,0,...; right of it they are
    n-2,n-1,n-2,n-1,... (assembly: x&1 on the left,
    (n-1) - ((n-1 ^ x) & 1) on the right).
    """
    idx = np.arange(-pad, n + pad)
    out = np.empty_like(idx)
    neg = idx < 0
    out[neg] = idx[neg] & 1
    big = idx >= n
    out[big] = (n - 1) - ((n - 1 ^ idx[big]) & 1)
    mid = ~(neg | big)
    out[mid] = idx[mid]
    return out


def itp_full_image(raw, black, gains, scale_7311, tile: int = 1024, halo: int = 12,
                   params: bytes = PARAMS_DEFAULT):
    """Run the ITP over a whole image using Sony's 1024x1024 tile grid.

    FUN_100139788 fixes the tile size at 0x400 x 0x400 and 0x1000de588 builds
    a non-overlapping grid clamped to the image border.  Each tile is processed
    with a halo of `halo` pixels; the Process ROI (10 px inset) then hands back
    exactly the tile.  At the image border the halo is filled with the
    parity-preserving edge extension from TileDiv.
    """
    raw = np.asarray(raw)
    h, w = raw.shape
    rows = _edge_indices(h, halo)
    cols = _edge_indices(w, halo)
    padded = raw[np.ix_(rows, cols)]
    out = [np.zeros((h, w), np.uint16) for _ in range(3)]
    for y0 in range(0, h, tile):
        y1 = min(y0 + tile, h)
        for x0 in range(0, w, tile):
            x1 = min(x0 + tile, w)
            crop = padded[y0:y1 + 2 * halo, x0:x1 + 2 * halo]
            planes, _ = itp_process(crop, black, gains, scale_7311, params=params)
            ch, cw = y1 - y0, x1 - x0
            for k in range(3):
                out[k][y0:y1, x0:x1] = planes[k][halo:halo + ch, halo:halo + cw]
    return tuple(out)


def _avg(a, b):
    return f32(f32(a + b) * f32(0.5))


def _mix121(a, b, c):
    """(a+b+6c)/8 in the assembly order: fadd, fmadd(c,6), fmul 0.125."""
    return f32(fma(c, f32(6.0), f32(a + b)) * f32(0.125))


def mix_121_average(outA: Plane, outB: Plane, src: Plane, roi: ROI):
    """+0xf8 / 0x1000f50e8.  Two output planes, 1-2-1 / average mix.

    Row pair (y, y+1), y += 2, for y in [y0+2, y1-3).
    Each row gets: averages at even x, step-2 1-2-1 at odd x.
    The two row parities are processed as strided blocks.
    """
    x0, y0, x1, y1 = roi
    s = src.data
    A, B = outA.data, outB.data
    cs = slice(x0 + 2, x1 - 2, 2)
    cs1 = slice(x0 + 3, x1 - 1, 2)
    rs0 = slice(y0 + 2, y1 - 3, 2)
    rs1 = slice(y0 + 3, y1 - 2, 2)
    if len(np.arange(cs.start, cs.stop, 2)) == 0:
        return
    B[rs0, cs] = _avg(s[_s(rs0, -1), cs], s[_s(rs0, 1), cs])
    A[rs0, cs] = _avg(s[rs0, _s(cs, -1)], s[rs0, _s(cs, 1)])
    B[rs0, cs1] = _mix121(s[_s(rs0, 2), cs1], s[_s(rs0, -2), cs1], s[rs0, cs1])
    A[rs0, cs1] = _mix121(s[rs0, _s(cs, 3)], s[rs0, _s(cs, -1)], s[rs0, cs1])
    B[rs1, cs] = _mix121(s[_s(rs1, 2), cs], s[_s(rs1, -2), cs], s[rs1, cs])
    A[rs1, cs] = _mix121(s[rs1, _s(cs, 2)], s[rs1, _s(cs, -2)], s[rs1, cs])
    B[rs1, cs1] = _avg(s[_s(rs1, -1), cs1], s[_s(rs1, 1), cs1])
    A[rs1, cs1] = _avg(s[rs1, cs], s[rs1, _s(cs, 2)])


def h_second_diff(out_a: Plane, out_abs: Plane, src: Plane, roi: ROI):
    """+0x70 / 0x1000f15b8.  Horizontal second difference, step 2.

    out_a[y,x]   = (src[y,x-2] + src[y,x+2] - 2*src[y,x]) * -0.25
    out_abs[y,x] = |out_a| * ((x ^ y) & 1)
    Written for x in [x0+2, x1-2), all y in [y0, y1).  Other positions untouched.
    """
    x0, y0, x1, y1 = roi
    s = src.data
    out_a.data.fill(0.0)
    a = out_a.data
    b = out_abs.data
    if x1 - 2 <= x0 + 2:
        return
    ys = slice(y0, y1)
    c = s[ys, x0 + 2:x1 - 2]
    v = fma(c, f32(-2.0), s[ys, x0:x1 - 4] + s[ys, x0 + 4:x1]) * f32(-0.25)
    a[ys, x0 + 2:x1 - 2] = v
    X = np.arange(x0 + 2, x1 - 2)
    Y = np.arange(y0, y1)[:, None]
    par = ((X[None, :] ^ Y) & 1).astype(np.float32)
    b[ys, x0 + 2:x1 - 2] = np.abs(v) * par


def v_second_diff(out_a: Plane, out_abs: Plane, src: Plane, roi: ROI | None = None):
    """+0x78 / 0x1000f173c.  Vertical second difference, step 2.

    This function takes no ROI: it walks the whole output plane, rows
    [2, h-2), and clears out_a first.  The fifth argument in the original
    call is unused by the callee.
    """
    w, h = out_a.w, out_a.h
    s = src.data
    out_a.data.fill(0.0)
    a = out_a.data
    b = out_abs.data
    if h <= 4:
        return
    c = s[2:h - 2, :]
    v = fma(c, f32(-2.0), s[0:h - 4, :] + s[4:h, :]) * f32(-0.25)
    a[2:h - 2, :] = v
    X = np.arange(w)[None, :]
    Y = np.arange(2, h - 2)[:, None]
    par = ((X ^ Y) & 1).astype(np.float32)
    b[2:h - 2, :] = np.abs(v) * par


def h_121(dst: Plane, src: Plane, roi: ROI):
    """+0x158 / 0x1000f76e0.  Horizontal 1-2-1 at step 2: (a+b*6+c)/8.

    Writes x in [x0+2, x1-2); copies the first and last two pixels of the row.
    """
    x0, y0, x1, y1 = roi
    d = dst.data
    s = src.data
    ys = slice(y0, y1)
    d[ys, x0:x0 + 2] = s[ys, x0:x0 + 2]
    if x1 - 2 > x0 + 2:
        v = (fma(s[ys, x0 + 2:x1 - 2], f32(6.0), s[ys, x0:x1 - 4])
             + s[ys, x0 + 4:x1]) * f32(0.125)
        d[ys, x0 + 2:x1 - 2] = v
    d[ys, x1 - 2:x1] = s[ys, x1 - 2:x1]


def v_121(dst: Plane, src: Plane, roi: ROI):
    """+0x160 / 0x1000f77e0.  Vertical 1-2-1 at step 2, rows [y0+2, y1-2)."""
    x0, y0, x1, y1 = roi
    d = dst.data
    s = src.data
    if y1 - 2 <= y0 + 2 or x1 - 2 <= x0 + 2:
        return
    v = (fma(s[y0 + 2:y1 - 2, x0 + 2:x1 - 2], f32(6.0), s[y0:y1 - 4, x0 + 2:x1 - 2])
         + s[y0 + 4:y1, x0 + 2:x1 - 2]) * f32(0.125)
    d[y0 + 2:y1 - 2, x0 + 2:x1 - 2] = v


def h_average(dst: Plane, src: Plane, roi: ROI):
    """+0x168 / 0x1000f7934.  (left + right) / 2, x in [x0+2, x1-2)."""
    x0, y0, x1, y1 = roi
    if x1 - 2 <= x0 + 2:
        return
    ys = slice(y0, y1)
    dst.data[ys, x0 + 2:x1 - 2] = (src.data[ys, x0 + 1:x1 - 3]
                                   + src.data[ys, x0 + 3:x1 - 1]) * f32(0.5)


def v_average(dst: Plane, src: Plane, roi: ROI):
    """+0x170 / 0x1000f7a10.  (up + down) / 2, rows [y0+2, y1-2)."""
    x0, y0, x1, y1 = roi
    if y1 - 2 <= y0 + 2:
        return
    dst.data[y0 + 2:y1 - 2, x0:x1] = (src.data[y0 + 1:y1 - 3, x0:x1]
                                      + src.data[y0 + 3:y1 - 1, x0:x1]) * f32(0.5)


def soft_weight(a: float, b: float, c: float) -> np.float32:
    """+0x178 / 0x1000f9fdc.  |b-c| / (|a-c| + |b-c|), 0.5 when the sum is 0."""
    a, b, c = f32(a), f32(b), f32(c)
    den = f32(abs(f32(a - c)) + abs(f32(b - c)))
    if den == 0.0:
        return f32(0.5)
    return f32(abs(f32(b - c)) / den)


def lerp_blend(dst: Plane, base: Plane, lo: Plane, hi: Plane, weight: Plane, roi: ROI):
    """+0x188 / 0x1000f8540.  dst = fma(weight, hi-lo, base)."""
    x0, y0, x1, y1 = roi
    ys, xs = slice(y0, y1), slice(x0, x1)
    dst.data[ys, xs] = fma(weight.data[ys, xs],
                           hi.data[ys, xs] - lo.data[ys, xs],
                           base.data[ys, xs])


def lerp1(dst: Plane, base: Plane, other: Plane, weight: Plane, roi: ROI, shrink: int = 0):
    """+0x100 / 0x1000f5534 (shrink=4) and +0x108 / 0x1000f5f50 (shrink=0).
    dst = fma(weight, other-base, base).

    f5534 walks y in [y0+4, y1-4) but x in [x0, x1-8): Sony insets only the
    right side in x.  This asymmetry is in the assembly (loop count (x1-x0)-8).
    """
    x0, y0, x1, y1 = roi
    ys, xs = slice(y0 + shrink, y1 - shrink), slice(x0, x1 - 2 * shrink)
    d, b, o, w = dst.data, base.data, other.data, weight.data
    d[ys, xs] = fma(w[ys, xs], o[ys, xs] - b[ys, xs], b[ys, xs])


def lerp2(dst: Plane, base: Plane, other: Plane, w1: Plane, w2: Plane, roi: ROI):
    """+0x110 / 0x1000f6074.  dst = fma(max(w1,w2), other-base, base)."""
    x0, y0, x1, y1 = roi
    ys, xs = slice(y0, y1), slice(x0, x1)
    a, c = w1.data[ys, xs], w2.data[ys, xs]
    w = np.where(a > c, a, c)
    dst.data[ys, xs] = fma(w, other.data[ys, xs] - base.data[ys, xs], base.data[ys, xs])


def pair_average(oa: Plane, ob: Plane, a1: Plane, b1: Plane, a2: Plane, b2: Plane, roi: ROI):
    """+0xe0 / 0x1000f5d90.  oa=(a2+a1)/2, ob=(b2+b1)/2."""
    x0, y0, x1, y1 = roi
    ys, xs = slice(y0, y1), slice(x0, x1)
    oa.data[ys, xs] = (a2.data[ys, xs] + a1.data[ys, xs]) * f32(0.5)
    ob.data[ys, xs] = (b2.data[ys, xs] + b1.data[ys, xs]) * f32(0.5)


def pair_average_clamped(oa: Plane, ob: Plane, a1: Plane, b1: Plane, a2: Plane, b2: Plane,
                         upper: Plane, lower: Plane, roi: ROI):
    """+0xd8 / 0x1000f5af0.  Average each pair, then max(min(v, upper), lower)."""
    x0, y0, x1, y1 = roi
    ys, xs = slice(y0, y1), slice(x0, x1)
    for out, p, q in ((oa, a2, a1), (ob, b2, b1)):
        v = (p.data[ys, xs] + q.data[ys, xs]) * f32(0.5)
        u = upper.data[ys, xs]
        v = np.where(v < u, v, u)              # fminnm
        lo = lower.data[ys, xs]
        out.data[ys, xs] = np.where(v > lo, v, lo)


def directional_weight(dst: Plane, a: Plane, b: Plane, coef, roi: ROI):
    """+0xb8 / 0x1000f2c08.  Normalised positive part of two directional votes.

    coef = [k0, k1, eps]; den = (max(a-k0,0)+eps) + eps + max(b-k1,0).
    """
    x0, y0, x1, y1 = roi
    k0, k1, eps = f32(coef[0]), f32(coef[1]), f32(coef[2])
    ys, xs = slice(y0, y1), slice(x0, x1)
    av, bv = a.data[ys, xs], b.data[ys, xs]
    t1 = np.where(av - k0 <= 0.0, f32(0.0), av - k0) + eps
    t2 = np.where(bv - k1 <= 0.0, f32(0.0), bv - k1)
    den = (t1 + eps) + t2
    out = np.full(den.shape, f32(0.5), np.float32)
    np.divide(t1, den, out=out, where=den > 0.0)
    dst.data[ys, xs] = out


def blend_factor(dst: Plane, a: Plane, b: Plane, params: bytes, roi: ROI):
    """+0xc0 / 0x1000f2fec.  min(0.5*min(r,1/r), 1), r=(8+8a)/(8+8b).

    params is the raw byte block at 0x1003b96ed (see phase16/README.md §4.2).
    """
    k = f32(struct.unpack_from("<f", params, 1)[0])     # 8.0
    lo = f32(struct.unpack_from("<f", params, 5)[0])    # 0.0 (0x96f2)
    hi = f32(struct.unpack_from("<f", params, 9)[0])    # 0.0 (0x96f6)
    half = f32(struct.unpack_from("<f", params, 0xd)[0])  # 0.5 (0x96fa)
    x0, y0, x1, y1 = roi
    ys, xs = slice(y0, y1), slice(x0, x1)
    av, bv = a.data[ys, xs], b.data[ys, xs]
    num = k + av * f32(8.0)
    den = k + bv * f32(8.0)
    r = (num / den).astype(np.float32)
    with np.errstate(divide="ignore", invalid="ignore"):
        inv_hi = f32(1.0) / r
    inv = np.where(r > 1.0, inv_hi, r)
    v = lo + (inv - lo) * half
    res = np.where(v < f32(1.0), v, f32(1.0))
    dst.data[ys, xs] = np.where(lo <= r, res, hi)
