#!/usr/bin/env python3
"""One figure: Edit / Capture One / Camera Raw renders of the same look, residuals vs Edit, 100% crops.
Both exports are warped into Edit's geometry (cubic polynomial, compare_acr_export.fit_poly).
usage: compare_figure.py EDIT.tif C1.tif ACR.tif OUT.jpg [label]
"""
import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare_acr_export as C  # noqa: E402
import compare_c1_export as X  # noqa: E402
import sony_render as R  # noqa: E402


def register(path, ed):
    img, _ = X.load(path)
    cx, cy, n, res = C.fit_poly(img, ed)
    best = None
    for s in (1, -1):
        w, valid = C.warp(img, s * cx, s * cy, ed.shape[:2])
        mae = np.abs(R.box(w, 8) - R.box(ed, 8))[R.box(valid[..., None].astype(float), 8)[..., 0] > 0.999].mean()
        if best is None or mae < best[0]:
            best = (mae, w, valid)
    return best[1], best[2], res


def font(sz):
    for f in ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"):
        try:
            return ImageFont.truetype(f, sz)
        except OSError:
            pass
    return ImageFont.load_default()


def main():
    ed_p, c1_p, acr_p, out = sys.argv[1:5]
    label = sys.argv[5] if len(sys.argv) > 5 else ""
    ed = tifffile.imread(ed_p)[..., :3].astype(np.float64) / 65535
    c1, v1, r1 = register(c1_p, ed)
    acr, v2, r2 = register(acr_p, ed)
    valid = v1 & v2
    s = 4
    to8 = lambda x: (np.clip(x, 0, 1) * 255 + 0.5).astype(np.uint8)
    E, Cc, A = (R.box(x, s) for x in (ed, c1, acr))
    m = R.box(valid[..., None].astype(float), s)[..., 0] > 0.999
    shown = lambda x: to8(np.where(m[..., None], x, 0.1))       # grey outside the area both exports cover
    def resid(x):                       # |difference| in 8-bit units, x8 gain, black where invalid
        d = np.abs(x - E).max(-1) * 255 * 8
        d[~m] = 0
        return np.repeat(np.clip(d, 0, 255)[..., None], 3, -1).astype(np.uint8)
    mae = lambda x: float((np.abs(x - E) * 255)[m].mean())
    h, w = E.shape[:2]
    # 100% crop: most textured 600x400 window inside the valid area
    g = np.hypot(*np.gradient(R.box(ed, 8).mean(-1)))
    gm = R.box(np.where(R.box(valid[..., None].astype(float), 8)[..., 0] > 0.999, g, 0)[..., None], 1)[..., 0]
    cw, ch = w, int(w * 0.5)
    best, by, bx = -1, 0, 0
    for yy in range(0, gm.shape[0] - ch // 8, 5):
        for xx in range(0, gm.shape[1] - cw // 8, 5):
            v = gm[yy:yy + ch // 8, xx:xx + cw // 8].mean()
            if v > best:
                best, by, bx = v, yy * 8, xx * 8
    crop = lambda x: to8(x[by:by + ch, bx:bx + cw])
    pad, top = 24, 90
    F, Fs = font(64), font(46)
    W = 3 * w + 4 * pad
    rows_h = [h, h, ch]
    H = sum(rows_h) + 3 * top + 4 * pad
    canvas = Image.new("RGB", (W, H), (24, 24, 24))
    dr = ImageDraw.Draw(canvas)
    y = pad
    cells = [[(shown(E), "Edit（Sony 官方）"), (shown(Cc), f"Capture One   MAE {mae(Cc):.2f}"), (shown(A), f"Camera Raw   MAE {mae(A):.2f}")],
             [(None, ""), (resid(Cc), "C1 与 Edit 的残差 ×8"), (resid(A), "ACR 与 Edit 的残差 ×8")],
             [(crop(ed), "Edit 100% 裁切"), (crop(c1), "C1 100% 裁切"), (crop(acr), "ACR 100% 裁切")]]
    for r, row in enumerate(cells):
        x = pad
        for img, txt in row:
            dr.text((x, y + 6), txt, fill=(235, 235, 235), font=F)
            if img is not None:
                canvas.paste(Image.fromarray(img), (x, y + top))
            elif r == 1:
                lines = [label, f"上两行缩小 {s} 倍，MAE 为 8 位平均误差", "残差 = 三通道最大差 ×8", "越亮差异越大，32 级即全白", f"配准残差：C1 {r1:.1f}px，ACR {r2:.1f}px", "黑框 = 第三行 100% 裁切的位置", "灰色 = 两张导出未共同覆盖的区域"]
                for k, t in enumerate(lines):
                    dr.text((x, y + top + 20 + 70 * k), t, fill=(200, 200, 200), font=Fs)
            if r == 0:
                dr.rectangle([x + bx // s, y + top + by // s, x + (bx + cw) // s, y + top + (by + ch) // s], outline=(0, 0, 0), width=6)
            x += (img.shape[1] if img is not None else w) + pad
        y += rows_h[r] + top + pad
    canvas.save(out, quality=92)
    print(out, canvas.size, "MAE c1", round(mae(Cc), 3), "acr", round(mae(A), 3), "crop at", bx, by)


if __name__ == "__main__":
    main()
