#!/usr/bin/env python3
"""Sony Y-plane detail stages: ZcTaskSharpness (0x1000ef77c) and ZcTaskSpica (0x100100b60).

Both run on plane 0 (Y) of the YCC image, in place, over a task region (x0, y0, x1, y1).
FUN_10004f5a8 (SIMD) and FUN_10014a1bc (OpenCL) return 0, so these plain versions are the ones used.

Sharpness (between ChromaSuppress and YGamma; Y on 0..32767):
  H  = 7-tap binomial (1 6 15 20 15 6 1) along x, C = same along y of H   (sums, /4096 later)
  L  = 4Y + up + down + left + right                                      (int)
  d  = (Y - L/8) + s9 * (L/8 - C/4096)
  e  = d*s0 if d > thr, d*s1 if d < -thr, else 0
  Y' = clamp(Y + k*e, 0, 32767), truncated
  thr = dev214 + 25, s0/s1 = (dev20c/dev210 + 100)/100, s9 = dev2c4*0.015 - 0.5 (dev2c4 <= 50),
  k = look+0x1060 * 0.02 * (dev208 + 100) * (dev2c4*0.02) * s11 (obj+0x2277c == 1 -> s11 = 1).
Spica (after YGamma/YNR, before YCC2RGB; srsettings0000.tbl from Edit.app/Contents/Resources):
  9-pixel cross census -> pattern (0..255) -> (coefficient set, tap step dx, dy) -> 25-tap diamond
  filter f (x512). d = (f - 512 Y) * s; gain = 2 * min(g1(d/128), g2(max-min), g3((max+min)/2))
  t = trunc(d * gain / 512^2) + Y  (clamped 0..16383), then Y' = Y + k2 (t - Y), k2 = 0.5*0.0002*
  (dev208+100)*(100-dev2c4). The output region shrinks by 3 (filter) / 4 (task).
"""
import struct
from pathlib import Path

import numpy as np

from sony_look import fma32

f32 = np.float32
import edit_binary  # noqa: E402
SRSETTINGS = edit_binary.resources_path() / "srsettings0000.tbl"     # from the user's Edit install

# ------------------------------------------------------------------------------------------------
# Sharpness


def sharpness_params(dev208=0, dev20c=0, dev210=0, dev214=0, dev2c4=50, look1060=1.7, obj2277c=1.0,
                     obj1b4=0):
    s8 = f32(dev214 + 25) if dev214 >= 0 else f32(f32(dev214 + 100) * f32(0.25))
    s10 = f32(f32(dev2c4) * f32(0.02))
    s11 = f32(1.0)
    if f32(obj2277c) != f32(1.0):
        s2 = f32(obj2277c)
        if obj1b4 < dev208:
            s11 = f32(f32(f32(f32(s2 - f32(2)) * f32(100 - dev208)) / f32(100 - obj1b4)) + f32(2))
        else:
            s11 = s2
    d = f32(dev2c4)
    s9 = f32(d * f32(0.005)) if s10 > f32(1) else fma32(d, f32(0.015), f32(-0.5))
    s0 = f32(f32(dev20c + 100) * f32(0.01))
    s1 = f32(f32(dev210 + 100) * f32(0.01))
    k = f32(f32(f32(look1060) * f32(0.02)) * f32(dev208 + 100))
    k = f32(k * f32(s10 * s11))
    return dict(thr=f32(s8), s0=s0, s1=s1, s9=f32(s9), k=k)


def sharpness(Y, region, P):
    """Y: uint16 plane (whole tile, read outside `region` for the 3x3 cross); region (x0, y0, x1, y1).
    Returns a copy of Y with the region processed exactly as 0x1000ef77c. The task first shrinks the
    region by 2 on every side (vtable+0x50 of the region object) and runs all three passes on that."""
    x0, y0, x1, y1 = region[0] + 2, region[1] + 2, region[2] - 2, region[3] - 2
    Yi = Y.astype(np.int64)
    src = Y[y0:y1, x0:x1].astype(f32)
    h, w = src.shape
    wts = (1, 6, 15, 20, 15, 6, 1)
    # pass 1a: horizontal binomial into buffer columns c <-> element k = c - 3 (k = -3 .. w+2)
    # vectorised equivalent: for element k, contributions ordered by source s ascending: s = k-3 ... k+3,
    # source s writes weight wts[(k - s) + 3]
    Hk = np.zeros((h, w + 6), f32)
    for o in range(-3, 4):                 # o = s - k, ascending s
        wt = f32(wts[3 - o])
        cols = np.arange(w + 6) - 3 + o    # source column (region coords)
        ok = (cols >= 0) & (cols < w)
        v = np.zeros((h, w + 6), f32)
        v[:, ok] = src[:, cols[ok]]
        new = fma32(v, wt, Hk)
        Hk = np.where(ok[None, :], new, Hk).astype(f32)
    Hc = Hk[:, 3:3 + w]                    # pass 2 reads only the region columns
    # pass 1b: Laplacian-like cross (integers)
    L = (4 * Yi[y0:y1, x0:x1] + Yi[y0 - 1:y1 - 1, x0:x1] + Yi[y0 + 1:y1 + 1, x0:x1]
         + Yi[y0:y1, x0 - 1:x1 - 1] + Yi[y0:y1, x0 + 1:x1 + 1]).astype(f32)
    # pass 2: vertical binomial of H; C row r gets H rows q = r-3 .. r+3 ascending (inside region)
    C = np.zeros((h, w), f32)
    for o in range(-3, 4):
        wt = f32(wts[3 - o])
        rows = np.arange(h) + o
        ok = (rows >= 0) & (rows < h)
        v = np.zeros((h, w), f32)
        v[ok] = Hc[rows[ok]]
        new = fma32(v, wt, C)
        C = np.where(ok[:, None], new, C).astype(f32)
    # pass 3
    s7 = (C * f32(-0.000244140625)).astype(f32)
    s7 = fma32(L, f32(0.125), s7)
    s16 = fma32(-L, f32(0.125), src)
    d = fma32(s7, P["s9"], s16)
    e = np.where(d > P["thr"], (P["s0"] * d).astype(f32),
                 np.where(d < -P["thr"], (P["s1"] * d).astype(f32), f32(0))).astype(f32)
    o = fma32(P["k"], e, src)
    o = np.maximum(np.minimum(o, f32(32767.0)), f32(0))
    out = Y.copy()
    out[y0:y1, x0:x1] = o.astype(np.int64).astype(np.uint16)
    return out


# ------------------------------------------------------------------------------------------------
# Spica

def load_srsettings(path=SRSETTINGS, weights=(2048.0,) + (0.0,) * 9):
    """FUN_1000e4cb0 (+e4ddc/e4fe4/e51ec/ffeac): pattern table and 25-tap coefficient sets.
    `weights` are struct+0x10..0x34 at load time (mode-2 block 0x1002ecc04: 2048, 0 ...)."""
    b = Path(path).read_bytes()
    assert b[:14] == b"SONY RAW SRPRM"
    npat, ncls, ntap, nw, _ = struct.unpack_from("<5h", b, 14)
    assert (npat, ncls, ntap, nw) == (0x100, 100, 25, 10)
    off = 24
    pat = []
    for i in range(npat):
        idx, dx, dy = struct.unpack_from("<Hbb", b, off + 4 * i)
        pat.append((idx, dx, dy))
    off += 4 * npat
    raw = np.frombuffer(b, "<i2", ncls * 240, off).astype(np.int64).reshape(ncls, 24, 10)
    w = np.array(weights, f32)
    coefs = np.zeros((ncls, 26), f32)
    for c in range(ncls):
        acc = f32(0)
        t = 0
        for k in range(25):
            if k == 12:
                continue
            v = raw[c, t].astype(f32)
            s = f32(w[0] * v[0])
            for j in range(1, 10):
                s = fma32(v[j], w[j], s)
            coefs[c, 1 + k] = f32(s * f32(0.00048828125))
            acc = fma32(s, f32(0.00048828125), acc)
            t += 1
        coefs[c, 1 + 12] = f32(f32(512.0) - acc)
    return dict(patterns=np.array(pat, np.int64), coefs=coefs)


# mode-1 parameter block 0x1002ecdb4 (FUN_1000e5370(1)), 0xd8 bytes
MODE1 = bytes.fromhex(
    "01000000000000410000003f01000000002000000000000000080000000000000000000000000000000000000000"
    "000000000000000000000000000000000000080000002400000080000000000200000000803e0000803e08000000"
    "2400000080000000000200000000803e0000803e020000000400000008000000200000003000000000020000000"
    "0403e0000803e0000000030000000b0000000000200000000003d0000003e000080450000004500008044900100"
    "0040060000006400009001000040060000006400000000803f0000803f0000403f")


def spica_params(iso=100, block=MODE1):
    """FUN_1000e50f8 + FUN_100100900 + the strength interpolation in 0x100100b60."""
    I = lambda o: struct.unpack_from("<i", block, o)[0]
    U = lambda o: struct.unpack_from("<I", block, o)[0]
    F = lambda o: f32(struct.unpack_from("<f", block, o)[0])
    S = {}
    S["thr"] = F(4); S["k2"] = F(8); S["full"] = I(0); S["gain_on"] = block[0xc] != 0
    # FUN_100100900
    u1, u2, u3 = U(0xb4), U(0xb8), U(0xbc)
    if iso < u1:
        S[0x84] = f32(f32(I(0x70)) * f32(64)); S[0xc4] = F(0xa8)
    elif iso < u2:
        t = f32(f32(iso - u1) / f32(u2 - u1)); r = f32(1) - t
        S[0x84] = f32(f32(t * f32(I(0x74)) + f32(f32(I(0x70)) * r)) * f32(64))
        S[0xc4] = f32(F(0xac) * t + r * F(0xa8))
    elif iso < u3:
        t = f32(f32(iso - u2) / f32(u3 - u2)); r = f32(1) - t
        S[0x84] = f32(f32(t * f32(I(0x78)) + f32(f32(I(0x74)) * r)) * f32(64))
        S[0xc4] = f32(F(0xb0) * t + r * F(0xac))
    else:
        S[0x84] = f32(f32(I(0x78)) * f32(64)); S[0xc4] = F(0xb0)
    for dst, src in ((0x44, 0x40), (0x64, 0x58)):
        a, b_, c0, c1, sa, sb = I(src), I(src + 4), I(src + 8), I(src + 0xc), F(src + 0x10), F(src + 0x14)
        S[dst] = f32(f32(a) * f32(64)); S[dst + 8] = f32(f32(b_) * f32(64))
        S[dst + 0x10] = f32(c0); S[dst + 0x14] = f32(c1); S[dst + 0x18] = sa; S[dst + 0x1c] = sb
        dlt = f32(f32(c1) - f32(c0))
        S[dst + 4] = f32(f32(dlt / sa) + S[dst]); S[dst + 0xc] = f32(f32(dlt / sb) + S[dst + 8])
    S[0x8c] = f32(f32(I(0x7c)) * f32(64)); S[0x94] = f32(I(0x80)); S[0x98] = f32(I(0x84))
    S[0x9c] = F(0x88); S[0xa0] = F(0x8c)
    dlt = f32(S[0x98] - S[0x94])
    S[0x88] = f32(S[0x84] + f32(dlt / S[0x9c])); S[0x90] = f32(f32(dlt / S[0xa0]) + S[0x8c])
    a, b_, c0, c1, sa, sb = I(0x90), I(0x94), I(0x98), I(0x9c), F(0xa0), F(0xa4)
    S[0xa4] = f32(f32(a) * f32(64)); S[0xac] = f32(f32(b_) * f32(64)); S[0xb4] = f32(c0); S[0xb8] = f32(c1)
    S[0xbc] = sa; S[0xc0] = sb
    dlt = f32(f32(c1) - f32(c0))
    S[0xa8] = f32(f32(dlt / sa) + S[0xa4]); S[0xb0] = f32(f32(dlt / sb) + S[0xac])
    # pass-1 strength (0x100100b60, by ISO from +0xc0/+0xc4/+0xc8 and +0xcc/+0xd0/+0xd4)
    i0, i1, i2 = I(0xc0), I(0xc4), I(0xc8)
    if iso < i0:
        s = F(0xcc)
    elif iso < i1:
        s = f32(f32(f32(f32(F(0xd0) - F(0xcc)) * f32(iso - i0)) / f32(i1 - i0)) + F(0xcc))
    elif iso < i2:
        s = f32(f32(f32(f32(F(0xd4) - F(0xd0)) * f32(iso - i1)) / f32(i2 - i1)) + F(0xd0))
    else:
        s = F(0xd4)
    S["s"] = s
    return S


def spica_gain(d, rng, mid, S):
    """FUN_1001006d4 -> local_a4 (vectorised; operations in the original order)."""
    d = np.asarray(d, f32); rng = np.asarray(rng, f32); mid = np.asarray(mid, f32)
    if not S["gain_on"]:
        return np.full(d.shape, f32(512), f32)

    def side(p, b):          # p >= 0 path with base offset b (0x44 positive, 0x64 negative)
        A, A2, B, B2, Y0, Y1 = S[b], S[b + 4], S[b + 8], S[b + 0xc], S[b + 0x10], S[b + 0x14]
        r = np.full(p.shape, Y0, f32)
        m1 = (p >= A) & (p < A2)
        num = (((Y0 - Y1) * (A2 - p).astype(f32)).astype(f32))
        r1 = (num / f32(A2 - A)).astype(f32) + Y1
        r = np.where(m1, r1.astype(f32), r)
        m2 = p >= A2
        r2 = np.where(p >= B, np.where(p < B2, (((Y1 - Y0) * (B2 - p).astype(f32)).astype(f32) /
                                                 f32(B2 - B)).astype(f32) + Y0, Y0), Y1)
        return np.where(m2, r2.astype(f32), r).astype(f32)

    pos = d > 0
    g1 = np.where(pos, side(d, 0x44), side((-d).astype(f32), 0x64)).astype(f32)

    def curve(p, A, A2, B, B2, Y0, Y1):
        r = np.full(p.shape, Y0, f32)
        lo = p >= A
        mid_ = lo & (p < A2)
        r = np.where(mid_, ((((Y0 - Y1) * (A2 - p).astype(f32)).astype(f32) / f32(A2 - A)).astype(f32)
                            + Y1).astype(f32), r)
        hi = lo & (p >= A2)
        r3 = np.where(p >= B, np.where(p < B2, ((((Y1 - Y0) * (B2 - p).astype(f32)).astype(f32) /
                                                 f32(B2 - B)).astype(f32) + Y0).astype(f32), Y0), Y1)
        return np.where(hi, r3.astype(f32), r).astype(f32)

    g2 = curve(rng, S[0x84], S[0x88], S[0x8c], S[0x90], S[0x94], S[0x98])
    g3 = curve(mid, S[0xa4], S[0xa8], S[0xac], S[0xb0], S[0xb4], S[0xb8])
    m = np.minimum(np.minimum(g1.astype(np.int64), g2.astype(np.int64)), g3.astype(np.int64))
    return (f32(S[0xc4] * f32(0.00048828125)) * m.astype(f32)).astype(f32)


CROSS = ((0, -2), (0, -1), (-2, 0), (-1, 0), (0, 0), (1, 0), (2, 0), (0, 1), (0, 2))
TAPS = ((0, -3), (-1, -2), (0, -2), (1, -2), (-2, -1), (-1, -1), (0, -1), (1, -1), (2, -1),
        (-3, 0), (-2, 0), (-1, 0), (0, 0), (1, 0), (2, 0), (3, 0),
        (-2, 1), (-1, 1), (0, 1), (1, 1), (2, 1), (-1, 2), (0, 2), (1, 2), (0, 3))


def spica(Y, region, T, S, dev208=0, dev2c4=50, strip=512):
    """Y: uint16 plane; region (x0, y0, x1, y1): pixels [x0+3, x1-3) x [y0+3, y1-3) are processed.

    Processed in horizontal strips; the 3-px support is covered by the strip halo, so
    the result is the same as processing the whole plane.  The pattern tables give
    dx, dy = +/-1 only, so the 25 taps are read through plain slices of a float32
    copy (four quadrant variants) instead of per-pixel fancy indexing.
    """
    x0, y0, x1, y1 = region
    out = Y.copy()
    Wout = x1 - x0 - 6
    Hout = y1 - y0 - 6
    if Wout <= 0 or Hout <= 0:
        return out
    patterns = T["patterns"]
    coefs = T["coefs"]
    thr = np.uint64(int(S["thr"]))
    full = S["full"]
    so = S["s"]
    k = f32(f32(f32(S["k2"] * f32(0.0002)) * f32(dev208 + 100)) * f32(100 - dev2c4))
    step = max(1, int(strip))
    for r0 in range(0, Hout, step):
        r1 = min(r0 + step, Hout)
        hh, ww = r1 - r0, Wout
        A = Y[y0 + r0:y0 + r0 + hh + 6, x0:x1]
        Ai = A.astype(np.int32)
        Af = A.astype(np.float32)
        # 9-pixel cross census, local output origin (3, 3)
        v = [Ai[3 + dy:3 + dy + hh, 3 + dx:3 + dx + ww] for dx, dy in CROSS]
        mn = v[0].copy(); mx = v[0].copy()
        for a in v[1:]:
            mn = np.minimum(mn, a); mx = np.maximum(mx, a)
        rng = mx - mn
        half = rng // 2
        bits = sum(((half <= (v[i] - mn)).astype(np.int32) << i) for i in range(9))
        low = bits & 0xff
        idx = np.where(bits & 0x100, (~low) & 0xff, low)
        idx = np.where(rng.astype(np.uint64) < thr, 0, idx)
        ci = patterns[idx, 0]
        dxs = patterns[idx, 1]
        dys = patterns[idx, 2]
        q_pp = (dxs > 0) & (dys > 0)
        q_pm = (dxs > 0) & (dys < 0)
        q_mp = (dxs < 0) & (dys > 0)
        # 25-tap diamond; each tap sits on one of the four +/-1 quadrants
        acc = None
        for t, (a, b) in enumerate(TAPS):
            rp, rn = 3 + b, 3 - b
            cp, cn = 3 + a, 3 - a
            spp = Af[rp:rp + hh, cp:cp + ww]
            spm = Af[rp:rp + hh, cn:cn + ww]
            smp = Af[rn:rn + hh, cp:cp + ww]
            smm = Af[rn:rn + hh, cn:cn + ww]
            val = np.where(q_pp, spp, np.where(q_pm, smp, np.where(q_mp, spm, smm)))
            ct = coefs[ci, 1 + t]
            acc = (ct * val).astype(f32) if acc is None else fma32(ct, val, acc)
        Yc = Ai[3:3 + hh, 3:3 + ww]
        if full == 0:
            t_ = acc.astype(np.int64) >> 9
        else:
            d = ((acc - (Yc << 9).astype(f32)).astype(f32) * so).astype(f32)
            mid = ((mn + mx) // 2).astype(f32)          # (min+max) >= 0: truncating halving
            g = spica_gain((d * f32(0.0078125)).astype(f32), rng.astype(f32), mid, S)
            m = (f32(0.001953125) * (g * f32(0.001953125)).astype(f32)).astype(f32)
            t_ = (d * m).astype(f32).astype(np.int64) + Yc
        t_ = np.minimum(t_, 0x3fff)
        t_ = np.maximum(t_, 0)
        y = Yc.astype(f32)
        o = fma32(k, (t_.astype(np.int16).astype(f32) - y).astype(f32), y)
        o = np.maximum(np.minimum(o, f32(32767.0)), f32(0))
        out[y0 + 3 + r0:y0 + 3 + r1, x0 + 3:x1 - 3] = o.astype(np.int64).astype(np.uint16)
    return out
