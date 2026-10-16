#!/usr/bin/env python3
"""Sony DRO (ZcTaskVatr, process 0x1000e65a4): local tone mapping with a bilateral grid.

Analysis (FUN_1000d52cc -> render+0xc01c0; ctor 0x1000fb758, FUN_1000fb9a4(.., 8, 6, 14), FUN_1000fc3e0):
  1. FUN_1000fc734: every 4th Bayer quad of the decoded raw buffer (7168x5120, origin centred) gives
     v = (max(R-bR,0)*0.1495 + (max(G-bG,0) + max(B-bB,0))*0.057) * k54, L = log2(v) via table.
  2. FUN_1000fd814: 16x15-sample means -> 112x84 sub-cells; 14x14 sub-cells per cell -> 8x6 cells.
  3. per cell: 14-bin histogram of sub-cell L (FUN_1000fc3e0), 3x3 cell smoothing (FUN_1000fda44),
     bin blur w[|j-k|] -> W[j], S[j] (weighted bin centroid, pulled to j at the ends by blend[j]).
  4. curve (FUN_1000fe258): 104 entries at 1/8 log2 step from a piecewise cubic Bezier.
     If look+0x10f0 is set (ARW SR2 0x781b/0x781c present) the camera's knots/control points are
     used (u16/1024); otherwise a built-in level table.
Per pixel (Vatr): Y = 2*(0.1495 R + 0.2935 G + 0.057 B) on (u16>>1); L = log2 Y;
  (W, S) = bilinear over the 4 nearest cell centres x linear over bins at L; local = S/W;
  gain = exp2(((L - local)*strength - L) + curve(local))   (strength 1 when dev+0x238 == 1)
  out = ((u16>>1) * (1 + (gain - 1)*k)) clamped to [0, 16383], truncated, << 1.
"""
import struct

import numpy as np

from sony_look import fma32

f32 = np.float32

COEF = (f32(0.1495), f32(0.2935), f32(0.057))          # dro+0x48/0x4c/0x50 (0x3e191687 ...)
K54 = f32(1.0)
LOG_STEP = f32(8.0)                                      # dro+0x496c
EXP_STEP = f32(0.0068359375)                             # dro+0x4970
BIN_W = np.array([0.8828129768371582, 0.6054689884185791, 0.3242189884185791, 0.1367190033197403,
                  0.04296899959445, 0.011718999594449997, 0.0039059999398887157, 0.0039059999398887157,
                  0.0039059999398887157], f32)           # 0x1002ecff8 -> dro+0xe0
BLEND = np.array([1, .75, .5, .25, 0, 0, 0, 0, 0, .25, .5, .75, 1, 1], f32)   # 0x1002ed088 -> dro+0x104



def log_table():
    """dro+0x96c: t[0] = 0, t[j] = log2f(8 j) (j < 2048); t[2048] is the first entry of the exp2 table
    that follows in memory (exp2f(0) = 1), read by the lerp for 16376 <= v < 16384."""
    j = np.arange(1, 2048)
    t = np.zeros(2049, f32)
    t[1:2048] = np.log2((LOG_STEP * j.astype(f32)).astype(np.float64)).astype(f32)
    t[2048] = f32(1.0)
    return t


LOG_T = log_table()
LOG2_MAX = f32(np.log2(np.float64(f32(K54 * f32(8191.0)))))     # dro+0x70 (FUN_1000fb9a4)


def log_lookup(v):
    """FUN_1000fe760 / inline in FUN_1000fc734 (v already multiplied by k54)."""
    v = np.asarray(v, f32)
    out = np.zeros(v.shape, f32)
    hi = v >= f32(16384.0)
    mid = (v >= 0) & ~hi
    t = (v[mid] / LOG_STEP).astype(f32)
    i = t.astype(np.int64)
    fr = (t - np.trunc(t)).astype(f32)
    a = LOG_T[i]
    out[mid] = fma32(fr, (LOG_T[i + 1] - a).astype(f32), a)
    out[hi] = np.log2(v[hi].astype(np.float64)).astype(f32)
    return out


# ---------------------------------------------------------------------------------------------
# analysis: grid + curve

def grid_geometry(w, h, nx=8, ny=6, nsub=14, step=4):
    """FUN_1000fc3e0 / FUN_1000fc734 sizes for a raw buffer of w x h."""
    sx, sy = nsub * nx, nsub * ny                       # 112, 84 sub-cells
    subpx_x = ((w >> 2) // sx) * 4
    subpx_y = ((h >> 2) // sy) * 4
    used_x, used_y = subpx_x * sx, subpx_y * sy
    return dict(nx=nx, ny=ny, nsub=nsub, step=step, sub_x=sx, sub_y=sy, subpx_x=subpx_x, subpx_y=subpx_y,
                org_x=((w - used_x) >> 1) & 0xfffe, org_y=((h - used_y) >> 1) & 0xfffe,
                ns_x=used_x // step, ns_y=used_y // step, per_x=subpx_x // step, per_y=subpx_y // step)


def raw_samples(raw, geo, black=(512, 512, 512)):
    """FUN_1000fc734 (u16 raw, dro+0x46 = 1): log-luminance of every step-th RGGB quad."""
    p = raw.astype(np.int64)
    ys = geo["org_y"] + geo["step"] * np.arange(geo["ns_y"])
    xs = geo["org_x"] + geo["step"] * np.arange(geo["ns_x"])
    Y, X = np.meshgrid(ys, xs, indexing="ij")
    r = np.maximum((p[Y, X] - black[0]).astype(f32), f32(0))
    g = np.maximum((((p[Y + 1, X] + p[Y, X + 1]) >> 1) - black[1]).astype(f32), f32(0))
    b = np.maximum((p[Y + 1, X + 1] - black[2]).astype(f32), f32(0))
    v = (fma32((b + g).astype(f32), COEF[2], (r * COEF[0]).astype(f32)) * K54).astype(f32)
    return log_lookup(v)


def subcell_means(samples, geo):
    """FUN_1000fd814: sequential fma accumulation, rows outer, x inner."""
    px, py = geo["per_x"], geo["per_y"]
    inv = f32(f32(1.0) / f32(px * py))
    s = samples.reshape(geo["sub_y"], py, geo["sub_x"], px)
    acc = np.zeros((geo["sub_y"], geo["sub_x"]), f32)
    for r in range(py):
        for c in range(px):
            acc = fma32(s[:, r, :, c], inv, acc)
    return acc


def smooth_cells(h):
    """FUN_1000fda44: 3x3 cross smoothing of the per-cell histograms (h: ny, nx, nb)."""
    ny, nx, _ = h.shape
    out = np.empty_like(h)
    for y in range(ny):
        for x in range(nx):
            c = h[y, x]
            L = h[y, x - 1] if x > 0 else None
            R = h[y, x + 1] if x < nx - 1 else None
            U = h[y - 1, x] if y > 0 else None
            D = h[y + 1, x] if y < ny - 1 else None
            # operand order of the original (first neighbour fused into c*k, then the rest)
            if y == 0 or y == ny - 1:
                V = D if y == 0 else U
                if x == 0:
                    out[y, x] = (fma32(c, f32(2), R) + V).astype(f32) * f32(0.25)
                elif x == nx - 1:
                    out[y, x] = (fma32(c, f32(2), L) + V).astype(f32) * f32(0.25)
                else:
                    out[y, x] = (fma32(c, f32(3), L) + (R + V).astype(f32)).astype(f32) * f32(0.16666667)
            elif x == 0:
                out[y, x] = (fma32(c, f32(3), R) + (U + D).astype(f32)).astype(f32) * f32(0.16666667)
            elif x == nx - 1:
                out[y, x] = (fma32(c, f32(3), L) + (U + D).astype(f32)).astype(f32) * f32(0.16666667)
            else:
                out[y, x] = (fma32(c, f32(4), L) + ((R + U).astype(f32) + D).astype(f32)).astype(f32) * f32(0.125)
    return out.astype(f32)


def curve_table(knots, ctrl):
    """FUN_1000fe258: 104 samples of the piecewise cubic Bezier (knots: 10, ctrl: 28) at
    X_i = (dro+0x70 * 1/104) * i, clamped to dro+0x70."""
    k = np.asarray(knots, f32); P = np.asarray(ctrl, f32)
    c1 = f32(LOG2_MAX * f32(0.009615385))
    out = np.zeros(104, f32)
    for i in range(104):
        X = f32(c1 * f32(i))
        s = 9
        for j in range(1, 10):
            if not (k[j] <= X):
                s = j - 1
                break
        d = f32(k[s + 1] - k[s]) if s < 9 else f32(0)
        t = f32(f32(X - k[s]) / d) if d != 0 else f32(0)
        u = f32(f32(1) - t)
        t2 = f32(t * t)
        a = fma32(-t, u, u)                                  # fmsub: u - t*u
        p0, p1, p2, p3 = P[3 * s:3 * s + 4]
        s5 = f32(a * f32(f32(3) * p1))
        s5 = fma32(p3, t2, s5)
        s7 = fma32(f32(t2 * f32(3)), p2, f32(a * p0))
        s7 = fma32(-t, s7, s7)                               # fmsub
        out[i] = min(fma32(s5, t, s7), LOG2_MAX)
    return out


def build_grid(raw, knots, ctrl, black=(512, 512, 512)):
    """Full analysis for a decoded raw buffer (uint16, h x w). Returns the grid dict used by vatr()."""
    h, w = raw.shape
    geo = grid_geometry(w, h)
    samples = raw_samples(raw, geo, black)
    sub = subcell_means(samples, geo)
    ny, nx, n = geo["ny"], geo["nx"], geo["nsub"]
    nb = 14
    cells = sub.reshape(ny, n, nx, n).transpose(0, 2, 1, 3).reshape(ny, nx, n * n)
    hist = np.zeros((ny, nx, nb), f32)
    inc = f32(f32(1.0) / f32(n * n))
    inv70 = f32(f32(1.0) / LOG2_MAX)
    for i in range(n * n):
        v = cells[..., i]
        b = np.maximum(((v * f32(nb)).astype(f32) * inv70).astype(f32), f32(0)).astype(np.int64)
        b = b - (b == nb)
        for yy in range(ny):
            for xx in range(nx):
                hist[yy, xx, b[yy, xx]] = f32(hist[yy, xx, b[yy, xx]] + inc)
    sm = smooth_cells(hist)
    W = np.zeros_like(sm); S = np.zeros_like(sm)
    for j in range(nb):
        w_ = np.zeros((ny, nx), f32); s_ = np.zeros((ny, nx), f32)
        for k in range(nb):
            wt = BIN_W[min(abs(j - k), 8)]
            hk = sm[..., k]
            w_ = fma32(hk, wt, w_)
            s_ = fma32((hk * wt).astype(f32), f32(k), s_)
        diff = fma32(w_, f32(j), -s_)                                   # fnmsub: W*j - S, one rounding
        S[..., j] = fma32(BLEND[j], diff, s_)
        W[..., j] = w_
    return dict(geo=geo, samples=samples, sub=sub, cells=cells, hist=hist, smooth=sm, S=S, W=W,
                curve=curve_table(knots, ctrl), nb=nb)


# ---------------------------------------------------------------------------------------------
# per-pixel operator

def _axis(p, c0, cw, n):
    """FUN_1000fe7d4 along one axis (unsigned arithmetic)."""
    p = np.asarray(p, np.int64)
    i0 = np.where(p < c0, 0, np.where(p < c0 + (n - 1) * cw, (p - c0) // max(cw, 1), n - 1))
    i1 = np.where(p < c0, 0, np.where(p < c0 + (n - 1) * cw, i0 + 1, n - 1))
    d0 = np.abs(p - (c0 + i0 * cw)); d1 = np.abs(p - (c0 + i1 * cw))
    return i0, i1, d0, d1


def grid_lookup(G, x, y, L):
    """FUN_1000fe7d4 + FUN_1000e6388: returns (W, S) at integer position (x, y) and log-lum L."""
    g = G["geo"]
    cw, ch = g["subpx_x"] * g["nsub"], g["subpx_y"] * g["nsub"]
    i0, i1, dx0, dx1 = _axis(x, g["org_x"] + (cw >> 1), cw, g["nx"])
    j0, j1, dy0, dy1 = _axis(y, g["org_y"] + (ch >> 1), ch, g["ny"])
    nb = G["nb"]
    L = np.asarray(L, f32)
    b = np.floor(np.maximum(L, 0)).astype(np.int64)
    f = (L - np.floor(L)).astype(f32)
    b1 = np.where(b < nb - 2, b + 1, nb - 1)
    sx, sy = dx0 + dx1, dy0 + dy1
    fdx0, fdx1, fdy0, fdy1 = (a.astype(f32) for a in (dx0, dx1, dy0, dy1))
    inv = np.where(sx != 0, f32(1) / np.maximum(sx, 1).astype(f32), f32(0)).astype(f32)

    def get(A, jj, ii, bb):
        Ap = np.concatenate([A, np.zeros(A.shape[:2] + (2,), f32)], axis=2)   # bins past the end read 0
        return Ap[jj, ii, np.minimum(bb, nb + 1)]

    res = []
    for A in (G["W"], G["S"]):
        v = []
        for bb in (b, b1):
            a0, a1, a2, a3 = get(A, j0, i0, bb), get(A, j0, i1, bb), get(A, j1, i0, bb), get(A, j1, i1, bb)
            top = np.where(sx != 0, (fma32(a1, fdx0, (a0 * fdx1).astype(f32)) * inv).astype(f32), a0)
            bot = np.where(sx != 0, (fma32(a3, fdx0, (a2 * fdx1).astype(f32)) * inv).astype(f32), a2)
            vv = np.where(sy != 0, (fma32(bot, fdy0, (top * fdy1).astype(f32)) /
                                    np.maximum(sy, 1).astype(f32)).astype(f32), top)
            v.append(vv.astype(f32))
        res.append(v)
    (w0, w1), (s0, s1) = res
    W = fma32(w1, f, fma32(-f, w0, w0))                              # fmsub then fmadd
    S = fma32(s1, f, (s0 * (f32(1) - f).astype(f32)).astype(f32))
    return W.astype(f32), S.astype(f32)


def vatr(planes, x, y, G, strength=f32(1.0), k_up=f32(1.0), k_down=f32(1.0), scale=f32(1.0)):
    """ZcTaskVatr::process (0x1000e65a4) on u16 planes R, G, B (any shape) whose pixels sit at
    integer image coordinates x, y (same shape). Returns the three u16 output planes."""
    r, g, b = ((np.asarray(p).astype(np.int64) >> 1).astype(f32) for p in planes)
    Y = fma32(COEF[2], b, fma32(COEF[1], g, (COEF[0] * r).astype(f32)))
    Y = (Y + Y).astype(f32)
    L = log_lookup((K54 * Y).astype(f32))
    px = (scale * np.asarray(x).astype(f32)).astype(f32).astype(np.int64)
    py = (scale * np.asarray(y).astype(f32)).astype(f32).astype(np.int64)
    W, S = grid_lookup(G, px, py, L)
    local = np.where(W == 0, LOG2_MAX, (S / np.where(W == 0, f32(1), W)).astype(f32)).astype(f32)
    c = G["curve"]
    idx = np.maximum((local * f32(8)).astype(f32), f32(0)).astype(np.int64)
    inside = idx < 0x67
    ic = np.minimum(idx, 0x66)
    t = (fma32(ic.astype(f32), f32(-0.125), local) * f32(8)).astype(f32)
    cv = np.where(inside, fma32(t, (c[ic + 1] - c[ic]).astype(f32), c[ic]), c[103]).astype(f32)
    e = fma32((L - local).astype(f32), strength, -L)                   # fnmsub
    e = (e + cv).astype(f32)
    gain = np.exp2(e.astype(np.float64)).astype(f32)
    k = np.where((gain - f32(1)) > 0, k_up, k_down).astype(f32)
    m = fma32(gain, k, -k)                                                # fnmsub
    out = []
    for v in (r, g, b):
        o = fma32(m, v, v)
        o = np.maximum(np.minimum(o, f32(16383.0)), f32(0))
        out.append((o.astype(np.int64) << 1).astype(np.uint16))
    return out


def vatr_gain(rgb, x, y, G, strength=f32(1.0)):
    """Float gain of the operator for linear rgb on the ITP scale (0..16383) at image coords x, y.
    Same path as vatr() without the u16 halving/truncation."""
    r, g, b = (np.asarray(rgb[..., i], np.float64) / 2 for i in range(3))
    Y = (2 * (COEF[0] * r + COEF[1] * g + COEF[2] * b)).astype(f32)
    L = log_lookup((K54 * np.maximum(Y, 0)).astype(f32))
    W, S = grid_lookup(G, np.asarray(x).astype(np.int64), np.asarray(y).astype(np.int64), L)
    local = np.where(W == 0, LOG2_MAX, S / np.where(W == 0, f32(1), W)).astype(np.float64)
    c = G["curve"].astype(np.float64)
    idx = np.maximum(local * 8, 0).astype(np.int64)
    ic = np.minimum(idx, 102)
    cv = np.where(idx < 103, c[ic] + (local * 8 - ic) * (c[ic + 1] - c[ic]), c[103])
    return np.exp2((L - local) * float(strength) - L + cv)


def vatr_downscaled(img, G, s):
    """Approximate box_s(Vatr(full res)) on an image box-downsampled by s (crop coordinates):
    gain from the block mean at the block centre; E[2*floor(floor(v/2)*g)] ~ g*v - g/2 - 1."""
    H, W_, _ = img.shape
    cy = (np.arange(H) * s + (s - 1) // 2)
    cx = (np.arange(W_) * s + (s - 1) // 2)
    X, Y = np.meshgrid(cx, cy)
    g = vatr_gain(img, X, Y, G)
    return img * g[..., None] - 0.5 * g[..., None] - 1.0, g


# ---------------------------------------------------------------------------------------------

def curve_points_from_arw(arw):
    """SR2 0x781b (10 knots) / 0x781c (28 Bezier values), u16/1024 (FUN_1000fe258 custom path)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raw_structure"))
    import sr2
    t = sr2.sr2_subifd(str(arw))
    if 0x781b not in t or 0x781c not in t:
        return None
    k = np.array(t[0x781b][1][:10], f32) * f32(0.0009765625)
    c = np.array(t[0x781c][1][:28], f32) * f32(0.0009765625)
    return k.astype(f32), c.astype(f32)


def grid_from_arw(arw, black=(512, 512, 512)):
    import rawpy
    with rawpy.imread(str(arw)) as r:
        raw = r.raw_image.copy()
    pts = curve_points_from_arw(arw)
    assert pts is not None, "no camera DRO curve (0x781b/0x781c)"
    return build_grid(raw, *pts, black=black)
