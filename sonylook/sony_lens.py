#!/usr/bin/env python3
"""Sony lens geometry correction (ZcTaskGeoCorr, process 0x100140b34): distortion + lateral CA.

Lens struct L (render+0x18) built by FUN_1000d58f0 -> FUN_1000d65a0 (mode 1, look+0x12fc/0x12fe
!= -1) from SR2 0x7984 (distortion, 19 knots), 0x7980 (CA), 0x7983:
    L+0x08/0x10/0x18  float tables R/G/B (G[i] = 1 + 0x7984[i]/32768)
    L+0x20 s, L+0x28 s2 (fill-frame zoom), L+0x3c/0x40 centre, L+0x44 knot spacing (px),
    L+0x2c..0x38 R/B lateral offsets (int bits, 0 here)
For every output pixel p (whole-image coordinates) and channel k (FUN_1000d6b04 / 0x1000d6b20):
    r = |p - c|;  t = s2*r / L44;  f = lerp(table_k, t)  (last knot beyond the table)
    src = c + s*f*((p + off_k) - c)
then a bilinear read of input channel k at src - tile_origin, truncated to u16.
"""
import numpy as np

from sony_look import fma32

f32 = np.float32


def _lerp_table(tab, n, t):
    i = np.floor(np.maximum(t, 0)).astype(np.int64)       # fcvtmu
    inside = (i < n - 1)
    ic = np.clip(i, 0, n - 2)
    a, b = tab[ic].astype(f32), tab[ic + 1].astype(f32)
    v = fma32((b - a).astype(f32), (t - ic.astype(f32)).astype(f32), a)
    return np.where(inside, v, f32(tab[n - 1])).astype(f32)


def src_coords(px, py, L, k):
    """px, py: float32 output coordinates; L: dict from lens_struct(); k: 0 R, 1 G, 2 B."""
    cx, cy = f32(L["cx"]), f32(L["cy"])
    dx, dy = (px - cx).astype(f32), (py - cy).astype(f32)
    r = np.sqrt(fma32(dy, dy, (dx * dx).astype(f32))).astype(f32)
    t = ((f32(L["s2"]) * r).astype(f32) / f32(L["step"])).astype(f32)
    f = _lerp_table(np.asarray(L["tables"][k], f32), L["n"], t)
    ox, oy = (L["off"][k] if k != 1 else (f32(0), f32(0)))
    s = f32(L["s"])
    sx = fma32((f * ((f32(ox) + px).astype(f32) - cx).astype(f32)).astype(f32), s, cx)
    sy = fma32((f * ((f32(oy) + py).astype(f32) - cy).astype(f32)).astype(f32), s, cy)
    return sx, sy


def bilinear_u16(plane, fx, fy):
    """GeoCorr u16 path: index clamping and weight order as in the original."""
    H, W = plane.shape
    ix, iy = np.trunc(fx).astype(np.int64), np.trunc(fy).astype(np.int64)
    x0 = np.where(ix <= W - 1, np.maximum(ix, 0), W - 1)
    x1 = np.where(x0 + 1 < W - 1, x0 + 1, W - 1)
    y0 = np.where(iy <= H - 1, np.maximum(iy, 0), H - 1)
    y1 = np.where(y0 + 1 < H - 1, y0 + 1, H - 1)
    p = plane.astype(f32)
    v00, v01, v10, v11 = p[y0, x0], p[y0, x1], p[y1, x0], p[y1, x1]
    X0, X1, Y0, Y1 = (a.astype(f32) for a in (x0, x1, y0, y1))
    ax, bx = (fx - X0).astype(f32), (X1 - fx).astype(f32)
    top = fma32(bx, v00, (ax * v01).astype(f32))                 # asm 0x10014155c..
    bot = fma32(ax, v11, (bx * v10).astype(f32))
    v = fma32(bot, (fy - Y0).astype(f32), (top * (Y1 - fy).astype(f32)).astype(f32))
    return (np.trunc(v).astype(np.int64) & 0xffff).astype(np.uint16)


def geocorr_u16(planes, origin, region, L):
    """planes: 3 x (H, W) uint16 input tile whose (0,0) is whole-image `origin` (x, y);
    region: output (x0, y0, x1, y1) in whole-image coordinates. Returns 3 x (y1-y0, x1-x0)."""
    x0, y0, x1, y1 = region
    px, py = np.meshgrid(np.arange(x0, x1).astype(f32), np.arange(y0, y1).astype(f32))
    out = []
    for k in range(3):
        sx, sy = src_coords(px, py, L, k)
        out.append(bilinear_u16(planes[k], (sx - f32(origin[0])).astype(f32), (sy - f32(origin[1])).astype(f32)))
    return out


# --- lens struct from tags (FUN_1000d65a0 mode 1 + FUN_1000d69b4) ---------------------------------

K_MM = f32(0.69348127)          # 0x3f3187fd: knots per mm of image height


def _interp_g(G, x):
    i = int(np.floor(max(float(x), 0.0)))
    if i < 18:
        return fma32(f32(G[i + 1] - G[i]), f32(x - f32(i)), f32(G[i]))
    return f32(G[18])


def lens_struct_from_tags(dist, ca_r, ca_b, half_w, half_h, p5=1, look8=0, dist_on=True, ca_on=True,
                          offsets=((0, 0), (0, 0))):
    """dist: 0x7984 knots (without the leading count); ca_r / ca_b: the two halves of 0x7980;
    half_w / half_h: integer half sizes of the decoder rect (vt+0x88), i.e. 3504 / 2336."""
    n = len(dist)
    G, R, B = (np.zeros(n, f32) for _ in range(3))
    for i in range(n):
        d7 = float(dist[i]) * 3.0517578125e-05 + 1.0 if dist_on else 1.0
        cr = float(ca_r[min(i, len(ca_r) - 1)]) * 4.76837158203125e-07 + 1.0 if ca_on else 1.0
        cb = float(ca_b[min(i, len(ca_b) - 1)]) * 4.76837158203125e-07 + 1.0 if ca_on else 1.0
        G[i], R[i], B[i] = f32(d7), f32(cr * d7), f32(cb * d7)
    s8 = f32(14.2) if look8 == 2 else f32(21.63)
    l24 = f32(s8 * K_MM)
    s9 = _interp_g(G, l24)
    hx, hy, fp5 = f32(half_w), f32(half_h), f32(p5)
    cx, cy = f32(hx / fp5), f32(hy / fp5)
    rr = f32(np.sqrt(fma32(hx, hx, f32(hy * hy))))
    step = f32(rr / f32(l24 * fp5))
    mm = f32(s8 / rr)
    if s9 >= f32(1.0):
        lo, best, y = f32(rr * f32(0.5)), f32(0.0), rr
        if rr > lo:
            k = f32(mm * K_MM)
            while True:
                v = f32(_interp_g(G, f32(k * y)) * y)
                cand = f32(y + f32(0.5))
                if not (v >= rr):
                    best = cand
                y = f32(y - f32(1.0))
                if not (v >= rr) or y <= lo:
                    break
        res = f32(best / rr) if best != 0 else _interp_g(G, f32(f32(rr * K_MM) * mm))
    else:
        hi, best, y = f32(hy * f32(1.3)), f32(0.0), hy
        if hi > hy:
            k = f32(mm * K_MM)
            while True:
                v = f32(_interp_g(G, f32(k * y)) * y)
                cand = f32(y - f32(0.5))
                if v > hy:
                    best = cand
                y = f32(y + f32(1.0))
                if v > hy or y >= hi:
                    break
        res = f32(best / hy) if best != 0 else _interp_g(G, f32(f32(hy * K_MM) * mm))
    asf = lambda v: np.frombuffer(np.int32(v).tobytes(), "<f4")[0]
    return dict(mode=1, n=n, tables=[R, G, B], s=res, s2=res, cx=cx, cy=cy, step=step,
                off=[(asf(offsets[0][0]), asf(offsets[0][1])), (0, 0), (asf(offsets[1][0]), asf(offsets[1][1]))])


def lens_from_arw(arw):
    """Lens struct for an ARW (SR2 0x7984 / 0x7980), default develop settings (distortion + CA on)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raw_structure"))
    import sr2
    tags = sr2.sr2_subifd(str(arw))
    dist = tags[0x7984][1][1:]
    ca = tags[0x7980][1][1:]
    half = len(ca) // 2
    return lens_struct_from_tags(dist, ca[:half], ca[half:], 3504, 2336)


def geocorr_downscaled(img, L, s):
    """Approximate box_s(GeoCorr(full-res)) on an image already box-downsampled by s: every output
    block centre is mapped through the exact source-coordinate function and the downsampled input
    is sampled bilinearly (float, no truncation)."""
    H, W, _ = img.shape
    cy, cx = (np.arange(H) * s + (s - 1) / 2.0).astype(f32), (np.arange(W) * s + (s - 1) / 2.0).astype(f32)
    px, py = np.meshgrid(cx, cy)
    out = np.empty_like(img)
    for k in range(3):
        sx, sy = src_coords(px, py, L, k)
        fx = (sx.astype(np.float64) - (s - 1) / 2.0) / s
        fy = (sy.astype(np.float64) - (s - 1) / 2.0) / s
        x0 = np.clip(np.floor(fx).astype(int), 0, W - 2); y0 = np.clip(np.floor(fy).astype(int), 0, H - 2)
        ax = np.clip(fx - x0, 0, 1); ay = np.clip(fy - y0, 0, 1)
        c = img[..., k]
        out[..., k] = ((1 - ay) * ((1 - ax) * c[y0, x0] + ax * c[y0, x0 + 1]) +
                       ay * ((1 - ax) * c[y0 + 1, x0] + ax * c[y0 + 1, x0 + 1]))
    return out
