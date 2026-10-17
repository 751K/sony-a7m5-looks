"""ZcTaskMarble (Imaging Edge Edit 4.0.00.10312): bit-exact port of the low-frequency YCC stage after YCC2RGB.

Process 0x100126f0c with FUN_1001293ac / 1001294f4 (to and from the gamma-2.2 work space, as in Effect),
FUN_10012672c (crop + 2:1 horizontal chroma), FUN_1001295b8 (6-pixel edge padding), FUN_1001297f0 and
FUN_10012a250 (3x3 binomial), FUN_100129af0 (chroma upsample + red protection), FUN_10012a464 (luma
upsample), FUN_10012a804 (luma detail gain, 10-bit), FUN_100126c58 (back to full width, region shrunk by 16).

The builder inserts it when (dev+0x218 == 1 and look+0x109c != 0) or dev+0x2c0 (setting 0x901c) != 0.
- Chroma path (only with NR on, dev+0x218 == 1): block means (4x4 or 8x8), 5x5 sigma filter on the
  block grid with luma-adaptive thresholds, 3x3 smoothing, bilinear upsample, then Cr is pulled back
  to the original where Cr is high and Cb near neutral.
- Luma path (dev+0x2c0 != 0): 8x8 block means, 5x5 sigma, 3x3, bilinear upsample, and the detail
  Y - up is added back with a tone-dependent gain; Y is requantised to 10 bits.
- Always: chroma leaves at half horizontal resolution (pairs averaged, then duplicated) and is
  blended with the original by `f` (1.0 unless the decoder's vtable+0xf0 flag is set).
"""
import struct

import numpy as np

import sony_effect as E
from sony_look import fma32

f32 = np.float32
RECIP25 = (32768, 32768, 16384, 10923, 8192, 6554, 5461, 4681, 4096, 3641, 3277, 2979, 2731, 2521, 2341, 2185,
           2048, 1928, 1820, 1725, 1638, 1560, 1489, 1425, 1365, 1311)                      # DAT_1002ed340
RECIP_U16 = (0, 32768, 16384, 10923, 8192, 6554, 5461, 4681, 4096, 3641, 3277, 2979, 2731, 2521, 2341, 2185, 2048,
             1928, 1820, 1725, 1638, 1560, 1489, 1425, 1365, 1311, 1260, 1214, 1170, 1130, 1092, 1057, 1024, 993,
             964, 936, 910, 886, 862, 840, 819, 799, 780, 762, 745, 728, 712, 697, 683, 669, 655, 643, 630, 618,
             607, 596, 585, 575, 565, 555, 546, 537, 529, 520)                                  # DAT_1002ed3a8
LUMA_STRENGTH = (0, 20, 44, 68, 92, 117, 142, 167, 192, 217, 242)                          # DAT_1002ed728
# upsample weights DAT_1002ed4a8..ed728, contiguous: the B=4 index k*2+j runs past each 8-entry table
UPW = np.array([84, 28, 60, 20, 36, 12, 12, 4, 28, 84, 20, 60, 12, 36, 4, 12, 4, 12, 12, 36, 20, 60, 28, 84,
                12, 4, 36, 12, 60, 20, 84, 28,
                105, 75, 45, 15, 91, 65, 39, 13, 77, 55, 33, 11, 63, 45, 27, 9, 49, 35, 21, 7, 35, 25, 15, 5, 21, 15,
                9, 3, 7, 5, 3, 1,
                15, 45, 75, 105, 13, 39, 65, 91, 11, 33, 55, 77, 9, 27, 45, 63, 7, 21, 35, 49, 5, 15, 25, 35, 3, 9,
                15, 21, 1, 3, 5, 7,
                1, 3, 5, 7, 3, 9, 15, 21, 5, 15, 25, 35, 7, 21, 35, 49, 9, 27, 45, 63, 11, 33, 55, 77, 13, 39, 65, 91,
                15, 45, 75, 105,
                7, 5, 3, 1, 21, 15, 9, 3, 35, 25, 15, 5, 49, 35, 21, 7, 63, 45, 27, 9, 77, 55, 33, 11, 91, 65, 39, 13,
                105, 75, 45, 15,
                0, 20, 44, 68, 92, 117, 142, 167, 192, 217, 242], np.int64)                     # (+ ed728 tail)
UPW_BASE = 0x1002ed4a8
# (t12, t11, t32, t10) for (c00, c01, c11, c10)
UP_TABLES = {4: (0x1002ed4a8, 0x1002ed4c8, 0x1002ed4e8, 0x1002ed508),
             8: (0x1002ed528, 0x1002ed5a8, 0x1002ed628, 0x1002ed6a8)}
# Edit's per-look Clarity default (DAT_1002e8214, 0x24-byte rows, last field), read by the decoder's
# vtable+0x178 and scaled x10 into dev+0x2c0 (setting 0x901c, slider 0..100). FL2 (0x16) and FL3 (0x17)
# have no row in 4.0.00.10312, so vtable+0x178 fails and every default, Clarity included, is 0.
CLARITY_DEFAULT = {2: 1, 4: 1, 7: 1, 8: 0, 0x10: 1, 0x11: 1, 0x12: 1, 0x13: 8, 0x14: 1, 0x15: 1, 0x18: 1}


def clarity_default(look_id):
    """dev+0x2c0 (setting 0x901c) after Reset in Edit for a look id (sony_render.LOOK_IDS)."""
    return 10 * CLARITY_DEFAULT.get(look_id, 0)



def look_from_tags(tags, fmt=0x3600):
    """Look struct bytes (from look+0x1000) for the Marble fields, built from SR2SubIFD tags as the
    parser does (0x1000a56e4..0x1000a5dac). tags: {int tag: list of ints}; fmt: ARW format code.

    fmt >= 0x3220: 0x109c <- 0x794a, 0x10a0..0x10c4 <- 0x794b..0x795d (else 0x109c = 0);
    fmt >= 0x3230: 0x109e <- 0x795e (chroma block size flag: 1 -> 8x8, else 4x4).
    if 0x7815 present: 0x113e = 1, 0x1140 <- 0x7815[0..9], 0x1154 <- 0x7816[0..9],
    0x1168/0x1169 <- 0x7817/0x7818 (bytes), 0x116a <- (0x7810 != 0),
    0x116c/0x116e/0x1170/0x1172 <- 0x7811..0x7814, 0x1174/0x1176 <- 0x7819/0x781a.
    """
    b = bytearray(0x200)
    put = lambda off, v: struct.pack_into("<H", b, off - 0x1000, int(v) & 0xffff)
    g = lambda t, i=0: (tags.get(t) or [0])[i]
    if fmt >= 0x3220:
        put(0x109c, g(0x794a))
        for i in range(19):
            put(0x10a0 + 2 * i, g(0x794b + i))
        if fmt >= 0x3230:
            put(0x109e, g(0x795e))
    if tags.get(0x7815) is not None:
        b[0x13e] = 1
        for i, v in enumerate(tags[0x7815][:10]):
            put(0x1140 + 2 * i, v)
        for i, v in enumerate((tags.get(0x7816) or [])[:10]):
            put(0x1154 + 2 * i, v)
        b[0x168], b[0x169] = g(0x7817) & 0xff, g(0x7818) & 0xff
        b[0x16a] = int(g(0x7810) != 0)
        for off, t in ((0x116c, 0x7811), (0x116e, 0x7812), (0x1170, 0x7813), (0x1172, 0x7814),
                       (0x1174, 0x7819), (0x1176, 0x781a)):
            put(off, g(t))
    return Look(bytes(b))


# look offsets that marble() reads
USED = [0x109c, 0x109e] + list(range(0x10a0, 0x10c6, 2)) + [0x113e] + list(range(0x1140, 0x1168, 2)) + \
       [0x1168, 0x1169, 0x116a] + list(range(0x116c, 0x1178, 2))


class Look:
    """Raw look struct bytes from look+0x1000 (e.g. dumped from the emulator) with typed getters."""

    def __init__(self, blob, base=0x1000):
        self.b, self.base = bytes(blob), base

    def s16(self, off):
        return struct.unpack_from("<h", self.b, off - self.base)[0]

    def u16(self, off):
        return struct.unpack_from("<H", self.b, off - self.base)[0]

    def u8(self, off):
        return self.b[off - self.base]


def _cdiv(a, b):
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _i32(v):
    return ((np.asarray(v, np.int64) + (1 << 31)) & 0xffffffff) - (1 << 31)


def _sat15(v):
    """(u32)v; if (int)v < 0 -> 0xffff else (v >> 15) & 0xffff."""
    v = np.asarray(v, np.int64) & 0xffffffff
    return np.where(v >= (1 << 31), 0xffff, (v >> 15) & 0xffff)


def strength(dev224, dec):
    if dec is None:
        return f32(0.5)
    if dec["d14c"] == 3:
        if dev224 > -51:
            return f32(1.0)
        return f32(f32((2 * dev224 + 200) & 0xffffffff) * f32(0.01))
    if dec["f0"] == 0:
        return f32(1.0)
    iso = dec["iso"]
    if iso >= 1600:
        f = f32(1.0)
    else:
        f = f32(f32(f32(f32(1.0) - f32(0.5)) * f32(f32(iso) - f32(100))) / f32(f32(1600) - f32(100)) + f32(0.5))
    if dev224 >= 20:
        s0 = f32(_cdiv(dev224, 20) + 5)
        s2 = -fma32(s0, f32(0.2), f32(-1.0))
        s0b = fma32(s0, f32(0.2), f32(-2.0))
        f = f32(-fma32(f, s0b, s2))
    return f32(f)


def _strength_470(scale, level, tab, maxlvl):
    """FUN_100129470: 10-step table interpolation used when the decoder's
    +0x22780 / +0x22784 floats are not 1.0.

    `tab(i)` reads the raw u16 entry (the assembly reads past index 9 into the
    next table, so this is a raw read, not the extrapolation used by the 1.0
    path).  `maxlvl` is ZcARW+0x1b0 (decoder d1b0).
    """
    scale = f32(scale)
    level = int(level)
    if maxlvl < level:
        k = maxlvl // 10
        f1 = f32(tab(9))                     # ldr h1, [x2, #0x12]
        u4, u3 = 10, maxlvl
    else:
        k = level // 10
        u4, u3 = k + 1, level
        f1 = f32(f32(tab(u4)) * scale)       # fmul s1, s1, s0
    f2 = f32(tab(k))
    frac = f32(level - (u3 // 10) * 10)      # madd with -10
    den = f32(f32(u4 - u3 // 10) * f32(10.0))
    s = fma32(f32(-f2), scale, f1)           # fmsub s1, s2, s0, s1 = f1 - f2*scale
    s = f32(s * frac)                        # fmul  s1, s1, s3
    s = f32(s / den)                         # fdiv  s1, s1, s4
    return f32(fma32(f2, scale, s))          # fmadd s0, s2, s0, s1


def to_ycc(r, g, b):
    R, G, B = (np.asarray(p, np.int64) for p in (r, g, b))
    y = np.minimum((R * 0xb44 + G * 0xdc3 + B * 0x271) >> 11, 0xefff) + 0x1000
    cb = np.clip(_i32(R * -0x6ab + G * -0x964 + B * 0x1011) >> 11, -0x8000, 0x7fff) ^ 0x8000
    cr = np.clip(_i32(R * 0x1acc + G * -0x1ac0 + B * -9) >> 11, -0x8000, 0x7fff) ^ 0x8000
    return y, cb & 0xffff, cr & 0xffff


def from_ycc(y, cb, cr):
    Y, U, V = (np.asarray(p, np.int64) for p in (y, cb, cr))
    Y, U, V = Y - 0x1000, U - 0x8000, V - 0x8000
    out = []
    for c in ((0x2546, 0x1526, -0x59e), (0x254b, -0x1118, -0x5b3), (0x2542, -0x136, 0x3a10)):
        v = _i32(Y * c[0] + V * c[1] + U * c[2])
        out.append(np.clip(v >> 15, 0, 0x3fff))
    return out


def _pad6(a, nw, nh):
    ri = np.clip(np.arange(nh + 12) - 6, 0, nh - 1)
    ci = np.clip(np.arange(nw + 12) - 6, 0, nw - 1)
    return a[np.ix_(ri, ci)]


def _binom3(p, nw, nh, a):
    """FUN_1001297f0 / FUN_10012a250 on a padded plane: rows/cols 6..n+5."""
    out = np.zeros_like(p)
    r, c = slice(6, nh + 6), slice(6, nw + 6)
    s = lambda dy, dx: p[6 + dy:nh + 6 + dy, 6 + dx:nw + 6 + dx]
    S = (s(-1, -1) + 2 * s(-1, 0) + s(-1, 1) + 2 * s(0, -1) + 4 * s(0, 0) + 2 * s(0, 1) + s(1, -1) + 2 * s(1, 0) + s(1, 1))
    out[r, c] = (((S * (0x100 - a) + a * 0x10 * s(0, 0) + 0x800) & 0xffffffff) >> 12) & 0xffff
    return out


def marble(r, g, b, look, dev, dec, region=None, mode=0):
    """r, g, b: uint16 planes (YCC2RGB output, sRGB-encoded for mode 0, AdobeRGB/gamma-2.2 for
    mode 1; both from render+0x44). look: Look. dev: dict(d218, d224, d2c0).
    dec: dict(iso, f0, d14c, f22780, f22784, d1b0) or None. Returns (r, g, b) and the valid region (shrunk by 16).
    Pixels outside the valid region are not meaningful (Sony leaves them unwritten)."""
    H, W = np.shape(r)
    x0, y0, x1, y1 = region if region is not None else (0, 0, W, H)
    bnr = dev["d218"] == 1 and look.s16(0x109c) != 0
    lum = dev["d2c0"]
    if not bnr and lum == 0:
        return (r, g, b), (x0, y0, x1, y1)
    T = E.tables()
    sl = (slice(y0, y1), slice(x0, x1))
    planes = [np.array(p, np.int64) for p in (r, g, b)]
    if mode == 0:
        conv = E._convert([p[sl] for p in planes], T["srgb_dec"], E.M_FWD[0], T["gamma_enc"])
    else:
        conv = E._convert([p[sl] for p in planes], T["gamma_dec"], E.M_FWD[1], T["gamma_enc"])
    for p, c in zip(planes, conv):
        p[sl] = c
    Y, Cb, Cr = to_ycc(*planes)
    origCr, origCb = Cr.copy(), Cb.copy()
    f = strength(dev["d224"], dec)

    rw, rh = x1 - x0, y1 - y0
    w4 = rw if rw % 4 == 0 else rw - rw % 4 + 4
    h4 = rh if rh % 4 == 0 else rh - rh % 4 + 4
    if w4 < 10:
        raise ValueError("region narrower than 10 px: Sony returns the YCC image unchanged (not modelled)")

    # FUN_10012672c: crop, chroma pairs averaged, pad right with column x1, bottom with the last row
    Yc = np.zeros((h4, w4), np.int64)
    Cbh = np.zeros((h4, w4 // 2), np.int64)
    Crh = np.zeros((h4, w4 // 2), np.int64)
    n2 = rw // 2
    if rw > 1:
        Yc[:rh, :2 * n2] = Y[y0:y1, x0:x0 + 2 * n2]
        Cbh[:rh, :n2] = (Cb[y0:y1, x0:x0 + 2 * n2:2] + Cb[y0:y1, x0 + 1:x0 + 2 * n2:2]) >> 1
        Crh[:rh, :n2] = (Cr[y0:y1, x0:x0 + 2 * n2:2] + Cr[y0:y1, x0 + 1:x0 + 2 * n2:2]) >> 1
    if n2 < w4 // 2:
        Yc[:rh, 2 * n2:] = Y[y0:y1, x1:x1 + 1]
        Cbh[:rh, n2:] = Cb[y0:y1, x1:x1 + 1]
        Crh[:rh, n2:] = Cr[y0:y1, x1:x1 + 1]
    if w4 > 1 and rh < h4:
        Yc[rh:, 0::2] = Yc[rh - 1, 0::2]
        Yc[rh:, 1::2] = Yc[rh - 1, 0::2]
        Cbh[rh:] = Cbh[rh - 1]
        Crh[rh:] = Crh[rh - 1]

    Y35 = Yc.copy()
    Cr38 = Crh.copy()
    Cr36 = Crh.copy() if not bnr else np.zeros_like(Crh)
    Cb37 = Cbh.copy() if not bnr else np.zeros_like(Cbh)
    Y39 = Yc.copy() if lum else None

    # parameters (Process 0x100127xxx)
    d218, d224 = dev["d218"], dev["d224"]
    k67 = _cdiv(d224, 20) + 5 if d218 != 0 else 0
    t = (d224 * -0x66666667) >> 32
    t = ((t + (1 << 31)) & 0xffffffff) - (1 << 31)
    k24 = ((t >> 3) - (t >> 31)) if d218 != 0 else 5
    flag = dec["f0"] if (dec is not None and k67 > 5) else 0
    if flag == 1:
        k = k67 * 8 - 40
        b4 = _cdiv(k * look.s16(0x10ae), 5) + look.s16(0x10ae)
        b0 = _cdiv(k * look.s16(0x10b0), 5) + look.s16(0x10b0)
    else:
        b4 = k67 * _cdiv(look.s16(0x10ae), 5)
        b0 = k67 * _cdiv(look.s16(0x10b0), 5)
    P = dict(d0=look.s16(0x10a0) & 0xffffffff, cc=look.s16(0x10a2), c8=look.s16(0x10a4), c4=look.s16(0x10a6),
             c0=look.s16(0x10a8), bc=look.s16(0x10aa), b8=look.s16(0x10ac), b4=b4, b0=b0,
             ac=_cdiv(k24 * look.s16(0x10b2), 5) + look.s16(0x10b2),
             a8=_cdiv(k24 * look.s16(0x10b4), 5) + look.s16(0x10b4),
             l98=look.s16(0x10bc) & 0x7f, l94=look.s16(0x10be) & 0x7f, l90=look.s16(0x10c0) & 0x7f,
             l8c=look.s16(0x10c2) & 0x7f, l88=look.s16(0x10c4))
    q, r10 = lum // 10, lum % 10
    if look.u8(0x113e) == 0:
        gate, lo_l, hi_l, g34, g38, g3c, g40, thrY, a_y = 0, 0, 0, 0, 0, 0, 0, 0, 0
        i24 = q + 1 if lum < 100 else 10
        base = LUMA_STRENGTH[q]
        v67 = int(E._fma64((LUMA_STRENGTH[i24] - base) * 0.1, float(r10), float(base)))
        iso = dec["iso"] if dec is not None else 0
        if iso < 0xc81:
            isof = 100
        elif iso < 0x64000:
            w10 = int(E._fma64(float(iso - 0xc800), -4.7433035714285714e-05, 67.0))
            w11 = int(E._fma64(float(iso - 0x3200), -0.00044270833333333337, 84.0))
            w9 = w10 if iso > 0xc800 else w11
            w10b = int(E._fma64(float(iso - 0xc80), -0.0016666666666666668, 100.0))
            isof = w9 if iso > 0x3200 else w10b
        else:
            isof = 0x32
        g128 = g124 = int(float(v67) * (0.01 * float(isof)))
    else:
        gate = look.u8(0x116a)
        lo_l, hi_l = look.u8(0x1168), look.u8(0x1169)          # local_120: +0x28 / +0x2c
        g34, g38 = look.u16(0x1172), look.u16(0x116e)          # local_114: +0x34 / +0x38
        g3c, g40 = look.u16(0x1170), look.u16(0x116c)          # local_10c: +0x3c / +0x40
        thrY, a_y = look.u16(0x1174), look.u16(0x1176)         # local_130: +0x18 / +0x1c
        i56 = q + 1 if lum < 100 else 10

        def tab(base_off, i):
            if i < 10:
                return look.u16(base_off + 2 * i)
            return (look.u16(base_off + 0x12) * 2 - look.u16(base_off + 0x10)) & 0xffffffff

        maxlvl = dec["d1b0"] if dec is not None else 0
        raw = lambda base, i: look.u16(base + 2 * i)      # no extrapolation here
        if dec is not None and dec["f22780"] != 1.0:
            g128 = int(_strength_470(dec["f22780"], lum, lambda i: raw(0x1140, i), maxlvl))
        else:
            lo, hi = tab(0x1140, q), tab(0x1140, i56)
            g128 = int(float(E._fma64(float(r10) * 0.1, float(_i32(hi - lo)), float(lo))))
        if dec is not None and dec["f22784"] != 1.0:
            g124 = int(_strength_470(dec["f22784"], lum, lambda i: raw(0x1154, i), maxlvl))
        else:
            lo, hi = tab(0x1154, q), tab(0x1154, i56)
            g124 = int(float(E._fma64(float(_i32(hi - lo)) * 0.1, float(r10), float(lo))))

    B = 8 if look.s16(0x109e) == 1 else 4
    ok = True
    if bnr:
        ok = _chroma_path(Yc, Cbh, Crh, Cr38, Cb37, Cr36, B, w4, h4, P)
    if lum and ok:
        _luma_path(Y35, Y39, w4, h4, thrY, a_y, dict(g20=g128, g24=g124, lo=lo_l, hi=hi_l, gate=gate, g34=g34,
                                                       g38=g38, g3c=g3c, g40=g40))

    # FUN_100126c58: back to full width inside the region
    Yo = np.zeros((H, W), np.int64)
    Cbo = np.zeros((H, W), np.int64)
    Cro = np.zeros((H, W), np.int64)
    m = (rw + 1) // 2 if rw + 1 >= 0 else -((-(rw + 1)) // 2)
    if rw + 1 > 1:
        Yo[y0:y1, x0:x0 + 2 * m] = Y35[:rh, :2 * m]
        for dx in (0, 1):
            Cbo[y0:y1, x0 + dx:x0 + 2 * m:2] = Cb37[:rh, :m]
            Cro[y0:y1, x0 + dx:x0 + 2 * m:2] = Cr36[:rh, :m]
    vr = (x0 + 16, y0 + 16, x1 - 16, y1 - 16)

    def blend(new, orig):
        o = orig.astype(np.float32)
        v = fma32((new.astype(np.float32) - o).astype(np.float32), f, o)
        v = np.fmax(np.fmin(v, f32(65535.0)), f32(0.0))
        return np.trunc(v).astype(np.int64) & 0xffff

    Cro = blend(Cro, origCr)
    Cbo = blend(Cbo, origCb)
    R, G, Bp = from_ycc(Yo, Cbo, Cro)
    vs = (slice(vr[1], vr[3]), slice(vr[0], vr[2]))
    if mode == 0:
        back = E._convert([p[vs] for p in (R, G, Bp)], T["gamma_dec"], E.M_INV[0], T["srgb_enc"])
    else:
        back = E._convert([p[vs] for p in (R, G, Bp)], T["gamma_dec"], E.M_INV[1], T["gamma_enc"])
    outs = [R.copy(), G.copy(), Bp.copy()]
    for o, c in zip(outs, back):
        o[vs] = c
    return tuple(o.astype(np.uint16) for o in outs), vr


def _chroma_path(Yc, Cbh, Crh, Cr38, Cb37, Cr36, B, w4, h4, P):
    nbw, nbh = -(-w4 // B), -(-h4 // B)
    shY = 6 if B == 8 else 4
    shC = 5 if B == 8 else 3
    # block means
    sY = _rect_sum(Yc, B, B, h4, w4, nbh, nbw)
    sCb = _rect_sum(Cbh, B, B // 2, h4, w4 // 2, nbh, nbw)
    sCr = _rect_sum(Crh, B, B // 2, h4, w4 // 2, nbh, nbw)
    mY = np.maximum((sY + (1 << (shY - 1))) >> shY, 1)
    mCb = np.maximum((sCb + (1 << (shC - 1))) >> shC, 1)
    mCr = np.maximum((sCr + (1 << (shC - 1))) >> shC, 1)
    pY, pCb, pCr = (_pad6(a, nbw, nbh) for a in (mY, mCb, mCr))
    # 5x5 sigma (rows/cols 4..n+7)
    R0, R1, C0, C1 = 4, nbh + 8, 4, nbw + 8
    sh = lambda a, dy, dx: a[R0 + dy:R1 + dy, C0 + dx:C1 + dx]
    yc, ub, vc = sh(pY, 0, 0), sh(pCb, 0, 0), sh(pCr, 0, 0)
    t79 = np.clip(P["c8"] + (_i32(P["cc"] * yc + 0x80) >> 8), 0, 0xffff)
    t59 = np.clip(P["c0"] + (_i32((ub - 0x8000) * P["c4"] + 0x80) >> 8), 0, 0xffff)
    t58 = np.clip(P["b8"] + (_i32((vc - 0x8000) * P["bc"] + 0x80) >> 8), 0, 0xffff)

    def thr(x, k):
        p = (x * t79) & 0xffffffff
        u = np.where(p < 0x80, 1, (p + 0x80) >> 8)
        u = np.where(p < 0xffff80, u, 0xffff)
        v = ((u * k + 0x80) & 0xffffffff) >> 8
        v = np.where(v < 0x101, 0x100, v)
        return np.where(v > 0x7fff, 0x8000, v)

    tb, tr = thr(t59, P["b4"]), thr(t58, P["b0"])
    sb = np.zeros_like(yc); sr = np.zeros_like(yc); n = np.zeros_like(yc)
    for dy in (-4, -2, 0, 2, 4):
        for dx in (-4, -2, 0, 2, 4):
            yn, bn, rn = sh(pY, dy, dx), sh(pCb, dy, dx), sh(pCr, dy, dx)
            okk = ((np.abs(ub - bn) <= tb) & (np.abs(vc - rn) <= tr) & (np.abs(yc - yn) <= P["d0"])
                   & (yn != 0) & (bn != 0) & (rn != 0))
            sb += np.where(okk, bn, 0); sr += np.where(okk, rn, 0); n += okk
    idx = np.where((n - 1) & 0xffffffff > 24, 0, n)
    rc = np.array(RECIP25, np.int64)[idx]
    qb = np.zeros_like(pY); qr = np.zeros_like(pY)
    qb[R0:R1, C0:C1] = _sat15(rc * sb + 0x4000)
    qr[R0:R1, C0:C1] = _sat15(rc * sr + 0x4000)
    # 3x3
    qb = _binom3(qb, nbw, nbh, P["ac"])
    qr = _binom3(qr, nbw, nbh, P["a8"])
    # FUN_100129af0: upsample to half width (offset B/2 rows, B/4 cols, later blocks overwrite) + merge
    half = w4 // 2
    upB = np.zeros((h4, half + 12), np.int64)
    upR = np.zeros((h4, half + 12), np.int64)
    t12, t11, t32, t10 = ((a - UPW_BASE) // 4 for a in UP_TABLES[B])
    hb, qb4 = B // 2, B // 4
    ncol = qb4 + hb
    # column c gets the value of the last block bx that writes it (bx ascending)
    cols = np.arange(half + 12)
    colbx = np.full(cols.size, -1); colj = np.zeros(cols.size, np.int64)
    for bx in range(nbw):
        jmax = min(ncol, half - bx * hb)
        if jmax > 0:
            c0 = qb4 + bx * hb
            colbx[c0:c0 + jmax] = bx
            colj[c0:c0 + jmax] = np.arange(jmax)
    rows = np.arange(h4)
    rowby = (rows - hb) // B; rowk = (rows - hb) % B
    rv = (rows >= hb) & (rowby < nbh)
    cv = colbx >= 0
    R_, C_ = np.ix_(rows[rv], cols[cv])
    by_ = rowby[rv][:, None]; bx_ = colbx[cv][None, :]
    ii = rowk[rv][:, None] * hb + colj[cv][None, :]
    for qd, up in ((qr, upR), (qb, upB)):
        c00, c01 = qd[by_ + 6, bx_ + 6], qd[by_ + 6, bx_ + 7]
        c11, c10 = qd[by_ + 7, bx_ + 7], qd[by_ + 7, bx_ + 6]
        v = UPW[t12 + ii] * c00 + UPW[t11 + ii] * c01 + UPW[t32 + ii] * c11 + UPW[t10 + ii] * c10
        up[R_, C_] = _up_sat(v)
    # merge
    T7c = _recip_signed(P["l94"] - P["l98"])
    T80 = _recip_signed(P["l8c"] - P["l90"])
    cb = upB[:, :half]; crv = upR[:, :half]
    a = np.where(cb < 0x8000, 0x8000 - cb, cb - 0x8000)
    u5 = _i32((a - P["l98"] * 0x100) * T7c)
    u5 = np.maximum(u5, 0); u5 = np.where(u5 > 0x7fffff, 0x800000, u5)
    u29 = _i32((crv - P["l90"] * 0x100 - 0x8000) * T80)
    u29 = np.maximum(u29, 0)
    u29 = np.where(u29 >= 0x800000 - u5, 0x800000 - u5, u29)
    u29 = np.where(u29 > 0x7fffff, 0x800000, u29)
    w = ((u29 + 0x8000) >> 16) * P["l88"]
    Cr36[:] = _sat15((0x8000 - w) * crv + w * Cr38 + 0x4000)
    Cb37[:] = cb
    return True


def _rect_sum(a, bh, bw, h, w, nh, nw):
    p = np.zeros((nh * bh, nw * bw), np.int64)
    hh, ww = min(h, nh * bh), min(w, nw * bw)
    p[:hh, :ww] = a[:hh, :ww]
    return p.reshape(nh, bh, nw, bw).sum(axis=(1, 3))


def _up_sat(v):
    v = np.asarray(v, np.int64)
    o = np.where(v + 0x40 < 0, 0, ((v + 0x40) >> 7) & 0xffff)
    return np.where(v > 0x7fffbf, 0xffff, o)


def _recip_signed(d):
    return -RECIP_U16[-d] if d < 0 else RECIP_U16[d]


def _luma_path(Y35, Y39, w4, h4, thrY, a_y, G):
    n8w, n8h = (w4 + 7) >> 3, (h4 + 7) >> 3
    s = _rect_sum(Y35, 8, 8, h4, w4, n8h, n8w)
    m = np.maximum((s + 0x20) >> 6, 1)
    p = _pad6(m, n8w, n8h)
    R0, R1, C0, C1 = 4, n8h + 8, 4, n8w + 8
    sh = lambda dy, dx: p[R0 + dy:R1 + dy, C0 + dx:C1 + dx]
    c = sh(0, 0)
    acc = np.zeros_like(c); n = np.zeros_like(c)
    for dy in (-4, -2, 0, 2, 4):
        for dx in (-4, -2, 0, 2, 4):
            v = sh(dy, dx)
            okk = (np.abs(c - v) <= thrY) & (v != 0)
            acc += np.where(okk, v, 0); n += okk
    idx = np.where((n - 1) & 0xffffffff > 24, 0, n)
    q = np.zeros_like(p)
    q[R0:R1, C0:C1] = _sat15(np.array(RECIP25, np.int64)[idx] * acc + 0x4000)
    q = _binom3(q, n8w, n8h, a_y)
    # FUN_10012a464: bilinear x8 (double, exact) into Y35
    up = np.zeros((n8h * 8, n8w * 8), np.int64)
    d29, d30 = q[6:6 + n8h, 6:6 + n8w], q[6:6 + n8h, 7:7 + n8w]
    d27, d25 = q[7:7 + n8h, 6:6 + n8w], q[7:7 + n8h, 7:7 + n8w]
    for k in range(8):
        top, bot = 15 - 2 * k, 1 + 2 * k
        for j in range(8):
            L, Rw = 15 - 2 * j, 1 + 2 * j
            X = (d30 * Rw + d29 * L) * top + bot * (d25 * Rw + d27 * L)
            up[k::8, j::8] = ((X + 128) >> 8) & 0xffff      # (int)(X/256 + 0.5), X >= 0, exact
    Y35[:, :] = up[:h4, :w4]
    # FUN_10012a804
    Yo = Y39 >> 6
    d = Yo - (Y35 >> 6)
    gain = np.where(d >= 0, G["g20"], G["g24"])
    a = np.abs(d)
    a5 = np.minimum(a, G["g3c"])
    a4 = np.where(a >= G["g38"], a5 - G["g38"], 0)
    g2 = (((G["g34"] * 0x400 + a4 * G["g40"]) >> 9) + 1) >> 1
    g2 = np.where(g2 > 0x3ff, 0x400, g2)
    g5 = g2 if G["gate"] != 0 else np.full_like(d, 0x400)
    wlo = {1: Yo * 6, 2: Yo << 2, 3: Yo * 3}.get(G["hi"], np.full_like(Yo, 0x200))
    whi = {1: (Yo ^ 0x3ff) * 6, 2: (Yo << 2) ^ 0xffc, 3: (Yo ^ 0x3ff) * 3}.get(G["lo"], np.full_like(Yo, 0x200))
    w = np.minimum(wlo & 0xffffffff, whi & 0xffffffff)
    w = np.where(w > 0x1ff, 0x200, w)
    v = _i32((((g5 * gain) >> 9) + 1 >> 1) * d * w + Yo * 0x80000)
    r = np.where(v > 0, ((v >> 18) + 1) >> 1, -((((-v) >> 18) + 1) >> 1))
    r = np.clip(r, 0, 0x3ff)
    Y35[:, :] = r << 6
