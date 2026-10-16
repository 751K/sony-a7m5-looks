"""Per-stage data for the darkroom: the Standard look split into its steps (ARW is only read).

luts/ST_mat.bin   LinearMatrix16 only, shown with a plain sRGB encode (same display as the WB step)
luts/ST_tone.bin  LinearMatrix16 + MainGamma tone curve, no YCC stages
stages.json       the MainGamma curve and the colour matrix the table uses for neutral greys
"""
import json, sys
from pathlib import Path
import numpy as np

REPO = Path(__file__).resolve().parents[1] if (Path(__file__).resolve().parents[1] / "sonylook").exists() else Path("/Volumes/MacoutDsik/Code/sony-a7m5-looks")
sys.path.insert(0, str(REPO / "sonylook"))
import sony_render as R, sony_look as SL

ARW = sys.argv[1]
OUT = Path(sys.argv[2]); (OUT / "luts").mkdir(parents=True, exist_ok=True)
G, CLIP = 33, 8183.6

def srgb(v):
    v = np.clip(v, 0, 1)
    return np.where(v <= 0.0031308, 12.92 * v, 1.055 * v ** (1 / 2.4) - 0.055)

looks = R.load_looks(ARW)
look = R.Look("ST", looks)
atan, _ = SL.load_static_tables()
u = np.linspace(0, 1, G)
lin = 16383 * u ** 2
grid = np.stack(np.meshgrid(lin, lin, lin, indexing="ij"), -1).reshape(-1, 3)   # R slowest, as the other LUTs
planes = [np.ascontiguousarray(grid[:, c], np.float32) for c in range(3)]
hue = SL.hue_index(planes, planes, atan, 0)
mixed = SL.apply_lm16(planes, hue, look.table, (0.0, 0.0, 0.0), True)
mat = np.stack(mixed, -1)
np.clip(np.round(srgb(mat / CLIP) * 255), 0, 255).astype(np.uint8).tofile(OUT / "luts" / "ST_mat.bin")
tone = np.stack([SL.apply_main_gamma(p, look.gamma) for p in mixed], -1).astype(np.float64)
np.clip(np.round(tone / 64), 0, 255).astype(np.uint8).tofile(OUT / "luts" / "ST_tone.bin")

# the tone curve: 14-bit linear in, 14-bit encoded out
xs = np.round(16383 * np.linspace(0, 1, 129) ** 2).astype(int)
curve = [[int(x), int(look.gamma[0x8000 + x])] for x in xs]
# the 3x3 part of the colour table for a neutral grey (the table holds 1024 of these, one per hue)
gp = [np.full(1, 2000, np.float32)] * 3
gi = int(np.trunc(SL.hue_index(gp, gp, atan, 0))[0])
B = np.asarray(look.table, np.float64)[gi, :9].reshape(3, 3)
json.dump(dict(curve=curve, matrix=B.round(3).tolist(), hue_bins=int(np.asarray(look.table).shape[0])), open(OUT / "stages.json", "w"))
print("grey hue bin", gi, "\n", B.round(3), "\nrow sums", B.sum(1).round(3))
print("curve mid: 18% grey", int(look.gamma[0x8000 + int(0.18 * CLIP)]), "of 16383")
