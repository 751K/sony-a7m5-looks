#!/usr/bin/env python3
"""Compare a Capture One export (Adobe RGB or sRGB TIFF) with the Imaging Edge export of the same look.

Geometry differs (Edit applies lens distortion correction + 1.9% fill zoom), so an affine map is
fitted from gradient phase correlation on a 4x5 patch grid and the C1 image is resampled into
Edit's coordinates. Colours are compared in sRGB 8-bit units after an 8x box reduction.
usage: compare_c1_export.py C1.tif EDIT.tif   (report: <C1>_vs_edit.json/.png next to the export)
"""
import io
import json
import sys
from pathlib import Path

import numpy as np
import tifffile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sonylook"))
import sony_render as R  # noqa: E402

A98 = np.array([[0.5767309, 0.1855540, 0.1881852], [0.2973769, 0.6273491, 0.0752741], [0.0270343, 0.0706872, 0.9911085]])
SRGB = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])


def to_srgb(a, icc_desc):
    a = np.clip(a, 0, 1)
    if "Adobe RGB" in icc_desc:
        lin = np.clip((a ** (563 / 256)) @ (np.linalg.inv(SRGB) @ A98).T, 0, 1)
        return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    if "ProPhoto" in icc_desc or "ROMM" in icc_desc:   # Camera Raw / DxO exports (D50, gamma 1.8)
        import c1_look_profile as C
        pp = np.array([[0.7976749, 0.1351917, 0.0313534], [0.2880402, 0.7118741, 0.0000857], [0.0, 0.0, 0.8252100]])
        m = np.linalg.inv(np.linalg.inv(pp) @ C.CHAD @ C.M_SRGB)
        lin = np.clip((a ** 1.8) @ m.T, 0, 1)
        return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return a


def load(path):
    from PIL import ImageCms
    with tifffile.TiffFile(path) as t:
        p = t.pages[0]
        a = p.asarray()[..., :3].astype(np.float64) / (65535 if p.dtype == np.uint16 else 255)
        icc = p.tags.get(34675)
        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc.value))) if icc else "sRGB"
    return to_srgb(a, desc), desc


def phase(p, q):
    win = np.outer(np.hanning(p.shape[0]), np.hanning(p.shape[1]))
    P = np.fft.fft2((p - p.mean()) * win); Q = np.fft.fft2((q - q.mean()) * win)
    X = P * np.conj(Q); r = np.fft.ifft2(X / (np.abs(X) + 1e-9)).real
    y, x = np.unravel_index(r.argmax(), r.shape); h, w = r.shape
    return (x if x < w // 2 else x - w), (y if y < h // 2 else y - h), r.max()


def fit_affine(c1, ed, s=2, N=384):
    g = lambda x: np.hypot(*np.gradient(np.log1p(R.box(x, s).mean(-1) * 255)))
    A, S = g(c1), g(ed)
    H, W = A.shape
    rows = []
    for cy in np.linspace(N, H - N, 5).astype(int):
        for cx in np.linspace(N, W - N, 6).astype(int):
            dx, dy, q = phase(A[cy - N // 2:cy + N // 2, cx - N // 2:cx + N // 2], S[cy - N // 2:cy + N // 2, cx - N // 2:cx + N // 2])
            rows.append((cx * s, cy * s, dx * s, dy * s, q))
    r = np.array(rows, float)
    keep = r[:, 4] > 0.03
    for _ in range(3):                   # robust: drop points far from the fit
        X = np.c_[r[keep, 0], r[keep, 1], np.ones(keep.sum())]
        cx_, *_ = np.linalg.lstsq(X, r[keep, 2], rcond=None); cy_, *_ = np.linalg.lstsq(X, r[keep, 3], rcond=None)
        res = np.hypot(np.c_[r[:, 0], r[:, 1], np.ones(len(r))] @ cx_ - r[:, 2], np.c_[r[:, 0], r[:, 1], np.ones(len(r))] @ cy_ - r[:, 3])
        keep = keep & (res < 8)
    return cx_, cy_, int(keep.sum()), float(np.median(res[keep]))


def warp(img, cx_, cy_, sign):
    """Sample img at p + sign*d(p) (bilinear) for every Edit pixel p."""
    H, W, _ = img.shape
    y, x = np.mgrid[0:H, 0:W].astype(np.float64)
    sx = x + sign * (cx_[0] * x + cx_[1] * y + cx_[2]); sy = y + sign * (cy_[0] * x + cy_[1] * y + cy_[2])
    x0 = np.clip(np.floor(sx).astype(int), 0, W - 2); y0 = np.clip(np.floor(sy).astype(int), 0, H - 2)
    fx = np.clip(sx - x0, 0, 1)[..., None]; fy = np.clip(sy - y0, 0, 1)[..., None]
    out = (img[y0, x0] * (1 - fx) + img[y0, x0 + 1] * fx) * (1 - fy) + (img[y0 + 1, x0] * (1 - fx) + img[y0 + 1, x0 + 1] * fx) * fy
    valid = (sx >= 0) & (sy >= 0) & (sx <= W - 1) & (sy <= H - 1)
    return out, valid


def main():
    c1_path = sys.argv[1]
    ed_path = sys.argv[2]
    c1, desc = load(c1_path)
    ed = tifffile.imread(ed_path)[..., :3].astype(np.float64) / 65535
    cx_, cy_, n, res = fit_affine(c1, ed)
    best = None
    for sign in (1, -1):
        w, valid = warp(c1, cx_, cy_, sign)
        m = R.box(valid[..., None].astype(float), 8)[..., 0] > 0.999
        d = (R.box(w, 8) - R.box(ed, 8)) * 255
        mae = float(np.abs(d[m]).mean())
        if best is None or mae < best[0]:
            best = (mae, sign, d, m, w)
    mae, sign, d, m, w = best
    Ys = R.box(ed, 8) @ [0.2126, 0.7152, 0.0722] * 255
    tone = {}
    for lo, hi in ((5, 20), (20, 50), (50, 90), (90, 130), (130, 170), (170, 210), (210, 250)):
        k = m & (Ys >= lo) & (Ys < hi)
        tone[f"{lo}-{hi}"] = dict(n=int(k.sum()), bias=d[k].mean(0).round(2).tolist())
    rep = dict(c1=c1_path, edit=ed_path, c1_icc=desc, affine_x=cx_.tolist(), affine_y=cy_.tolist(), points=n,
               fit_residual_px=round(res, 2), sign=sign, mae_box8=round(mae, 3),
               bias=d[m].mean(0).round(3).tolist(), p95=round(float(np.percentile(np.abs(d[m]), 95)), 3), tone=tone)
    print(json.dumps(rep, indent=1))
    out = Path(c1_path).with_name(Path(c1_path).stem.replace(" ", "_") + "_vs_edit.json")
    json.dump(rep, open(out, "w"), indent=1)
    from PIL import Image
    c = lambda x: np.clip(x, 0, 255).astype(np.uint8)
    e8, w8 = R.box(ed, 8) * 255, R.box(w, 8) * 255
    Image.fromarray(np.concatenate([c(e8), c(w8), c(np.abs(w8 - e8) * 4)], 1)).save(out.with_suffix(".png"))


if __name__ == "__main__":
    main()
