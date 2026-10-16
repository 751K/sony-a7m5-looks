#!/usr/bin/env python3
"""Patch values of a Camera Raw export of ../chart/SonyChart.ARW, in the export's own encoding
(no colour conversion; export as 16-bit ProPhoto RGB so values are the RGBTable output directly).
Registration and patch sampling as in ../capture_one/measure_chart.py.
usage: measure_acr_chart.py EXPORT.tif ...  -> ../chart/measure_<name>.json
"""
import io
import json
import sys
from pathlib import Path

import numpy as np
import tifffile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "capture_one")); sys.path.insert(0, str(HERE.parent / "sonylook"))
import measure_chart as M  # noqa: E402


def load_raw(path):
    from PIL import ImageCms
    with tifffile.TiffFile(path) as t:
        p = t.pages[0]
        a = p.asarray()[..., :3].astype(np.float64) / (65535 if p.dtype == np.uint16 else 255)
        icc = p.tags.get(34675)
        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc.value))) if icc else "none"
    return a, desc.strip(), str(p.dtype)


def main():
    for path in sys.argv[1:]:
        img, desc, dt = load_raw(path)
        H, W, _ = img.shape
        A, n, res = M.register(img)
        vals = []
        for p in M.SPEC["patches"]:
            x0, y0, x1, y1 = p["box_crop"]
            xs = np.arange(x0 + (x1 - x0) // 4, x1 - (x1 - x0) // 4)
            ys = np.arange(y0 + (y1 - y0) // 4, y1 - (y1 - y0) // 4)
            Xg, Yg = np.meshgrid(xs, ys)
            sx = np.clip(np.round(A[0, 0] * Xg + A[0, 1] * Yg + A[0, 2]).astype(int), 0, W - 1)
            sy = np.clip(np.round(A[1, 0] * Xg + A[1, 1] * Yg + A[1, 2]).astype(int), 0, H - 1)
            px = img[sy, sx].reshape(-1, 3)
            vals.append(dict(i=p["i"], v=np.median(px, 0).tolist(), p90=np.percentile(px, 90, 0).tolist(),
                             p10=np.percentile(px, 10, 0).tolist()))
        out = dict(export=path, icc=desc, dtype=dt, size=[W, H], affine=A.tolist(), fit_points=n,
                   fit_residual_px=res, patches=vals)
        name = Path(path).stem.replace(" ", "_")
        json.dump(out, open(M.CHART / f"measure_{name}.json", "w"), indent=1)
        print(path, desc, dt, (W, H), "fit", n, round(res, 2), "grey G:",
              [round(v["v"][1], 4) for v in vals[:48:6]])


if __name__ == "__main__":
    main()
