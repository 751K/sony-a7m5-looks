"""ZcTaskSSCS (Imaging Edge Edit 4.0.00.10312): bit-exact port of the near-saturation colour suppression.

Process 0x1000f0e14. The builder (0x1000b8b28) inserts it unconditionally, right after GeoCorrection and
before LinearMatrix16, so it works on white-balanced camera RGB (0..16383 scale after ITP).

It is per pixel. A pixel is changed only when R > tR, G > tG and B > tB. Then
    kG  = min((G - tG) * 128 / (T - tG), 128)                      green's progress towards T
    kRB = 128 if c > T else (c - tc) * 128 / (T - tc), c = min(R, B) (R when R <= B, else B)
    f   = 128 - kG * kRB / 128                                      128 = unchanged, 0 = grey
and the colour differences R - G, B - G are scaled by f / 128 around the weighted luma
    L = (wR*R + wG*G + wB*B) / 128.
Weights wR/wG/wB are the per-CFA-site bytes look+0x1207..0x120a (tag 0x78f7) summed by colour
(look+0x84..0x90); thresholds T, tR, tG, tB are look+0xf82..0xf88 (tags 0x7882..0x7885).

Two code paths, chosen by the plane format (plane+0x1c): integer (u16 planes, the RAW path after
ITP/GeoCorr) and float32.
"""
import numpy as np

f32 = np.float32
DEFAULT_WEIGHTS = (32, 32, 32, 32)          # look+0x1207 when 0x78f7 is absent (Sony's parser, DSC03591)


def params_from_tags(tags, cfa=(0, 1, 1, 2)):
    """tags: {int tag: list of ints} (SR2SubIFD). Returns dict(T, tR, tG, tB, w=(wR, wG, wB))."""
    def one(t):
        return int(np.int16(np.uint16(tags[t][0] & 0xffff))) if t in tags else 0
    site = list(DEFAULT_WEIGHTS)
    if 0x78f7 in tags:
        for k, v in enumerate(tags[0x78f7][:4]):
            site[k] = v & 0xff
    w = [0, 0, 0]
    for k in range(4):
        c = cfa[k]
        w[c if c in (0, 1) else 2] += site[k]
    return dict(T=one(0x7882), tR=one(0x7883), tG=one(0x7884), tB=one(0x7885), w=tuple(w))


def params_from_arw(arw):
    """params_from_tags on the SR2SubIFD of an ARW (read only)."""
    import sr2
    tags = {k: (v[1] if isinstance(v[1], list) else [v[1]]) for k, v in sr2.sr2_subifd(str(arw)).items()}
    return params_from_tags(tags)


def sscs_int(r, g, b, p):
    """Integer path (0x1000f1238..0x1000f1358). r, g, b: uint16 arrays. Returns uint16 arrays."""
    T, tR, tG, tB = p["T"], p["tR"], p["tG"], p["tB"]
    wR, wG, wB = p["w"]
    R = np.asarray(r, np.int64); G = np.asarray(g, np.int64); B = np.asarray(b, np.int64)
    m = (R - tR > 0) & (G > tG) & (B > tB)

    def sdiv(a, d):                                      # AArch64 sdiv: truncate towards zero
        q = np.abs(a) // abs(d)
        return np.where((a < 0) != (d < 0), -q, q)

    kg = sdiv((G - tG) << 7, T - tG)
    kg = np.where(kg < 128, kg, 128)
    r_le_b = R <= B                                      # unsigned compare of u16 values
    c = np.where(r_le_b, R, B)
    tc = np.where(r_le_b, tR, tB)
    krb = np.where(c > T, 128, sdiv((c - tc) << 7, T - tc))
    f = 128 - ((krb * kg) >> 7)
    L = wR * R + wG * G + wB * B
    dR = f * (R - G)
    dB = f * (B - G)
    g2 = ((L << 7) - dR * wR - dB * wB) >> 14
    r2 = ((g2 << 7) + dR) >> 7
    b2 = ((g2 << 7) + dB) >> 7
    out = [np.where(m, v, o).astype(np.int64) & 0xffff for v, o in ((r2, R), (g2, G), (b2, B))]
    return tuple(o.astype(np.uint16) for o in out)


def _fma(a, b, c):
    return f32(np.float64(a) * np.float64(b) + np.float64(c))


def sscs_float(r, g, b, p):
    """Float path (0x1000f10dc..0x1000f1224). r, g, b: float32 arrays. Returns float32 arrays.
    fma is evaluated in double and rounded once; float32 products are exact in double, so this only
    differs from a true fused op in rare double-rounding cases."""
    T, tR, tG, tB = (f32(p[k]) for k in ("T", "tR", "tG", "tB"))
    wR, wG, wB = (f32(v) for v in p["w"])
    R = np.asarray(r, f32); G = np.asarray(g, f32); B = np.asarray(b, f32)
    one, h, inv = f32(1.0), f32(128.0), f32(0.0078125)
    s16 = f32(one / f32(T - tG)); s17 = f32(one / f32(T - tR)); s20 = f32(one / f32(T - tB))
    m = (R > tR) & (G > tG) & (B > tB)
    kg = f32(f32(f32(G - tG) * h) * s16)
    kg2 = f32(kg * inv)
    sr = np.where(R > T, h, f32(f32(f32(R - tR) * h) * s17)).astype(f32)
    sb = np.where(B > T, h, f32(f32(f32(B - tB) * h) * s20)).astype(f32)
    srb = np.where(R <= B, sr, sb).astype(f32)
    k = np.where(kg > h, one, kg2).astype(f32)
    f = f32(np.float64(h) - np.float64(k) * np.float64(srb))          # fmsub
    L = f32(R * wR)
    L = _fma(G, wG, L)
    L = _fma(B, wB, L)
    dR = f32(f * f32(R - G))
    dB = f32(f * f32(B - G))
    s21 = f32(dR * wR)
    s21 = _fma(dB, wB, s21)
    s24 = f32(np.float64(L) * np.float64(h) - np.float64(s21))       # fnmsub
    g2 = f32(s24 * f32(6.1035156e-05))
    r2 = f32(_fma(s24, inv, dR) * inv)
    b2 = f32(_fma(s24, inv, dB) * inv)
    return tuple(np.where(m, v, o).astype(f32) for v, o in ((r2, R), (g2, G), (b2, B)))
