#!/usr/bin/env python3
"""Sony look LUTs (.cube) for DxO PhotoLab 10.1.

PhotoLab setup: Color Rendering = DCP Profile 'SonyA7M5 Linear Base v2.dcp', DCP Curve = Adobe,
Protect Saturated Colors 0; LUT Grading with LUT Color Space = ProPhoto RGB, Intensity 100.

Node v (ProPhoto gamma 1.8, the LUT input) -> Sony linear = dxo_model.E_inv(v) -> SSCS -> look chain
(pointwise) -> sRGB -> linear ProPhoto -> gamma 1.8.  Nothing after the LUT changes in-gamut colours.
usage: dxo_look.py ARW [--grid 64] [--looks Standard,FL2,...] [--write] [--eval]
"""
import argparse
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent / "camera_raw", HERE.parent / "capture_one", HERE.parent / "sonylook"):
    sys.path.insert(0, str(p))
import acr_look as AL  # noqa: E402
import c1_look_profile as C  # noqa: E402
import dxo_model as D  # noqa: E402
import dxo_profiles as P  # noqa: E402
import sony_render as R  # noqa: E402
import sony_sscs as SC  # noqa: E402


def build(look, n):
    lin = D.E_inv(P.grid(n))
    return AL.srgb_to_prophoto_v(C.look_srgb(look, lin))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arw", help="any A7M5 ARW (look parameters are read from it; --eval also renders it)")
    ap.add_argument("--grid", type=int, default=64)
    ap.add_argument("--looks", default=",".join(C.NAMES))
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--no-sscs", action="store_true")
    ap.add_argument("--out", help="output folder (default profiles/dxo)")
    a = ap.parse_args()
    out = Path(a.out) if a.out else P.OUT
    looks = R.load_looks(a.arw)
    sscs = None if a.no_sscs else SC.params_from_arw(a.arw)
    lin = np.minimum(R.front_end_sony(a.arw, 4), D.CLIP) if a.eval else None
    if a.write:
        out.mkdir(parents=True, exist_ok=True)
    for name in a.looks.split(","):
        look = R.Look(name, looks, sscs=sscs)
        tab = build(look, a.grid)
        msg = f"{C.NAMES[name]:4s}"
        if a.eval:
            ref = C.look_srgb(look, lin)
            got = AL.prophoto_v_to_srgb(AL.interp(tab, D.E(lin), "tet"))
            e = np.abs(got - ref) * 255
            msg += f"  tet: MAE {e.mean():.3f} p99 {np.percentile(e, 99):.2f}"
        if a.write:
            title = f"SonyA7M5 Look {C.NAMES[name]}"
            msg += "  " + P.write_cube(out / f"{title}.cube", tab, title).name
        print(msg, flush=True)


if __name__ == "__main__":
    main()
