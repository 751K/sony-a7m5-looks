#!/usr/bin/env python3
"""E for DxO PhotoLab 10.1: Sony linear camera RGB -> the value PhotoLab feeds the look LUT.

Setup: base DCP 'SonyA7M5 Linear Base v2' (no BaselineExposureOffset), DCP Curve = Adobe,
LUT Color Space = ProPhoto RGB.  The LUT input is ProPhoto encoded with gamma 1.8.

    lin_sony (white-balanced camera RGB, Sony scale)
      -> highlight recovery (only when max channel > 0.25*s3*K_G, i.e. lin ~3045):
           x = (512 + lin/K) * W/s3,  b = x*gain,  a = 0.75*max(x),  c = W*max(gain)
           out = b if b < a else min(a + (b-a)(W-a) / ((c-W)(b-a)/(c-a) + W - a), b)
           lin' = (out*s3/W / gain - 512) * K
      -> A (camera -> linear ProPhoto D50, Sony scale; 6422 = 1.0)
      -> DxO Wide Gamut: negative components clipped to 0, back to ProPhoto
      -> measured 1D curve per channel (e_curve_pure.csv: Lw after the kernel -> encoded v)

The curve was built from the neutral ramp of chart/SonyChart (v2 baseline, Base_v2 export).  Below the
highlight-recovery threshold it is gamma 1.8 with scale 6422; above it there is a further compression
(decoded 0.86 at Lw 6000, 0.77 at 7200) whose source is not located.  Chart error (8-bit codes):
below threshold 0.11 mean / 3.4 max, above 4.6 / 26 (all of it in colour patches).
Source: Denoise/docs/DxO色彩管线-交接.md and verify_kernel.py.

E_inv inverts E per node by fixed-point iteration on an approximate inverse (curve^-1, A^-1, no clip,
no kernel); the WG clip makes some encoded values unreachable, those converge to the nearest reachable.
"""
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
W = 32767.0
BLACK = 512.0
K = np.array([1.33545, 0.515625, 0.712891])          # raw - black -> lin_sony
GAIN = np.array([2.5898416, 1.0, 1.38281214])         # WB gains seen by the kernel (= K / K_G)
S3 = 23623.0                                          # InputParameters["WhiteLevel"] with highlights
SCALE = 6422.0
A = np.array([[0.533722, 0.278641, 0.187637],
              [0.131256, 0.867798, 0.000946],
              [0.136124, 0.003159, 0.860718]])
A_INV = np.linalg.inv(A)
MAT_WG = np.array([[1.12442, 0.011673, -0.136093],
                   [-0.069992, 1.080063, -0.010071],
                   [0.006409, -0.054428, 1.048019]])
MAT_PP = np.array([[0.888382, -0.003784, 0.115402],
                   [0.057556, 0.926086, 0.016357],
                   [-0.002441, 0.048111, 0.954330]])
CLIP = K * (16383 - BLACK)                            # sensor clip per channel, Sony scale


def _curve():
    c = np.loadtxt(HERE / "e_curve_pure.csv", delimiter=",", skiprows=1)
    c = c[np.argsort(c[:, 0])]
    keep = np.concatenate([[True], (np.diff(c[:, 0]) > 0) & (np.diff(c[:, 1]) > 0)])
    x, v = c[keep, 0], c[keep, 1]
    x = np.concatenate([[0.0], x])
    v = np.concatenate([[0.0], v])
    # beyond the last patch: continue the last segment's slope in the linear domain
    lx = v ** 1.8 * SCALE
    slope = (lx[-1] - lx[-2]) / (x[-1] - x[-2])
    return x, v, slope


_CX, _CV, _SLOPE = _curve()


def curve(Lw):
    """Lw (Sony scale, after the kernel) -> encoded v, per channel."""
    Lw = np.maximum(Lw, 0.0)
    v = np.interp(Lw, _CX, _CV)
    lin_hi = (_CV[-1] ** 1.8 * SCALE + _SLOPE * (Lw - _CX[-1])) / SCALE
    return np.where(Lw > _CX[-1], np.maximum(lin_hi, 0) ** (1 / 1.8), v)


def curve_inv(v):
    v = np.clip(v, 0.0, None)
    Lw = np.interp(v, _CV, _CX)
    hi = v > _CV[-1]
    Lw_hi = _CX[-1] + (v ** 1.8 * SCALE - _CV[-1] ** 1.8 * SCALE) / _SLOPE
    return np.where(hi, Lw_hi, Lw)


def kernel(lin):
    """Highlight recovery on Sony-scale camera RGB (..., 3)."""
    shp = lin.shape
    lin = lin.reshape(-1, 3)
    x = np.clip((BLACK + lin / K) * (W / S3), 0, W)
    b = x * GAIN
    m = x.max(1)
    a = 0.75 * m
    c = W * GAIN.max()
    den = (c - W) * ((b - a[:, None]) / np.maximum(c - a[:, None], 1e-9)) + (W - a)[:, None]
    comp = a[:, None] + (b - a[:, None]) * (W - a)[:, None] / np.maximum(den, 1e-9)
    out = np.clip(np.where(b < a[:, None], b, np.minimum(comp, b)), 0, W)
    after = np.clip((out * (S3 / W) / GAIN - BLACK) * K, 0, None)
    gate = lin.max(1) > 0.25 * S3 * K[1]
    return np.where(gate[:, None], after, lin).reshape(shp)


def wg_clip(Lw):
    return np.clip(Lw @ MAT_WG.T, 0, None) @ MAT_PP.T


def E(lin):
    """Sony linear camera RGB (..., 3) -> LUT input, ProPhoto gamma 1.8 encoded."""
    return curve(wg_clip(kernel(np.maximum(lin, 0)) @ A.T))


def _approx_inv(v):
    return curve_inv(v) @ A_INV.T


def E_inv(v, iters=40):
    """LUT input (..., 3) -> Sony linear camera RGB, clamped to [0, sensor clip]."""
    target = _approx_inv(v)
    lin = np.clip(target, 0, CLIP)
    for _ in range(iters):
        lin = np.clip(lin + (target - _approx_inv(E(lin))), 0, CLIP)
    return lin
