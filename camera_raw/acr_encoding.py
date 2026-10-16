#!/usr/bin/env python3
"""Camera Raw's mapping from Sony linear camera RGB to the RGBTable input, measured on the chart
with the identity 'Probe' table on 'SonyA7M5 Linear Base' (all sliders default, lens corrections off).

Model (checked on the 6x6x6 cube): x = table input in linear ProPhoto (v^1.8). Per pixel the largest and
smallest channel go through one 1D curve t (grey ramp), the middle channel keeps its linear ratio:
  x_mid = x_min + (x_max - x_min) * (l_mid - l_min) / (l_max - l_min)
(hue-preserving RGB tone, as DNG SDK's RefBaselineRGBTone). t is linear in the mid-tones
(x = l / 6422) with a shoulder above ~1500 and a slight toe below ~5.
usage: acr_encoding.py ../chart/measure_<Probe export>.json  (-> acr_encoding.json, prints checks)
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
TABLE = HERE / "acr_encoding.json"
CLIP = 0.515625 * (16383 - 512)          # Sony linear at sensor clip (G)


def fit(measure_path):
    S = json.load(open(HERE.parent / "chart/SonyChart.json"))["patches"]
    L = np.array([p["lin_sony"] for p in S])
    P = np.array([p["v"] for p in json.load(open(measure_path))["patches"]])
    l = L[:48].mean(1); x = (P[:48] ** 1.8).mean(1)
    keep = x > 0
    ll, lx = np.log(l[keep]), np.log(x[keep])
    lo = np.polyfit(ll[:4], lx[:4], 1)[0]; hi = np.polyfit(ll[-4:], lx[-4:], 1)[0]
    d = dict(log_lin=ll.tolist(), log_x=lx.tolist(), slope_low=float(lo), slope_high=float(hi),
             source=f"{Path(measure_path).name} (grey ramp, mean of RGB)")
    json.dump(d, open(TABLE, "w"), indent=1)
    return L, P


def _load():
    d = json.loads(TABLE.read_text())
    return np.array(d["log_lin"]), np.array(d["log_x"]), d["slope_low"], d["slope_high"]


def t(l):
    """Sony linear -> ACR linear (1D)."""
    ll, lx, lo, hi = _load()
    z = np.log(np.maximum(l, 1e-6))
    y = np.interp(z, ll, lx)
    y = np.where(z < ll[0], lx[0] + lo * (z - ll[0]), y)
    y = np.where(z > ll[-1], lx[-1] + hi * (z - ll[-1]), y)
    return np.exp(y)


def t_inv(x):
    ll, lx, lo, hi = _load()
    z = np.log(np.maximum(x, 1e-12))
    y = np.interp(z, lx, ll)
    y = np.where(z < lx[0], ll[0] + (z - lx[0]) / lo, y)
    y = np.where(z > lx[-1], ll[-1] + (z - lx[-1]) / hi, y)
    return np.exp(y)


def _hue_preserve(a, f):
    """Apply 1D f to max and min channel of a (..., 3); middle channel keeps its ratio."""
    a = np.asarray(a, np.float64)
    order = np.argsort(a, -1)
    s = np.take_along_axis(a, order, -1)
    lo, mid, hi = s[..., 0], s[..., 1], s[..., 2]
    flo, fhi = f(lo), f(hi)
    r = np.where(hi > lo, (mid - lo) / np.where(hi > lo, hi - lo, 1), 0)
    out_s = np.stack([flo, flo + r * (fhi - flo), fhi], -1)
    out = np.empty_like(out_s)
    np.put_along_axis(out, order, out_s, -1)
    return out


def forward(lin):
    """Sony linear RGB -> RGBTable input, encoded (ProPhoto gamma 1.8)."""
    return np.clip(_hue_preserve(lin, t), 0, None) ** (1 / 1.8)


def inverse(v):
    """RGBTable input (encoded) -> Sony linear RGB."""
    return _hue_preserve(np.clip(v, 0, None) ** 1.8, t_inv)


if __name__ == "__main__":
    L, P = fit(sys.argv[1])
    ok = (P.max(1) < 0.995) & (P.min(1) > 0.003)
    pred = forward(L)
    e = np.abs(pred - P)[ok] * 255
    print("chart forward model: mean %.3f  p95 %.3f  max %.3f (8-bit, %d patches)" % (e.mean(), np.percentile(e, 95), e.max(), ok.sum()))
    rt = forward(inverse(P[ok]))
    print("round trip max", np.abs(rt - P[ok]).max() * 255)
    print("t(clip) =", float(t(CLIP)), "-> v", float(t(CLIP)) ** (1 / 1.8))
