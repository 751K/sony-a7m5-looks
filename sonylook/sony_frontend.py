#!/usr/bin/env python3
"""Sony front end for the reference renderer (RAW -> LinearMatrix16 input).

Recovered with REA from Imaging Edge Edit 4.0.00.10312 (arm64):

* ZcTaskDemosaic process 0x100138ac8, before interpolation, for each CFA sample:
      out = (int)(look[0x40] * 2^-19 * gain[cfa] * (raw - look[0x38 + cfa]))   clamp int16
  look+0x38 = SR2SubIFD 0x7310 BlackLevel (512 x4), look+0x40 = 0x7311 (256),
  evaluated in double, left to right, truncated toward zero.
* gain[] from FUN_10013c4b0: WB mode dev+0x1e8 (setting 0x8002) = 1 "as shot" ->
  look+0x4a/0x4c/0x50 = 0x7313 WB_RGGBLevels R/G/B (2653 1024 1416). The tint step
  FUN_10013c29c scales by dev+0x1f0 (0x8014 = 0) and adds nothing; the exposure
  factor exp2(dev+0x1f8 / 300) and the dev+0x1f4 (0x8015) R/B factor are both 1 here.
* For RGGB sensors (look+0x84..0x90 != 1,0,2,1) the builder inserts ZcTaskITP (0x1000f95d0)
  instead of ZcTaskDemosaic; its scaling FUN_1000f13bc uses the same factors in float32
  (itp_scale) and the interpolated planes are clamped to 0..16383 and truncated to u16.
* No camera matrix follows: with 0x780f present (look+0x1c2 != 0) the builder inserts
  LinearMatrix16 only, so the 0x780f table works directly on white-balanced camera
  RGB. The per-look 0x7800 matrix feeds the non-16 LinearMatrix (FUN_1000d4cfc ->
  render+0x18a4) and is unused here.

The interpolation itself (Sony demosaic) is not ported. For box-downsampled comparisons
the CFA samples are averaged per channel inside each s x s block, which equals
box(demosaic(x)) up to edge effects of the interpolator.
"""
import numpy as np

SCALE_2_19 = 1.9073486328125e-06


def wb_gains(levels_rggb, k_7809=256, tint=0, exposure=0):
    """FUN_10013c4b0 for WB mode 1, tint 0, 0x8015 = 0: (R, G, G, B) shorts.

    After the WB/tint step every gain is multiplied (asm 0x10013c6d4..0x10013c750; the Ghidra
    pseudo code drops this) by d0 = (look[0x9d8] / 256) * exp2(EV / 300), truncated toward zero.
    look+0x9d8 = SR2SubIFD 0x7809 (264 here; 256 when 0x7807 is absent)."""
    if tint or exposure:
        raise NotImplementedError("only the default develop settings are ported")
    d0 = (float(k_7809) * 0.00390625) * 1.0          # fmul d0, d1, d0 with exp2(0) = 1
    r, g, _, b = (int(v) for v in levels_rggb)
    return tuple(int(d0 * v) for v in (r, g, g, b))


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
