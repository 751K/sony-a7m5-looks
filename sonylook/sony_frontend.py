#!/usr/bin/env python3
"""Sony front end for the reference renderer (RAW -> LinearMatrix16 input).

Recovered with REA from Imaging Edge Edit 4.0.00.10312 (arm64):

* ZcTaskDemosaic process 0x100138ac8, before interpolation, for each CFA sample:
      out = (int)(look[0x40] * 2^-19 * gain[cfa] * (raw - look[0x38 + cfa]))   clamp int16
  look+0x38 = SR2SubIFD 0x7310 BlackLevel (512 x4), look+0x40 = 0x7311 (256),
  evaluated in double, left to right, truncated toward zero.
* gain[] from FUN_10013c4b0: WB mode dev+0x1e8 (setting 0x8002). 1 = as shot
  (0x7313 WB_RGGBLevels), 2 = colour temperature (dev+0x1ec, setting "ColorTemperature",
  0x8013) interpolated over the five SR2 nodes 2500/3200/4500/6000/8500 K (0x782d/0x782c/
  0x7824/0x782b/0x782a), 3 = precomputed custom gains (decoder state +0xb0/+0xb4),
  4..0xc = the camera's preset light sources (0x7820..0x7829), 0xd = custom.
  The tint step FUN_10013c29c scales the node-segment deltas by dev+0x1f0 (0x8014);
  it is skipped for modes 2 and 3 (the assembly jumps past it).  Exposure is
  exp2(dev+0x1f8 / 300) and dev+0x1f4 (0x8015) is the R/B factor.
* For RGGB sensors (look+0x84..0x90 != 1,0,2,1) the builder inserts ZcTaskITP (0x1000f95d0)
  instead of ZcTaskDemosaic; its scaling FUN_1000f13bc uses the same factors in float32
  (itp_scale) and the interpolated planes are clamped to 0..16383 and truncated to u16.
* No camera matrix follows: with 0x780f present (look+0x1c2 != 0) the builder inserts
  LinearMatrix16 only, so the 0x780f table works directly on white-balanced camera
  RGB. The per-look 0x7800 matrix feeds the non-16 LinearMatrix (FUN_1000d4cfc ->
  render+0x18a4) and is unused here.

The interpolation itself is ported in sony_itp.py. For box-downsampled comparisons the CFA
samples can instead be averaged per channel inside each s x s block (block_average), which
equals box(demosaic(x)) up to edge effects of the interpolator.
"""
import math

import numpy as np

from sony_look import fma32

f32 = np.float32
SCALE_2_19 = 1.9073486328125e-06


def _f32bits(bits):
    return np.array([bits], np.uint32).view(np.float32)[0]


WB_0_1 = _f32bits(0x3DCCCCCD)
WB_0_2 = _f32bits(0x3E4CCCCD)
WB_0_3 = _f32bits(0x3E99999A)
WB_0_6 = _f32bits(0x3F19999A)
WB_W_SCALE = _f32bits(0x3B5A740E)       # 0.0033333334
WB_W_LIN_C = _f32bits(0x3FE500D6)       # 1.789088
WB_W_LIN_S = _f32bits(0xB8EC67C6)       # -0.00011272686


def _short(v):
    v = int(v) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _interp_wb(kelvin, table):
    """FUN_10013bda0: WB levels at `kelvin` from the five camera nodes.

    table = [(2500, R, G, B), (3200, ...), (4500, ...), (6000, ...), (8500, ...)]
    (SR2SubIFD 0x782d, 0x782c, 0x7824, 0x782b, 0x782a).  Double arithmetic,
    truncation and short wrap as in the assembly.
    """
    k = float(kelvin)
    if k < 3200.0:
        lo, hi = table[0], table[1]
        d = k * 0.0014285714285714286 + -3.5714285714285716
    elif k < 4500.0:
        lo, hi = table[1], table[2]
        d = k * 0.0007692307692307692 + -2.4615384615384617
    elif k < 6000.0:
        lo, hi = table[2], table[3]
        d = k * 0.0006666666666666666 + -3.0
    elif k < 8500.0:
        lo, hi = table[3], table[4]
        d = k * 0.0004 + -2.4
    else:                                            # extrapolate past the last node
        lo, hi = table[4], table[3]
        d = k * 0.0004 + -3.4
        return tuple(_short(int(float(a) + float(a - b) * d)) for a, b in zip(lo[1:], hi[1:]))
    return tuple(_short(int(float(a) + float(b - a) * d)) for a, b in zip(lo[1:], hi[1:]))


def _cct_from_red(r, table):
    """FUN_10013c0bc: inverse interpolation of the red level -> CCT."""
    R = [t[1] for t in table]                        # node red levels
    if r < R[0]:
        den = R[1] - R[0]
        return 2500 - int((float(R[0] - r) * 700.0) / den) if den else 2500
    if r < R[1]:
        den = R[1] - R[0]
        return int((float(r - R[0]) * 700.0) / den) + 2500 if den else 3200
    if r < R[2]:
        den = R[2] - R[1]
        return int((float(r - R[1]) * 1300.0) / den) + 3200 if den else 4500
    if r < R[3]:
        den = R[3] - R[2]
        return int((float(r - R[2]) * 1500.0) / den) + 4500 if den else 6000
    if r < R[4]:
        den = R[4] - R[3]
        return int((float(r - R[3]) * 2500.0) / den) + 6000 if den else 8500
    den = R[4] - R[3]
    return int((float(r - R[4]) * 2500.0) / den) + 8500 if den else 8500


def _apply_tint(gains, cct, tint, table):
    """FUN_10013c29c: add the node-segment deltas scaled by the tint slider."""
    if cct < 3200:
        lo, hi, scale = table[0], table[1], 0.0014285714285714286
    elif cct < 4500:
        lo, hi, scale = table[1], table[2], 0.0007692307692307692
    elif cct < 6000:
        lo, hi, scale = table[2], table[3], 0.0006666666666666666
    else:
        lo, hi, scale = table[3], table[4], 0.0004
    d = float(tint) * scale
    return tuple(_short(g + _short(int(d * float(h - l)))) for g, l, h in zip(gains, lo[1:], hi[1:]))


def _ref_gains_from_cfa(levels4, cfa):
    """The look+0x4a..0x50 selection in FUN_1000ae728 (model id != 0)."""
    a, b, c, d = (f32(v) for v in levels4)
    f9 = f19 = a
    if cfa[0] != 0:
        f19 = f32(1.0)
    if cfa[0] != 2:
        f9 = f32(1.0)
    f21 = f23 = b
    if cfa[1] != 0:
        f23 = f19
    if cfa[1] != 2:
        f21 = f9
    f22 = f24 = c
    if cfa[2] != 0:
        f24 = f23
    if cfa[2] != 2:
        f22 = f21
    f19 = d
    if cfa[3] != 0:
        f9 = f24
    if cfa[3] != 2:
        f19 = f22
    return f32(f9 * f32(0.0009765625)), f32(f19 * f32(0.0009765625))


def wb_pick_gains(raw, black, levels, x0, y0, x1, y1, cfa=(0, 1, 1, 2), model_id=1,
                  ref_rb=None):
    """Custom/picker WB gains: FUN_1000ae728 + FUN_1000ae2e0 (storage type 1 path).

    Averages `raw - black` per CFA channel over the box between (x0, y0) and
    (x1, y1) (the picker expands a <3 px box by one pixel and two at the far
    edge), then blends G/R and G/B with the as-shot gains by scene luminance.
    Returns the two floats stored at decoder state +0xb0/+0xb4.
    """
    if tuple(cfa) != (0, 1, 1, 2):
        raise NotImplementedError(f"picker WB only ported for RGGB, got {cfa}")
    raw = np.asarray(raw)
    h, w = raw.shape
    xa, xb = (x0, x1) if x0 <= x1 else (x1, x0)
    ya, yb = (y0, y1) if y0 <= y1 else (y1, y0)
    xs = max(xa - (1 if xb - xa < 3 else 0), 0)
    xe = min(xb + 2 if xb - xa < 3 else xb, w - 1)
    ys = max(ya - (1 if yb - ya < 3 else 0), 0)
    ye = min(yb + 2 if yb - ya < 3 else yb, h - 1)
    blk = [f32(v) for v in black]
    sums = [f32(0.0)] * 4
    cnt = [f32(0.0)] * 4
    for y in range(ys, ye + 1):
        cy = (y & 1) << 1
        row = raw[y]
        for x in range(xs, xe + 1):
            c = (x & 1) | cy
            sums[c] = f32(f32(f32(row[x]) - blk[c]) + sums[c])
            cnt[c] = f32(cnt[c] + f32(1.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        avg = [f32(sums[i] / cnt[i]) for i in range(4)]
    R, G1, G2, B = avg
    Gavg = f32(f32(G1 + G2) * f32(0.5))          # (avg[2] + avg[1]) * 0.5
    ratioR = f32(32.0) if R == 0.0 else f32(Gavg / R)
    ratioB = f32(32.0) if B == 0.0 else f32(Gavg / B)
    ratioR = min(max(ratioR, WB_0_1), f32(32.0))
    ratioB = min(max(ratioB, WB_0_1), f32(32.0))
    # fmul 0.6; fmadd B*0.1; fmadd R*0.3 (single rounding each)
    L = fma32(R, WB_0_3, fma32(B, WB_0_1, f32(Gavg * WB_0_6)))
    wgt = f32(L * WB_W_SCALE) if L < 300.0 else f32(1.0)
    if L > 7000.0:
        wgt = fma32(L, WB_W_LIN_S, WB_W_LIN_C)
    wgt = min(max(wgt, f32(0.0)), f32(1.0))
    if model_id == 0:
        if ref_rb is None:
            raise ValueError("model_id 0 uses look+0x58/+0x5c; pass ref_rb")
        refR, refB = f32(ref_rb[0]), f32(ref_rb[1])
    else:
        refR, refB = _ref_gains_from_cfa(levels, cfa)
    one_w = f32(f32(1.0) - wgt)
    outR = fma32(refR, one_w, f32(wgt * ratioR))     # fmadd s1, s1, s5, s4
    outB = fma32(refB, one_w, f32(wgt * ratioB))     # fmadd s0, s2, s5, s0
    outR = min(max(outR, WB_0_2), f32(5.0))
    outB = min(max(outB, WB_0_2), f32(5.0))
    return float(outR), float(outB)


def wb_custom_gains(raw, black, levels, k_7809, x0, y0, x1, y1, *, exposure=0,
                    rb_factor=0, cfa=(0, 1, 1, 2), model_id=1, ref_rb=None):
    """Picker WB end to end: wb_pick_gains -> wb_gains(mode 3).

    `raw` is the unsubtracted CFA image in decoder coordinates; `levels` the
    as-shot 0x7313 array; black the 0x7310 levels.  Tint does not apply to
    mode 3 (see wb_gains)."""
    sr, sb = wb_pick_gains(raw, black, levels, x0, y0, x1, y1, cfa=cfa, model_id=model_id,
                           ref_rb=ref_rb)
    return wb_gains(levels, k_7809, 0, exposure, mode=3, state_r=sr, state_b=sb,
                    rb_factor=rb_factor)


def wb_pick_state(arw, raw, black, levels, *, cfa=(0, 1, 1, 2), model_id=1, ref_rb=None):
    """The two decoder-state gains for WB mode 3, from an Edit-saved ARW.

    FUN_1000d3438's mode-3 branch: when setting 0x9015 (dev+0x26c) is non-zero
    the gains are 0x9016 (dev+0x270/+0x274) / 1024; otherwise the picker box in
    0x8021 (dev+0x24c..: x0, y0, x1, y1) is sampled through FUN_1000ae728.
    `raw` is only needed for the picker branch.
    """
    import develop_settings as DS
    st = DS.read_settings(arw)
    if not st:
        raise ValueError("no develop settings block (ARW was not saved by Edit)")
    if int(st.get(DS.PICKER_USE_GAINS, 0)) != 0:
        g = st.get(DS.PICKER_GAINS, (0, 0))
        return (f32(f32(g[0]) * f32(0.0009765625)),
                f32(f32(g[1]) * f32(0.0009765625)))
    box = st.get(DS.PICKER_BOX)
    if not box or len(box) < 4:
        raise ValueError("picker box 0x8021 missing")
    return wb_pick_gains(raw, black, levels, int(box[0]), int(box[1]), int(box[2]), int(box[3]),
                         cfa=cfa, model_id=model_id, ref_rb=ref_rb)


def wb_pick_from_settings(arw, raw, black, levels, k_7809, *, exposure=None, rb_factor=None,
                          cfa=(0, 1, 1, 2), model_id=1, ref_rb=None):
    """Mode-3 WB end to end from an Edit-saved ARW (state gains + exposure/R-B factor)."""
    import develop_settings as DS
    st = DS.read_settings(arw)
    sr, sb = wb_pick_state(arw, raw, black, levels, cfa=cfa, model_id=model_id, ref_rb=ref_rb)
    if exposure is None:
        exposure = int(st.get(DS.EXPOSURE, 0))
    if rb_factor is None:
        rb_factor = int(st.get(DS.RB_FACTOR, 0))
    return wb_gains(levels, k_7809, 0, exposure, mode=3, state_r=sr, state_b=sb,
                    rb_factor=rb_factor)


def wb_temperature_table(tags):
    """Five WB nodes from SR2SubIFD tags (keys like '0x782d', as arw_looks.main_tags)."""
    def get(t):
        return [int(v) for v in str(tags[f"0x{t:04x}"]).split()]
    return [(2500, *get(0x782D)), (3200, *get(0x782C)), (4500, *get(0x7824)),
            (6000, *get(0x782B)), (8500, *get(0x782A))]


def wb_presets(tags):
    """Modes 4..0xc in Sony's order: tags 0x7820, 0x7821, 0x7825, 0x7826,
    0x7828, 0x7827, 0x7829, 0x7822, 0x7823."""
    out = []
    for t in (0x7820, 0x7821, 0x7825, 0x7826, 0x7828, 0x7827, 0x7829, 0x7822, 0x7823):
        out.append(tuple(int(v) for v in str(tags[f"0x{t:04x}"]).split()))
    return out


def wb_gains(levels_rggb, k_7809=256, tint=0, exposure=0, *, mode=1, temperature=None,
             temp_table=None, presets=None, custom=None, rb_factor=0,
             state_r=None, state_b=None, rgGB=True):
    """FUN_10013c4b0: (R, G, G, B) WB gains.

    mode (dev+0x1e8 / setting 0x8002): 1 = as shot, 2 = colour temperature
    (dev+0x1ec / 0x8013), 3 = precomputed custom gains (decoder state +0xb0/+0xb4),
    4..0xc = the camera's preset light sources, 0xd = custom, other = 1024 each.
    tint = dev+0x1f0 (0x8014), exposure = dev+0x1f8 (0x8018, 1/300 EV),
    rb_factor = dev+0x1f4 (0x8015).

    levels_rggb is the as-shot 0x7313 array.  temp_table comes from
    wb_temperature_table(main_tags); it is needed for mode 2 and for any tint.
    """
    r, g, _, b = (int(v) for v in levels_rggb)
    if mode == 1:
        cur = (r, g, b)
    elif mode == 2:
        if temperature is None or temp_table is None:
            raise ValueError("mode 2 needs temperature and temp_table (wb_temperature_table)")
        cur = _interp_wb(temperature, temp_table)
    elif mode == 3:
        if state_r is None or state_b is None:
            raise ValueError("mode 3 needs the decoder state gains state_r/state_b")
        cur = (_short(int(float(np.float32(state_r)) * 1024.0)), 1024,
               _short(int(float(np.float32(state_b)) * 1024.0)))
    elif 4 <= mode <= 0xC:
        if presets is None or len(presets) < mode - 3:
            raise ValueError(f"mode {mode} needs presets (wb_presets)")
        cur = tuple(int(v) for v in presets[mode - 4])
    elif mode == 0xD:
        if custom is None:
            raise ValueError("mode 0xd needs custom WB levels")
        cur = tuple(int(v) for v in custom)
    else:
        cur = (0x400, 0x400, 0x400)

    if tint and mode not in (2, 3):      # cases 2 and 3 jump past c0bc/c29c (no tint)
        if temp_table is None:
            raise ValueError("tint needs temp_table (wb_temperature_table)")
        cur = _apply_tint(cur, _cct_from_red(cur[0], temp_table), tint, temp_table)

    gains = (cur[0], cur[1], cur[1], cur[2]) if rgGB else (cur[1], cur[0], cur[2], cur[1])

    d0 = float(np.float32(k_7809)) * 0.00390625 * math.exp2(float(exposure) / 300.0)
    out = [_short(int(d0 * v)) for v in gains]

    e = math.exp2(abs(float(rb_factor)) * 0.01 * 0.6780719051126377)
    f = e if rb_factor >= 0 else 2.0 - e
    ri, bi = (0, 3) if rgGB else (1, 2)
    for i in (ri, bi):
        out[i] = _short(int(f * int(d0 * gains[i])))
    return tuple(out)


def cfa_scale(raw, black, gains, scale_7311):
    """Per-sample scaling of the Demosaic task (int16 result).

    black / gains are indexed like Sony: (x & 1) | ((y & 1) << 1), i.e. R G G B for RGGB."""
    raw = np.asarray(raw, np.float64)
    idx = np.empty(raw.shape, np.int64)
    for y in range(2):
        for x in range(2):
            idx[y::2, x::2] = x | (y << 1)
    blk = np.asarray(black, np.float64)[idx]
    g = np.asarray(gains, np.float64)[idx]
    v = np.trunc(float(scale_7311) * SCALE_2_19 * g * (raw - blk))
    return np.clip(v, -0x8000, 0x7fff).astype(np.int16)


def itp_scale(raw, black, gains, scale_7311):
    """ZcTaskITP scaling FUN_1000f13bc (the RGGB path): float32
        out = (scale * 2^-15 * gain[cfa]) * 0.0625 * (float)(raw - black[cfa])"""
    raw = np.asarray(raw, np.int64)
    idx = np.empty(raw.shape, np.int64)
    for y in range(2):
        for x in range(2):
            idx[y::2, x::2] = x | (y << 1)
    f17 = np.float32(np.float32(scale_7311) * np.float32(3.0517578e-05))
    k = np.array([np.float32(f17 * np.float32(g)) for g in gains], np.float32)
    d = (raw - np.asarray(black, np.int64)[idx]).astype(np.float32)
    return ((k[idx] * np.float32(0.0625)).astype(np.float32) * d).astype(np.float32)


def block_average(cfa, s, colors=((0, 1), (1, 2))):
    """Average each colour's CFA samples inside s x s blocks -> (H/s, W/s, 3) float64.

    colors[y][x] is the channel at CFA position (y, x); default RGGB."""
    assert s % 2 == 0
    h, w = cfa.shape[0] // s * s, cfa.shape[1] // s * s
    out = np.zeros((h // s, w // s, 3))
    cnt = np.zeros(3)
    for y in range(2):
        for x in range(2):
            c = colors[y][x]
            sub = cfa[y:h:2, x:w:2].astype(np.float64)
            out[..., c] += sub.reshape(h // s, s // 2, w // s, s // 2).mean(axis=(1, 3))
            cnt[c] += 1
    return out / cnt
