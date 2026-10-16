"""Darkroom assets for index.html: raw image (sqrt-encoded WebP), per-look 3D LUTs, demosaic crop.

usage: python3 site/export_darkroom.py DSC05651.ARW site/assets   (the ARW is only read)
"""
import json, sys
from pathlib import Path
import numpy as np
from PIL import Image
from scipy.interpolate import RegularGridInterpolator

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "sonylook"))
import sony_render as R, sony_frontend as F, arw_looks as AL
import rawpy

ARW = sys.argv[1]
OUT = Path(sys.argv[2]); (OUT / "luts").mkdir(parents=True, exist_ok=True)
S, G = 8, 33
CLIP = 8183.6

main = AL.main_tags(ARW)
gains = F.wb_gains(R.ints(main["0x7313"]), int(main["0x7809"]))
with rawpy.imread(ARW) as r:
    vis = r.raw_image_visible
    raw = vis[R.CROP["top"]:R.CROP["top"] + R.CROP["height"], R.CROP["left"]:R.CROP["left"] + R.CROP["width"]].astype(np.float64)
    lin16 = r.postprocess(use_camera_wb=True, no_auto_bright=True, gamma=(1, 1), output_bps=16, output_color=rawpy.ColorSpace.raw, user_flip=0)

# 1) raw image for the darkroom: 2x2 CFA -> RGB, area-resized to 2560 px wide, black level still in.
#    Stored as 8-bit sqrt(max(raw - 500, 0) / 15883) in a lossless WebP; the page decodes raw = 500 + e^2 * 15883.
WIDTH = 2560
half = F.block_average(raw, 2)
hh = round(half.shape[0] * WIDTH / half.shape[1])
big = np.stack([np.asarray(Image.fromarray(half[..., c].astype(np.float32), "F").resize((WIDTH, hh), Image.BOX)) for c in range(3)], -1)
enc = np.round(255 * np.sqrt(np.clip((big - 500) / 15883, 0, 1))).astype(np.uint8)
Image.fromarray(enc).save(OUT / "raw.webp", "WEBP", lossless=True, quality=100, method=6)
h, w = enc.shape[:2]
blk = F.block_average(raw, S)

# 2) look LUTs on a sqrt-shaped grid of Sony-linear input
u = np.linspace(0, 1, G)
lin_axis = 16383 * u ** 2
grid = np.stack(np.meshgrid(lin_axis, lin_axis, lin_axis, indexing="ij"), -1).reshape(1, -1, 3)   # R slowest
looks = R.load_looks(ARW)
names = [n for n in looks if n not in ("_main", "Standard")]
for n in names:
    out = R.Look(n, looks).render(grid, pointwise=True)[0] / 64.0
    np.clip(np.round(out), 0, 255).astype(np.uint8).tofile(OUT / "luts" / f"{n}.bin")

# check: client-side path (normalise, as-shot WB, LUT) vs the reference renderer
wb = np.array([gains[0], gains[1], gains[3]]) / gains[1]
n = (blk - 512) / (16383 - 512) * wb
uu = np.sqrt(np.clip(n * CLIP / 16383, 0, 1))
lut = np.fromfile(OUT / "luts" / "ST.bin", np.uint8).reshape(G, G, G, 3).astype(np.float64)
pts = uu.reshape(-1, 3)
pred = np.stack([RegularGridInterpolator((u, u, u), lut[..., c])(pts) for c in range(3)], -1).reshape(*blk.shape[:2], 3)
ref = R.Look("ST", looks).render(R.front_end_sony(ARW, S, lens=False)) / 64.0
print("LUT path vs renderer, 8-bit MAE:", round(float(np.abs(pred - ref).mean()), 3))

# 3) demosaiced version of the mosaic crop (same window as raw_bayer.png, same sqrt display)
H, W = raw.shape
cy, cx = int(0.40 * H) // 2 * 2, int(0.385 * W) // 2 * 2
y0, x0 = R.CROP["top"] + cy - 128, R.CROP["left"] + cx - 192
c = lin16[y0:y0 + 256, x0:x0 + 384].astype(np.float64) / 65535
c = np.sqrt(np.clip(c, 0, 1)) * 255
Image.fromarray(c.clip(0, 255).astype(np.uint8)).resize((384 * 4, 256 * 4), Image.NEAREST).save(OUT / "demosaic.png", optimize=True)

json.dump(dict(w=w, h=h, enc="sqrt8", enc_black=500, enc_range=15883, black=512, white=16383, clip=CLIP, grid=G, wb=wb.round(4).tolist(),
               looks=names), open(OUT / "darkroom.json", "w"))
print(w, h, names)
