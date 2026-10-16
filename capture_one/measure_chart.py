#!/usr/bin/env python3
"""Read patch values from a Capture One export of chart/SonyChart.ARW.

The export is converted to sRGB (embedded ICC: Adobe RGB or sRGB), registered to the chart layout in
crop coordinates with an affine fit to the detected patch centroids (C1 exports a larger area),
and the median of the central 50% of every patch is taken.
usage: measure_chart.py EXPORT.tif [EXPORT2.tif ...]  -> ../chart/measure_<name>.json
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "sonylook"))
import compare_c1_export as X  # noqa: E402

CHART = HERE.parent / "chart"
SPEC = json.loads((CHART / "SonyChart.json").read_text()) if (CHART / "SonyChart.json").exists() else None


def register(img):
    """Fit export = A @ [x, y, 1] (crop coords -> export pixels) from patch centroids. The border grey
    is the most common colour; patches are the connected regions that differ from it."""
    from scipy import ndimage
    q = np.round(img[::4, ::4].reshape(-1, 3) * 255).astype(int)
    vals, cnt = np.unique(q, axis=0, return_counts=True)
    bg = vals[cnt.argmax()] / 255
    dif = np.abs(img - bg).max(-1) > 3 / 255
    lab, n = ndimage.label(ndimage.binary_opening(dif, iterations=3))
    objs = ndimage.find_objects(lab)
    cents = []
    for k, sl in enumerate(objs):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        area = (lab[sl] == k + 1).sum()
        if 120 < w < 500 and 100 < h < 400 and area > 0.8 * w * h:
            cents.append(((sl[1].start + sl[1].stop) / 2, (sl[0].start + sl[0].stop) / 2, w, h))
    cents = np.array(cents)
    exp = np.array([[(p["box_crop"][0] + p["box_crop"][2]) / 2, (p["box_crop"][1] + p["box_crop"][3]) / 2]
                    for p in SPEC["patches"]])
    pw = np.median([p["box_crop"][2] - p["box_crop"][0] for p in SPEC["patches"]])
    # initial scale/offset from the detected cloud vs the design grid, then nearest matching + LSQ
    s0 = np.median(cents[:, 2]) / pw
    A = np.array([[s0, 0, cents[:, 0].min() - s0 * exp[:, 0].min()], [0, s0, cents[:, 1].min() - s0 * exp[:, 1].min()]])
    for _ in range(5):
        pred = np.c_[exp, np.ones(len(exp))] @ A.T
        d = np.hypot(pred[:, None, 0] - cents[None, :, 0], pred[:, None, 1] - cents[None, :, 1])
        j = d.argmin(1); ok = d[np.arange(len(exp)), j] < pw * 0.3
        M = np.c_[exp[ok], np.ones(ok.sum())]
        A = np.linalg.lstsq(M, cents[j[ok], :2], rcond=None)[0].T
    res = np.hypot(*(np.c_[exp[ok], np.ones(ok.sum())] @ A.T - cents[j[ok], :2]).T)
    return A, int(ok.sum()), float(np.median(res))


def main():
    for path in sys.argv[1:]:
        img, desc = X.load(path)
        H, W, _ = img.shape
        A, n, res = register(img)
        vals = []
        for p in SPEC["patches"]:
            x0, y0, x1, y1 = p["box_crop"]
            xs = np.arange(x0 + (x1 - x0) // 4, x1 - (x1 - x0) // 4)
            ys = np.arange(y0 + (y1 - y0) // 4, y1 - (y1 - y0) // 4)
            Xg, Yg = np.meshgrid(xs, ys)
            sx = np.clip(np.round(A[0, 0] * Xg + A[0, 1] * Yg + A[0, 2]).astype(int), 0, W - 1)
            sy = np.clip(np.round(A[1, 0] * Xg + A[1, 1] * Yg + A[1, 2]).astype(int), 0, H - 1)
            px = img[sy, sx].reshape(-1, 3)
            vals.append(dict(i=p["i"], srgb=np.median(px, 0).tolist(), spread=np.percentile(px, 90, 0).tolist()))
        out = dict(export=path, icc=desc.strip(), affine=A.tolist(), fit_points=n,
                   fit_residual_px=res, patches=vals)
        name = Path(path).stem.replace(" ", "_")
        json.dump(out, open(CHART / f"measure_{name}.json", "w"), indent=1)
        grey = [v["srgb"] for v in vals[:48]]
        print(path, desc.strip(), "fit", n, round(res, 2), "grey ramp (8-bit G):",
              [round(g[1] * 255, 1) for g in grey[::6]])


if __name__ == "__main__":
    main()
