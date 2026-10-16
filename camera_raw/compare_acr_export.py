#!/usr/bin/env python3
"""Compare a Camera Raw export with Edit's export of the same look.

Like ../capture_one/compare_c1_export.py, but the geometry is fitted with a cubic polynomial displacement
(ACR without lens profile keeps the barrel distortion that Edit corrects; an affine map is not enough).
usage: compare_acr_export.py ACR.tif EDIT.tif
"""
import json
import sys
from pathlib import Path

import numpy as np
import tifffile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "capture_one")); sys.path.insert(0, str(HERE.parent / "sonylook"))
import compare_c1_export as X  # noqa: E402
import sony_render as R  # noqa: E402


def basis(x, y, W, H):
    u, v = x / W - 0.5, y / H - 0.5
    return np.stack([u ** i * v ** j for i in range(4) for j in range(4 - i)], -1)


def fit_poly(img, ed, s=2, N=256, ny=9, nx=12):
    g = lambda a: np.hypot(*np.gradient(np.log1p(R.box(a, s).mean(-1) * 255)))
    A, S = g(img), g(ed)
    H, W = S.shape
    rows = []
    for cy in np.linspace(N, H - N, ny).astype(int):
        for cx in np.linspace(N, W - N, nx).astype(int):
            dx, dy, q = X.phase(A[cy - N // 2:cy + N // 2, cx - N // 2:cx + N // 2], S[cy - N // 2:cy + N // 2, cx - N // 2:cx + N // 2])
            rows.append((cx * s, cy * s, dx * s, dy * s, q))
    r = np.array(rows, float)
    Hf, Wf = ed.shape[:2]
    B = basis(r[:, 0], r[:, 1], Wf, Hf)
    keep = r[:, 4] > 0.03
    for _ in range(4):
        cx_, *_ = np.linalg.lstsq(B[keep], r[keep, 2], rcond=None); cy_, *_ = np.linalg.lstsq(B[keep], r[keep, 3], rcond=None)
        res = np.hypot(B @ cx_ - r[:, 2], B @ cy_ - r[:, 3])
        keep = (r[:, 4] > 0.03) & (res < max(3, 3 * np.median(res[keep])))
    return cx_, cy_, int(keep.sum()), float(np.median(res[keep]))


def warp(img, cx_, cy_, shape):
    H, W = shape
    y, x = np.mgrid[0:H, 0:W].astype(np.float64)
    B = basis(x, y, W, H)
    sx, sy = x + B @ cx_, y + B @ cy_
    h, w, _ = img.shape
    valid = (sx >= 0) & (sx <= w - 2) & (sy >= 0) & (sy <= h - 2)
    x0 = np.clip(sx.astype(int), 0, w - 2); y0 = np.clip(sy.astype(int), 0, h - 2)
    fx, fy = (sx - x0)[..., None], (sy - y0)[..., None]
    out = (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x0 + 1] * fx * (1 - fy)
           + img[y0 + 1, x0] * (1 - fx) * fy + img[y0 + 1, x0 + 1] * fx * fy)
    return out, valid


def main():
    a_path, ed_path = sys.argv[1], sys.argv[2]
    img, desc = X.load(a_path)
    ed = tifffile.imread(ed_path)[..., :3].astype(np.float64) / 65535
    best = None
    for sign in (1, -1):                       # phase() sign convention: try both
        cx_, cy_, n, res = fit_poly(img, ed)
        cx_, cy_ = sign * cx_, sign * cy_
        w, valid = warp(img, cx_, cy_, ed.shape[:2])
        m = R.box(valid[..., None].astype(float), 8)[..., 0] > 0.999
        d = (R.box(w, 8) - R.box(ed, 8)) * 255
        mae = float(np.abs(d[m]).mean())
        if best is None or mae < best[0]:
            best = (mae, d, m, w, n, res)
    mae, d, m, w, n, res = best
    Ys = R.box(ed, 8) @ [0.2126, 0.7152, 0.0722] * 255
    tone = {}
    for lo, hi in ((5, 20), (20, 50), (50, 90), (90, 130), (130, 170), (170, 210), (210, 250)):
        k = m & (Ys >= lo) & (Ys < hi)
        tone[f"{lo}-{hi}"] = dict(n=int(k.sum()), bias=d[k].mean(0).round(2).tolist())
    rep = dict(acr=a_path, edit=ed_path, icc=desc, points=n, fit_residual_px=round(res, 2), mae_box8=round(mae, 3),
               bias=d[m].mean(0).round(3).tolist(), p95=round(float(np.percentile(np.abs(d[m]), 95)), 3), tone=tone)
    print(json.dumps(rep))
    out = Path(a_path).with_name(Path(a_path).stem + "_vs_edit.json")
    json.dump(rep, open(out, "w"), indent=1)
    from PIL import Image
    c = lambda x: np.clip(x, 0, 255).astype(np.uint8)
    e8, w8 = R.box(ed, 8) * 255, R.box(w, 8) * 255
    Image.fromarray(np.concatenate([c(e8), c(w8), c(np.abs(w8 - e8) * 4)], 1)).save(out.with_suffix(".png"))


if __name__ == "__main__":
    main()
