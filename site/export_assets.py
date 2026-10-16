"""Assets for the scroll story (index.html): rendered stages, raw mosaic views and real raw values.

usage: python3 site/export_assets.py DSC05651.ARW   (the ARW is only read; it is not in the repo)
"""
import json, sys
from pathlib import Path
import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "sonylook"))
import sony_render as R, sony_frontend as F, arw_looks as AL
import rawpy

ARW = sys.argv[1] if len(sys.argv) > 1 else "DSC05651.ARW"
OUT = Path(__file__).parent / "assets"
OUT.mkdir(parents=True, exist_ok=True)

def save(a, name, w=None, q=84):
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    if w:
        im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
    im.save(OUT / name, "JPEG", quality=q, optimize=True, progressive=True)
    return im.size

def srgb(v):
    v = np.clip(v, 0, 1)
    return np.where(v <= 0.0031308, 12.92 * v, 1.055 * v ** (1 / 2.4) - 0.055) * 255

main = AL.main_tags(ARW)
black = R.ints(main["0x7310"])
with rawpy.imread(ARW) as r:
    raw = r.raw_image_visible[R.CROP["top"]:R.CROP["top"] + R.CROP["height"], R.CROP["left"]:R.CROP["left"] + R.CROP["width"]].astype(np.int64)

S = 4
lin = R.front_end_sony(ARW, S, lens=False)
looks = R.load_looks(ARW)
final = R.Look("ST", looks).render(lin) / 64.0
sizes = {}
sizes["final"] = save(final, "final.jpg", 2000)

# raw as the sensor sees it: one sample per site, coloured by its filter
H, W = raw.shape
yy, xx = np.mgrid[0:H, 0:W]
ch = np.where(yy % 2 == 0, np.where(xx % 2 == 0, 0, 1), np.where(xx % 2 == 0, 1, 2))
k = np.sqrt(np.clip(raw - 512, 0, None) / (16383 - 512))
tint = np.array([[1.0, 0.25, 0.2], [0.3, 1.0, 0.35], [0.25, 0.4, 1.0]])
def bayer_img(y0, x0, h, w):
    kk = k[y0:y0 + h, x0:x0 + w]; cc = ch[y0:y0 + h, x0:x0 + w]
    return kk[..., None] * tint[cc] * 255
# 1:1 crop of lit windows, nearest-upscaled so the mosaic stays crisp
cy, cx = int(0.40 * H) // 2 * 2, int(0.385 * W) // 2 * 2
crop = bayer_img(cy - 128, cx - 192, 256, 384)
Image.fromarray(crop.clip(0, 255).astype(np.uint8)).resize((384 * 4, 256 * 4), Image.NEAREST).save(OUT / "raw_bayer.png", optimize=True)
# small real raw blocks for the 3D pixel wells and the readout grid
wells = raw[cy:cy + 8, cx:cx + 8].tolist()
grid = raw[cy - 8:cy + 24, cx - 8:cx + 24].tolist()
data = dict(sizes=sizes, wells=wells, grid=grid, origin=[int(cx), int(cy)], black=black[0], white=16383,
            frame=[int(W), int(H)], crop_frac=[(cx - 192) / W, (cy - 128) / H, 384 / W, 256 / H])
json.dump(data, open(OUT / "raw.json", "w"))
print(sizes, np.array(wells).min(), np.array(wells).max())
