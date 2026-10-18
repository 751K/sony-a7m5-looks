#!/usr/bin/env python3
"""Sony lens geometry correction (ZcTaskGeoCorr, process 0x100140b34): distortion + lateral CA.

Lens struct L (render+0x18) built by FUN_1000d58f0 -> FUN_1000d65a0 (mode 1, look+0x12fc/0x12fe
!= -1) from SR2 0x7984 (distortion, 19 knots), 0x7980 (CA), 0x7983:
    L+0x08/0x10/0x18  float tables R/G/B (G[i] = 1 + 0x7984[i]/32768)
    L+0x20 s, L+0x28 s2 (fill-frame zoom), L+0x3c/0x40 centre, L+0x44 knot spacing (px),
    L+0x2c..0x38 R/B lateral offsets from FUN_1000d58f0's shift block:
                  0x78a2 (look+0x12e8..), 0x78a3 (look+0x12f0), the decoder rect and
                  0x7922 (look+0x12f4/0x12f8); f0 + FUN_1000bd904 == 0x10 doubles dx/dy.
    centre = integer half of the decoder rect (vt+0x88); 0x7922 gives the same size for
    full frame.  look+8 = 1 (0x7983[1] == 16, full frame) or 2 (== 11, APS-C) selects
    21.63 / 14.2.  Distortion / CA switches come from dev+0x278 (0x8040) and dev+0x27c
    (0x900d) through FUN_1000aed68 / FUN_1000aed80.
For every output pixel p (whole-image coordinates) and channel k (FUN_1000d6b04 / 0x1000d6b20):
    r = |p - c|;  t = s2*r / L44;  f = lerp(table_k, t)  (last knot beyond the table)
    src = c + s*f*((p + off_k) - c)
then a bilinear read of input channel k at src - tile_origin, truncated to u16.
"""
import numpy as np

from sony_look import fma32

f32 = np.float32


def _lerp_base(n, t):
    """Index, fraction and in-range mask shared by the three channel tables."""
    i = np.floor(np.maximum(t, 0)).astype(np.int64)       # fcvtmu
    inside = (i < n - 1)
    ic = np.clip(i, 0, n - 2)
    frac = (t - ic.astype(f32)).astype(f32)
    return ic, frac, inside


def _lerp_from(tab, n, ic, frac, inside):
    a, b = tab[ic].astype(f32), tab[ic + 1].astype(f32)
    v = fma32((b - a).astype(f32), frac, a)
    return np.where(inside, v, f32(tab[n - 1])).astype(f32)


def _lerp_table(tab, n, t):
    return _lerp_from(tab, n, *_lerp_base(n, t))


def _coords_base(px, py, L):
    """Per-pixel terms that do not depend on the channel: centre, radius, table index."""
    cx, cy = f32(L["cx"]), f32(L["cy"])
    dx, dy = (px - cx).astype(f32), (py - cy).astype(f32)
    r = np.sqrt(fma32(dy, dy, (dx * dx).astype(f32))).astype(f32)
    t = ((f32(L["s2"]) * r).astype(f32) / f32(L["step"])).astype(f32)
    return (cx, cy) + _lerp_base(L["n"], t)


def _channel_coords(px, py, L, k, cx, cy, ic, frac, inside):
    f = _lerp_from(np.asarray(L["tables"][k], f32), L["n"], ic, frac, inside)
    ox, oy = (L["off"][k] if k != 1 else (f32(0), f32(0)))
    s = f32(L["s"])
    sx = fma32((f * ((f32(ox) + px).astype(f32) - cx).astype(f32)).astype(f32), s, cx)
    sy = fma32((f * ((f32(oy) + py).astype(f32) - cy).astype(f32)).astype(f32), s, cy)
    return sx, sy


def src_coords(px, py, L, k):
    """px, py: float32 output coordinates; L: dict from lens_struct(); k: 0 R, 1 G, 2 B."""
    return _channel_coords(px, py, L, k, *_coords_base(px, py, L))


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
    # radius and table index depend only on the output pixel, not the channel
    base = _coords_base(px, py, L)
    out = []
    for k in range(3):
        sx, sy = _channel_coords(px, py, L, k, *base)
        out.append(bilinear_u16(planes[k], (sx - f32(origin[0])).astype(f32), (sy - f32(origin[1])).astype(f32)))
    return out


# --- lens struct from tags (FUN_1000d65a0 mode 1 + FUN_1000d69b4) ---------------------------------

K_MM = f32(0.69348127)          # 0x3f3187fd: knots per mm of image height


def _interp_g(G, x):
    i = int(np.floor(max(float(x), 0.0)))
    if i < 18:
        return fma32(f32(G[i + 1] - G[i]), f32(x - f32(i)), f32(G[i]))
    return f32(G[18])


def lens_switch(v):
    """FUN_1000aed68 / FUN_1000aed80: is the correction on for dev+0x278 / dev+0x27c?
    On for -1 (auto) and 1; off for 0, 2, 3 and anything else."""
    u = (int(v) + 1) & 0xFFFFFFFF
    return (u & 1) == 0 and u < 4


def _shift_offsets(shift, shift_div, rect, crop, scale2=False):
    """FUN_1000d58f0's lens-shift block: look+0x12e8.. (0x78a2), 0x12f0 (0x78a3),
    0x12f4/0x12f8 (0x7922), the decoder rect, and the f0/0x10 doubling."""
    if not shift_div or not crop[0] or not crop[1]:
        return (f32(0.0), f32(0.0)), (f32(0.0), f32(0.0))
    dx = f32(f32(rect[2] - rect[0]) / f32(crop[0]))
    dy = f32(f32(rect[3] - rect[1]) / f32(crop[1]))
    if scale2:
        dx, dy = f32(dx + dx), f32(dy + dy)
    inv = f32(f32(1.0) / f32(f32(shift_div) * f32(256.0)))
    tdx, tdy = f32(dx * f32(1000.0)), f32(dy * f32(1000.0))
    return ((f32(tdx * f32(f32(shift[0]) * inv)), f32(tdy * f32(f32(shift[1]) * inv))),
            (f32(tdx * f32(f32(shift[2]) * inv)), f32(tdy * f32(f32(shift[3]) * inv))))


def lens_struct_from_tags(dist, ca_r, ca_b, rect, p5=1, look8=0, dist_on=True, ca_on=True,
                          shift=(0, 0, 0, 0), shift_div=0, crop=None, scale2=False):
    """dist: 0x7984 knots (without the leading count); ca_r / ca_b: the two halves of 0x7980;
    rect: decoder effective rectangle (x0, y0, x1, y1) from vt+0x88;
    look8: look+8 (1 full frame, 2 APS-C); crop: the 0x7922 (width, height) denominators
    (defaults to the rect size); shift: 0x78a2, shift_div: 0x78a3."""
    n = len(dist)
    crop = tuple(crop) if crop is not None else (rect[2] - rect[0], rect[3] - rect[1])
    half_w, half_h = (rect[2] - rect[0]) // 2, (rect[3] - rect[1]) // 2
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
    offR, offB = _shift_offsets(shift, shift_div, rect, crop, scale2)
    return dict(mode=1, n=n, tables=[R, G, B], s=res, s2=res, cx=cx, cy=cy, step=step,
                off=[offR, (f32(0.0), f32(0.0)), offB])


def lens_from_arw(arw, rect=None, dist_setting=None, ca_setting=None, p5=1, f0=False, zoom2=False):
    """Lens struct for an ARW.

    SR2 0x7984 (distortion), 0x7980 (CA), 0x7922 (crop width/height), 0x7983 (mode:
    0x7983[1] == 16 full frame, 11 APS-C -> look+8), 0x78a2/0x78a3 (lens shift).
    rect: decoder effective rectangle (vt+0x88); None uses
    decoder_params.crop_rect(arw) = DefaultCropOrigin (0xc61f) + DefaultCropSize (0xc620),
    which is exact for cropped modes too.  dist_setting/ca_setting are dev+0x278/+0x27c
    (settings 0x8040 / 0x900d); None reads them from an Edit-saved ARW's develop
    settings, falling back to -1 (auto/on) and 1 (on).
    """
    import sr2
    if dist_setting is None or ca_setting is None:
        import develop_settings as DS
        st = DS.read_settings(arw)
        if dist_setting is None:
            dist_setting = int(st.get(DS.DISTORTION, -1))
        if ca_setting is None:
            ca_setting = int(st.get(DS.CA_CORRECTION, 1))
    tags = sr2.sr2_subifd(str(arw))
    dist = tags[0x7984][1][1:]
    ca = tags[0x7980][1][1:]
    half = len(ca) // 2
    crop = tuple(tags[0x7922][1][:2])
    if rect is None:
        import decoder_params as DP
        rect = DP.crop_rect(arw)
    rect = tuple(rect)
    mode = tags[0x7983][1][1]
    look8 = 2 if mode == 11 else 1 if mode == 16 else 0
    return lens_struct_from_tags(dist, ca[:half], ca[half:], rect, p5=p5, look8=look8,
                                 dist_on=lens_switch(dist_setting), ca_on=lens_switch(ca_setting),
                                 shift=tuple(tags[0x78a2][1][:4]), shift_div=tags[0x78a3][1][0],
                                 crop=crop, scale2=bool(f0 and zoom2))


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
