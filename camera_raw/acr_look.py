#!/usr/bin/env python3
"""Sony look RGBTables for Camera Raw (on 'SonyA7M5 Linear Base').

Node v (ProPhoto gamma 1.8, the table input) -> Sony linear = acr_encoding.inverse(v) (clamped at sensor
clip) -> SSCS (sony_sscs, float path) -> verified look chain (pointwise) -> sRGB -> XYZ D50 -> linear ProPhoto -> gamma 1.8.
usage: acr_look.py ARW [--grid 64] [--looks Standard,FL2,...] [--write] [--eval]
"""
import argparse
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "capture_one"))
sys.path.insert(0, str(HERE.parent / "sonylook"))
import acr_encoding as E  # noqa: E402
import acr_profiles as A  # noqa: E402
import c1_look_profile as C  # noqa: E402
import sony_render as R  # noqa: E402
import sony_sscs as SC  # noqa: E402

XYZ_TO_PROPHOTO = np.linalg.inv(A.PROPHOTO_TO_XYZ)
SRGB_TO_PROPHOTO = XYZ_TO_PROPHOTO @ C.CHAD @ C.M_SRGB


def srgb_dec(s):
    return np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)


def srgb_enc(l):
    l = np.clip(l, 0, 1)
    return np.where(l <= 0.0031308, 12.92 * l, 1.055 * l ** (1 / 2.4) - 0.055)


def srgb_to_prophoto_v(s):
    return np.clip(srgb_dec(s) @ SRGB_TO_PROPHOTO.T, 0, 1) ** (1 / 1.8)


def prophoto_v_to_srgb(v):
    return srgb_enc(np.clip(v, 0, None) ** 1.8 @ np.linalg.inv(SRGB_TO_PROPHOTO).T)


def build(look, n):
    lin = np.minimum(E.inverse(A.grid(n)), E.CLIP)
    return srgb_to_prophoto_v(C.look_srgb(look, lin))


def interp(table, v, mode="tri"):
    n = table.shape[0]
    p = np.clip(v, 0, 1) * (n - 1)
    i = np.minimum(p.astype(int), n - 2); f = p - i
    if mode == "tri":
        out = 0
        for dr in (0, 1):
            for dg in (0, 1):
                for db in (0, 1):
                    w = (f[..., 0] if dr else 1 - f[..., 0]) * (f[..., 1] if dg else 1 - f[..., 1]) * (f[..., 2] if db else 1 - f[..., 2])
                    out = out + w[..., None] * table[i[..., 0] + dr, i[..., 1] + dg, i[..., 2] + db]
        return out
    # tetrahedral
    fr, fg, fb = f[..., 0], f[..., 1], f[..., 2]
    c = lambda a, b, d: table[i[..., 0] + a, i[..., 1] + b, i[..., 2] + d]
    c000, c111 = c(0, 0, 0), c(1, 1, 1)
    out = np.zeros(v.shape)
    conds = [(fr >= fg) & (fg >= fb), (fr >= fb) & (fb > fg), (fb > fr) & (fr >= fg),
             (fg > fr) & (fr >= fb), (fg >= fb) & (fb > fr), (fb > fg) & (fg > fr)]
    seqs = [((1, 0, 0), (1, 1, 0), fr, fg, fb), ((1, 0, 0), (1, 0, 1), fr, fb, fg), ((0, 0, 1), (1, 0, 1), fb, fr, fg),
            ((0, 1, 0), (1, 1, 0), fg, fr, fb), ((0, 1, 0), (0, 1, 1), fg, fb, fr), ((0, 0, 1), (0, 1, 1), fb, fg, fr)]
    for m, (a, b, x, y, z) in zip(conds, seqs):
        ca, cb = c(*a), c(*b)
        val = c000 + x[..., None] * (ca - c000) + y[..., None] * (cb - ca) + z[..., None] * (c111 - cb)
        out[m] = val[m]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arw", help="any A7M5 ARW (look parameters are read from it; --eval also renders it)")
    ap.add_argument("--grid", type=int, default=64)
    ap.add_argument("--looks", default=",".join(C.NAMES))
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--no-sscs", action="store_true",
                    help="leave out Sony's SSCS stage (bright, saturated colours towards luma; in since v1.1.0)")
    ap.add_argument("--out", help="output folder (default profiles/camera_raw)")
    a = ap.parse_args()
    if a.out:
        A.OUT = Path(a.out)
    looks = R.load_looks(a.arw)
    sscs = None if a.no_sscs else SC.params_from_arw(a.arw)
    lin = R.front_end_sony(a.arw, 4) if a.eval else None
    if a.write:
        A.OUT.mkdir(exist_ok=True); A.write_base()
    for name in a.looks.split(","):
        look = R.Look(name, looks, sscs=sscs)
        tab = build(look, a.grid)
        msg = f"{C.NAMES[name]:4s}"
        if a.eval:
            ref = C.look_srgb(look, np.minimum(lin, E.CLIP))
            v = E.forward(lin)
            for mode in ("tri", "tet"):
                got = prophoto_v_to_srgb(interp(tab, v, mode))
                e = np.abs(got - ref) * 255
                msg += f"  {mode}: MAE {e.mean():.3f} p99 {np.percentile(e, 99):.2f}"
        if a.write:
            msg += "  " + A.write_look(f"Look {C.NAMES[name]}", tab).name
        print(msg, flush=True)


if __name__ == "__main__":
    main()
