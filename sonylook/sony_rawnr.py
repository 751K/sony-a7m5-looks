"""ZcTaskRawNR (Imaging Edge Edit 4.0.00.10312): bit-exact port of the CFA-domain noise reduction.

Task ctor 0x1000e9acc, Process 0x1000e9b48, kernel 0x1000e9d04, tables FUN_1000d5ac0 (render+0xd0).
The builder FUN_1000b85e0 inserts it when dev+0x218 (setting 0x801e) == 1 and the decoder's
vtable+0x100 is non-zero; on an RGGB sensor the non-SIMD path FUN_1000b9594 is used.

Per CFA channel (input: raw u16 with black level, before demosaic):

1. Split into a high-pass `hp` and a base `b` on the same-colour lattice (region shrunk by 2):
   R/B: hp = (12c - diag2 - 2*axial2) >> 4
   G:   hp = ((24c - diag2 - 2*axial2) - 3*diag1) * 36 >> 10      (diag1 = the other G)
   b = clamp(off + c - hp, 0, 0x7fff), off_R = L18-L20, off_G = L18-(L1c+L28), off_B = L18-L24.
2. Sigma filter of `b` on a 5x5 lattice with 4-pixel spacing (region shrunk by 8 more):
   local mean m (R/B: 3x3 at spacing 4, *0x72>>10; G: 25 G samples within +-4, *0x29>>10),
   c' = (W[m]*b0 + (1024-W[m])*m) >> 10, neighbours v with (c'-v)^2 <= T[b0]^2 are averaged
   together with b0 (integer division).
3. out = clamp(clamp(gain*hp >> 8, -lim, lim) - off + avg, 0, 0x3fff), then
   final = trunc(fma32(out - raw, s, raw)) with s from ISO (0.4 below ISO 400, 1.0 from 1600).
"""
import numpy as np

f32 = np.float32

# look offset <- SR2SubIFD tag (parser 0x1000a4adc..0x1000a4dc0)
LOOK_TAGS = {0x1018: 0x78c0, 0x101c: 0x78c1, 0x1020: 0x78c2, 0x1024: 0x78c3, 0x1028: 0x78c4,
             0x102c: 0x78c5, 0x1030: 0x78c6, 0x1034: 0x78c7, 0x1038: 0x78c8,
             0x103c: 0x78c9, 0x1040: 0x78ca, 0x1044: 0x78cb,
             0x1048: 0x78cc, 0x104c: 0x78cd, 0x1050: 0x78ce,
             0x1054: 0x78cf, 0x1058: 0x78d0, 0x105c: 0x78d1,
             0x1090: 0x78d5, 0x1094: 0x78d6, 0x1098: 0x78d7}


def look_from_sr2(sr2):
    """look+0x1018.. fields from a parsed SR2SubIFD {tag: (type, values, raw)}.

    0x78cc..0x78ce (high-pass gain) are clamped to 256 by the parser; the G value above 256 becomes
    the Sharpness factor look+0x1060 = 1 + (G - 256) * 0.003125 (fmadd in float32).
    """
    look = {}
    for off, tag in LOOK_TAGS.items():
        v = sr2.get(tag)
        look[off] = int(v[1][0]) if v is not None else 0
    g = look[0x104c]
    if g > 256:
        look[0x1060] = float(f32(f32(g - 256) * f32(0.003125) + f32(1.0)))
        look[0x1048] = min(look[0x1048], 256)
        look[0x104c] = 256
        if look[0x1050] > 256:
            look[0x1050] = 256
    return look


def strength(iso):
    """Process 0x1000e9b48: DAT_1002ecfe8.. = (400, 0.4f, 1600, 1.0f), float32 interpolation."""
    if iso < 400:
        return f32(0.4)
    if iso >= 1600:
        return f32(1.0)
    return f32(f32(f32(f32(1.0) - f32(0.4)) * f32(f32(iso) - f32(400))) / f32(f32(1600) - f32(400)) + f32(0.4))


def _fma64(a, b, c):
    a, b, c = float(a), float(b), float(c)
    from fractions import Fraction
    return float(Fraction(a) * Fraction(b) + Fraction(c))


def tables(look, s21c=0, s220=0, s224=0):
    """FUN_1000d5ac0(render+0xd0, look). s21c/s220/s224 = dev+0x21c/0x220/0x224 (settings 0x8027,
    0x8028, 0x8029). Returns thresholds T[3][0x8000], blend W[3] (constant tables), gain[3], lim[3]."""
    d = _fma64(float(s21c), 0.0003125, 0.03125)
    g = [int(float(look[o]) * 32.0 * d) for o in (0x103c, 0x1040, 0x1044)]
    w = int(_fma64(float(s21c), 5.12, 512.0))
    if s220 < 0:
        s = float(s220)
        gain = [int((_fma64(float(look[o]), 0.00390625, s * _fma64(float(look[o]), 3.90625e-05, -0.01))) * 256.0)
                for o in (0x1048, 0x104c, 0x1050)]
        lim = min(float(look[0x1054]), 8184.0)
        lim = _fma64(s, _fma64(lim, 0.01, -81.84), lim)
    else:
        s = float(s220)
        gain = [int((float(look[o]) * 0.00390625 - s * float(look[o]) * 3.90625e-05) * 256.0)
                for o in (0x1048, 0x104c, 0x1050)]
        lim = min(float(look[0x1054]), 8184.0)
    lim = [int(lim)] * 3
    lo, hi = look[0x102c], look[0x1030]
    a3, b3 = look[0x1034] * 3, look[0x1038] * 3
    i = np.arange(0x8000, dtype=np.int64)
    i9 = np.where(i <= hi, np.maximum(i, lo), hi) - lo
    T = [np.clip((gc * a3 >> 8) + ((i9 * (b3 * gc >> 8)) >> 12), 0, 0x3fff) for gc in g]
    return dict(T=T, W=[w] * 3, gain=gain, lim=lim, g=g)


def _i16(v):
    return ((v + 0x8000) & 0xffff) - 0x8000


def _fma32_trunc(out, raw, s):
    """trunc(fmadd(out - raw, s, raw)) in float32 (one rounding)."""
    d = out.astype(np.float64) - raw.astype(np.float64)          # exact in float32
    v = (d * float(s) + raw.astype(np.float64)).astype(np.float32)  # product exact in double; one rounding
    return np.trunc(v).astype(np.int64)


def rawnr(raw, look, iso=100, region=None, settings=(0, 0, 0), strength_override=None):
    """Run RawNR on a raw CFA image (RGGB at even/even of the region origin).

    region = (x0, y0, x1, y1) of the task (half-open). Pixels outside region shrunk by 10 keep the
    input value (Sony leaves them unwritten in a fresh image; the tiler overlaps tiles instead).
    """
    R = np.asarray(raw).astype(np.int64)
    H, W = R.shape
    x0, y0, x1, y1 = region if region is not None else (0, 0, W, H)
    tb = tables(look, *settings)
    s = strength(iso) if strength_override is None else f32(strength_override)
    L18, L1c, L20, L24, L28 = (look[o] for o in (0x1018, 0x101c, 0x1020, 0x1024, 0x1028))
    offs = {(0, 0): L18 - L20, (0, 1): L18 - (L1c + L28), (1, 0): L18 - (L1c + L28), (1, 1): L18 - L24}
    ch = {(0, 0): 0, (0, 1): 1, (1, 0): 1, (1, 1): 2}

    def sh(a, dy, dx, ys, xs):
        return a[ys.start + dy:ys.stop + dy:2, xs.start + dx:xs.stop + dx:2]

    # phase A on region shrunk by 2
    ax0, ay0, ax1, ay1 = x0 + 2, y0 + 2, x1 - 2, y1 - 2
    hp = np.zeros((H, W), np.int64)
    base = np.zeros((H, W), np.int64)
    for (py, px), off in offs.items():
        ys, xs = slice(ay0 + py, ay1), slice(ax0 + px, ax1)
        if ys.start >= ys.stop or xs.start >= xs.stop:
            continue
        c = R[ys, xs][::2, ::2]
        d2 = sh(R, -2, -2, ys, xs) + sh(R, -2, 2, ys, xs) + sh(R, 2, -2, ys, xs) + sh(R, 2, 2, ys, xs)
        a2 = sh(R, 0, -2, ys, xs) + sh(R, -2, 0, ys, xs) + sh(R, 0, 2, ys, xs) + sh(R, 2, 0, ys, xs)
        if ch[(py, px)] == 1:
            d1 = sh(R, -1, -1, ys, xs) + sh(R, -1, 1, ys, xs) + sh(R, 1, -1, ys, xs) + sh(R, 1, 1, ys, xs)
            h = ((24 * c - (d2 + 2 * a2)) - 3 * d1) * 36 >> 10
        else:
            h = (12 * c - (d2 + 2 * a2)) >> 4
        hv = hp[ys, xs]; hv[::2, ::2] = _i16(h)
        bv = base[ys, xs]; bv[::2, ::2] = np.clip(off + c - h, 0, 0x7fff)

    out = R.copy()
    bx0, by0, bx1, by1 = ax0 + 8, ay0 + 8, ax1 - 8, ay1 - 8
    grid = [(dy, dx) for dy in (-8, -4, 0, 4, 8) for dx in (-8, -4, 0, 4, 8) if (dy, dx) != (0, 0)]
    g_mean = [(0, 0), (-4, 0), (0, -4), (0, 4), (4, 0), (0, -2), (0, 2), (-2, 0), (2, 0), (-2, -2), (2, -2),
              (-2, 2), (2, 2), (-3, -1), (-3, 1), (-1, -3), (-1, -1), (-1, 1), (-1, 3), (1, -3), (1, -1), (1, 1),
              (1, 3), (3, -1), (3, 1)]
    rb_mean = [(dy, dx) for dy in (-4, 0, 4) for dx in (-4, 0, 4)]
    for (py, px), off in offs.items():
        k = ch[(py, px)]
        ys, xs = slice(by0 + py, by1), slice(bx0 + px, bx1)
        if ys.start >= ys.stop or xs.start >= xs.stop:
            continue
        b0 = base[ys, xs][::2, ::2]
        thr = tb["T"][k][b0]
        t2 = thr * thr
        if k == 1:
            m = sum(sh(base, dy, dx, ys, xs) for dy, dx in g_mean) * 0x29 >> 10
        else:
            m = sum(sh(base, dy, dx, ys, xs) for dy, dx in rb_mean) * 0x72 >> 10
        Wm = tb["W"][k]
        cp = (Wm * b0 + (0x400 - Wm) * m) >> 10
        acc = b0.copy()
        n = np.ones_like(b0)
        for dy, dx in grid:
            v = sh(base, dy, dx, ys, xs)
            ok = (cp - v) * (cp - v) <= t2
            acc += np.where(ok, v, 0)
            n += ok
        avg = acc // n
        t = (tb["gain"][k] * hp[ys, xs][::2, ::2]) >> 8
        lim = tb["lim"][k]
        t = np.where(t <= -lim, -lim, t)
        t = np.where(t > lim, lim, t)
        o = np.clip(t - off + avg, 0, 0x3fff)
        ov = out[ys, xs]
        ov[::2, ::2] = _fma32_trunc(o, R[ys, xs][::2, ::2], s)
    return out.astype(np.uint16)
