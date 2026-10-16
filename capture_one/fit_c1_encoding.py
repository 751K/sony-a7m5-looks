#!/usr/bin/env python3
"""Fit Capture One's profile-input encoding from a chart export made with the Probe profile.

SonyA7M5-Probe.icc outputs its input v unchanged as sRGB, so the export's patch values are the v that
C1 feeds the profile for each patch's known Sony linear value (per channel; channels are independent).
Pooled samples are binned in log(linear); bin medians (made monotone) give log(lin) -> log(v), with the
end slopes for extrapolation.
usage: fit_c1_encoding.py ../chart/measure_<probe export>.json  -> c1_encoding_table.json
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CHART = HERE.parent / "chart"


def fit(measure_path, spec_path=CHART / "SonyChart.json"):
    lin = np.array([p["lin_sony"] for p in json.load(open(spec_path))["patches"]])
    Pr = np.array([p["srgb"] for p in json.load(open(measure_path))["patches"]])
    L, V = lin.ravel(), Pr.ravel()
    k = (V > 0.0003) & (V < 0.99) & (L > 0.5)
    L, V = L[k], V[k]
    o = np.argsort(L); L, V = L[o], V[o]
    ll = np.log(L); edges = np.linspace(ll.min(), ll.max(), 36)
    cen, med = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (ll >= a) & (ll <= b)
        if m.sum() >= 3:
            cen.append(np.median(ll[m])); med.append(np.log(np.median(V[m])))
    cen, med = np.array(cen), np.maximum.accumulate(np.array(med))
    s_lo = (med[2] - med[0]) / (cen[2] - cen[0]); s_hi = (med[-1] - med[-3]) / (cen[-1] - cen[-3])
    return dict(log_lin=cen.tolist(), log_v=med.tolist(), slope_low=float(s_lo), slope_high=float(s_hi),
                source="chart/SonyChart.ARW probe export (measure_chart.py)")


if __name__ == "__main__":
    t = fit(sys.argv[1])
    json.dump(t, open(HERE / "c1_encoding_table.json", "w"), indent=1)
    print("bins", len(t["log_lin"]), "slopes", round(t["slope_low"], 3), round(t["slope_high"], 3))
