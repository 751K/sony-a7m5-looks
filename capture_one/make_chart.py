#!/usr/bin/env python3
"""Write a synthetic colour-chart ARW for calibrating Capture One against Sony's pipeline.

Copies a source A7M5 ARW (the original is only read; lossless-compressed, 7168x5120 tiles) and replaces every 512x512 raw tile with an
LJ92 stream (lj92.py; the encoder reproduces Sony's own tiles byte for byte) holding flat patches.
New tiles are written at the original offsets; they are smaller, the rest of each slot is zero-padded,
so no TIFF tag changes. Metadata (WB, black, lens, SR2) stays that of the source file.
The three embedded JPEGs (160x120 thumbnail, 1616x1080 preview, 7008x4672 full size) are replaced the
same way (same offset, zero padded) by a rendering of the chart: the Sony ST look of each patch when
Imaging Edge Edit is installed (sonylook), else a plain sRGB rendering of the camera RGB.
usage: make_chart.py SOURCE.ARW   -> ../chart/SonyChart.ARW, SonyChart.json

Patch values are set in Sony's white-balanced linear scale (ITP: lin_c = k_c (raw - 512),
k = 0.0625 * 256/32768 * (2735, 1056, 1056, 1460)), so the linear input of every patch is known exactly.
Layout: 18 x 15 cells over the 7008 x 4672 crop at (12, 8); a 48-step grey ramp then a 6x6x6 cube.
"""
import json
import sys
from pathlib import Path

import numpy as np
import tifffile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lj92  # noqa: E402

OUT = HERE.parent / "chart" / "SonyChart.ARW"
BLACK, WHITE = 512, 15360
K = 0.0625 * 256 / 32768 * np.array([2735, 1056, 1056, 1460], np.float64)     # R G G B
CROP = (12, 8, 7008, 4672)
COLS, ROWS, BORDER = 18, 15, 36
CLIP_LIN = 0.515625 * (16383 - BLACK)                                      # Sony linear at sensor clip
BG = 300.0                                                                  # border grey, lin units


def patches():
    grey = np.exp(np.linspace(np.log(1.0), np.log(7600.0), 48))
    out = [(g, g, g) for g in grey]
    lv = [10.0, 45.0, 180.0, 650.0, 2100.0, 6200.0]
    out += [(r, g, b) for r in lv for g in lv for b in lv]
    return out


def lin_to_raw(lin_rgb):
    r, g, b = lin_rgb
    vals = [r / K[0], g / K[1], g / K[2], b / K[3]]
    return [int(np.clip(round(BLACK + v), BLACK, WHITE)) for v in vals]


def chart_image(spec, src):
    """Full-size (crop) sRGB 8-bit rendering of the chart from the patch values."""
    lin = np.array([[BG] * 3] + [p["lin_sony"] for p in spec], np.float64)
    try:
        sys.path.insert(0, str(HERE.parent / "sonylook"))
        import sony_render as R
        looks = R.load_looks(str(src))
        enc = R.Look("Standard", looks).render(np.clip(lin, 0, 16383).reshape(1, -1, 3).astype(np.float32), pointwise=True)
        col = np.round(enc.reshape(-1, 3) / 16383 * 255)
        how = "Sony ST look"
    except Exception as e:                      # no Edit install: plain gamma rendering of camera RGB
        x = np.clip(lin / CLIP_LIN, 0, 1)
        col = np.round(np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1 / 2.4) - 0.055) * 255)
        how = f"plain sRGB ({type(e).__name__})"
    img = np.empty((CROP[3], CROP[2], 3), np.uint8)
    img[:] = col[0]
    for p, c in zip(spec, col[1:]):
        x0, y0, x1, y1 = p["box_crop"]
        img[y0:y1, x0:x1] = c
    return img, how


def replace_jpegs(data, img):
    """Overwrite every embedded JPEG (IFD0 preview, IFD1 thumbnail, IFD2 full size) in place."""
    import io
    from PIL import Image
    full = Image.fromarray(img)
    with tifffile.TiffFile(io.BytesIO(bytes(data))) as t:
        slots = []
        for p in t.pages:
            o, n = p.tags.get("JPEGInterchangeFormat"), p.tags.get("JPEGInterchangeFormatLength")
            if o and n:
                slots.append((int(np.ravel(o.value)[0]), int(np.ravel(n.value)[0]), p.imagewidth, p.imagelength))
    for o, n, w, h in slots:
        if w * 3 == h * 4 and w < 400:          # 4:3 thumbnail: letterboxed like the camera's
            th = round(w * CROP[3] / CROP[2])
            im = Image.new("RGB", (w, h)); im.paste(full.resize((w, th), Image.LANCZOS), (0, (h - th) // 2))
        else:
            im = full.resize((w, h), Image.LANCZOS) if (w, h) != full.size else full
        for q in (95, 90, 85, 80, 70, 60, 50, 40):
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=q, optimize=True); enc = buf.getvalue()
            if len(enc) <= n:
                break
        assert len(enc) <= n, (w, h, len(enc), n)
        data[o:o + n] = enc + bytes(n - len(enc))
    return [(w, h) for _, _, w, h in slots]


def main():
    SRC = Path(sys.argv[1])
    P = patches()
    assert len(P) <= COLS * ROWS
    H, W = 5120, 7168
    raw = np.full((H, W), BLACK, np.int64)
    x0, y0, cw, ch = CROP
    cell_w, cell_h = (cw // COLS) & ~1, (ch // ROWS) & ~1
    bgq = lin_to_raw((BG, BG, BG))
    quad = lambda q: np.array([[q[0], q[1]], [q[2], q[3]]])
    raw[y0:y0 + ch, x0:x0 + cw] = np.tile(quad(bgq), (ch // 2, cw // 2))
    spec = []
    for i, lin in enumerate(P):
        r_, c_ = divmod(i, COLS)
        px0 = x0 + c_ * cell_w + BORDER; py0 = y0 + r_ * cell_h + BORDER
        pw, ph = (cell_w - 2 * BORDER) & ~1, (cell_h - 2 * BORDER) & ~1
        q = lin_to_raw(lin)
        raw[py0:py0 + ph, px0:px0 + pw] = np.tile(quad(q), (ph // 2, pw // 2))
        exact = [K[0] * (q[0] - BLACK), K[1] * (q[1] - BLACK), K[3] * (q[3] - BLACK)]   # Sony lin (G from G1)
        spec.append(dict(i=i, lin_target=list(lin), raw_rggb=q, lin_sony=exact,
                         box_crop=[px0 - x0, py0 - y0, px0 - x0 + pw, py0 - y0 + ph]))
    data = bytearray(SRC.read_bytes())
    with tifffile.TiffFile(SRC) as t:
        sub = t.pages[0].pages[0]
        offs, cnts = sub.dataoffsets, sub.databytecounts
        tw, tl = sub.tilewidth, sub.tilelength
    ntx = W // tw
    for ti, (o, n) in enumerate(zip(offs, cnts)):
        ty, tx = divmod(ti, ntx)
        tile = raw[ty * tl:(ty + 1) * tl, tx * tw:(tx + 1) * tw]
        s = np.stack([tile[k // 2::2, k % 2::2] for k in range(4)], -1)
        old = bytes(data[o:o + n])
        enc = lj92.encode(s, old[:lj92.parse(old)["scan"]])
        assert len(enc) <= n, (ti, len(enc), n)
        data[o:o + n] = enc + bytes(n - len(enc))
    img, how = chart_image(spec, SRC)
    sizes = replace_jpegs(data, img)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(bytes(data))
    from PIL import Image
    Image.fromarray(img).resize((CROP[2] // 4, CROP[3] // 4), Image.LANCZOS).save(OUT.with_name("SonyChart_preview.png"))
    json.dump(dict(source=SRC.name, K=K.tolist(), black=BLACK, crop=CROP, cells=[COLS, ROWS], border=BORDER,
                   bg_lin=BG, patches=spec), open(OUT.with_suffix(".json"), "w"), indent=1)
    print(OUT, len(data), "patches", len(spec), "| embedded JPEGs", sizes, "rendered with", how)


if __name__ == "__main__":
    main()
