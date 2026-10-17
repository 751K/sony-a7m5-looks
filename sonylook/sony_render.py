#!/usr/bin/env python3
"""End-to-end reference renderer: Sony front end + bit-verified Sony look chain.

    RAW --(raw-0x7310 black)*0x7311*WB_RGGBLevels*(0x7809/256)/2^19 --ITP--> --GeoCorr(0x7984/0x7980)--> (front_end_sony)--> camera RGB (int16 scale)
        --LinearMatrix16(0x780f, works directly on WB'd camera RGB)--> --MainGamma(0x7805/0x7806)-->
        --RGB2YCC(0x7842+0x7844, 0x7801)--> --AreaComp--> --ChromaSuppress(0x787e-0x7881)-->
        --YGamma(0x780c/d knee, 0x780e gain)--> --YCC2RGB--> [Effect: BW / Sepia only] --> 14-bit encoded RGB

All look parameters come from the ARW itself (arw_looks.py); constant tables from the user's own
Imaging Edge Edit install (edit_binary.py).
Not Sony: the demosaic interpolation (CFA samples are block-averaged instead), and SSCS (near-clip
highlights), YNR and Marble (low-frequency chroma NR), which are not ported.

usage: sony_render.py ARW LOOK [LOOK ...] [--scale N] [--out DIR]
"""
import argparse
import base64
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arw_looks as AL          # noqa: E402
import sony_look as SL          # noqa: E402
import sony_ycc as SY           # noqa: E402
import sony_ycc_stages as SS    # noqa: E402
import build_color_table as BT  # noqa: E402
import decode_780f as D         # noqa: E402
import sony_effect as EF        # noqa: E402

CROP = dict(left=12, top=8, width=7008, height=4672)     # Sony default crop (ExifTool)
MAIN_TAGS = ("0x7841", "0x7847", "0x7848", "0x780b", "0x787e", "0x787f", "0x7880", "0x7881", "0x74c5")


LOOK_IDS = {"Standard": 2, "ST": 2, "Vivid": 4, "Neutral": 7, "Portrait": 8, "BW": 0x10, "Sepia": 0x11,
            "FL": 0x12, "VV2": 0x13, "IN": 0x14, "SH": 0x15, "FL2": 0x16, "FL3": 0x17}   # table 0x1002c31e8


def ints(s):
    return [int(v) for v in str(s).split()]


def load_looks(arw):
    """{name: look tags} from the ARW; '_main' holds the main SR2SubIFD values looks do not override."""
    out = dict(AL.look_ifds(arw))
    out["ST"] = out["Standard"]
    main = AL.main_tags(arw)
    out["_main"] = {t: main[t] for t in MAIN_TAGS}
    out["_main"]["SonyModelID"] = AL.sony_model_id(arw)
    return out


def front_end_sony(arw, scale=8, lens=True, dro=False):
    """Sony black level / WB / 0x7311 scaling (sony_frontend.py), CFA block-averaged by `scale`.

    Output is white-balanced camera RGB on Sony's int16 scale: no exposure fit, no matrix."""
    import rawpy
    import sony_frontend as F
    main = AL.main_tags(arw)
    black = ints(main["0x7310"])                                        # BlackLevel
    gains = F.wb_gains(ints(main["0x7313"]), int(main["0x7809"]))       # WB_RGGBLevels
    s7311 = int(main["0x7311"])
    with rawpy.imread(str(arw)) as r:
        assert r.raw_pattern.tolist() == [[0, 1], [3, 2]] and r.color_desc == b"RGBG"
        raw = r.raw_image_visible[CROP["top"]:CROP["top"] + CROP["height"], CROP["left"]:CROP["left"] + CROP["width"]]
        cfa = F.itp_scale(raw, black, gains, s7311)
    # ITP interpolates the signed float samples, then clamps to 0..16383 and truncates to u16.
    # Block means of the unclamped samples minus 0.5 (E[floor(v)] ~ v - 0.5) match emulated ITP
    # output within 0.3 % (run_itp.py); clamping the CFA samples first would over-brighten shadows.
    lin = F.block_average(cfa, scale) - 0.5
    if dro:    # ZcTaskVatr = DRO (dev+0x238 == 1): bilateral-grid local tone, camera curve 0x781b/0x781c
        import sony_dro as DRO
        lin, _ = DRO.vatr_downscaled(lin, DRO.grid_from_arw(arw), scale)
    if lens:   # ZcTaskGeoCorr: distortion + lateral CA (sony_lens.py, bit-verified per pixel at full res)
        import sony_lens as LN
        lin = LN.geocorr_downscaled(lin, LN.lens_from_arw(arw), scale) - 0.5
    return lin


def box(img, s):
    if s == 1:
        return img
    h, w = img.shape[0] // s * s, img.shape[1] // s * s
    return img[:h, :w].reshape(h // s, s, w // s, s, -1).mean(axis=(1, 3))


class Look:
    def __init__(self, name, looks):
        g = looks[name]
        self.name = name
        MAIN = self.main = looks["_main"]
        blob = base64.b64decode(g["0x780f"].removeprefix("base64:"))
        self.table = np.array(BT.build_table(D.decode_blob(blob), output_mode=0, param8=0, correction_matrix=None), np.float32)
        self.gamma = SL.build_main_gamma(ints(g["0x7805"]), ints(g["0x7806"]), 128)
        arrays = {t: ints(g[t]) for t in ("0x7842", "0x7843", "0x7844", "0x7845", "0x7846")}
        arrays.update({t: ints(MAIN[t]) for t in ("0x7841", "0x7847", "0x7848")})
        self.ycc_params = SY.select_params(arrays, 1)
        self.luma = ints(g.get("0x7801", "19 38 7"))
        knee, slope = int(g.get("0x780c", 0)), int(g.get("0x780d", 0))
        _, self.ylut = SS.build_ygamma([i << 2 for i in range(512)], False, 0, 0, 0, knee, slope, 0,
                                       [0] * 512, [0] * 512, 0, 0)
        t780e = ints(g.get("0x780e", "16384 " * 10))
        self.ygain = float(np.float32(np.float32(SS.ygamma_gain_raw(1, t780e, 0)) * np.float32(6.1035156e-05)))
        self.yoff = SS.ygamma_offset(1, ints(MAIN["0x780b"]), 0)
        # ChromaSuppress, exact (FUN_1000d50f8 -> FUN_1000e6da0): thresholds go through the inverse
        # tone tables render+0x89962 (inverse YGamma LUT) / +0x69962 (inverse MainGamma LUT), the
        # exposure factor render+0x18ee = exp2(EV/300)*64, then forward MainGamma + YGamma LUT
        # (no 0x780e gain). Exposure-compensation slider at 0 -> 0x18ee = 64.
        self.cs = self.chroma_params(0)
        # AreaComp: inserted for ARW format code 0x3600 (0xb000 = ARW 6.0); 0x7770 look id
        self.area_comp = SS.area_comp_params(0x3600, True, LOOK_IDS.get(name, 0))
        # Effect (BW / Sepia, sony_effect.py): Sepia tint from 0x74c5 unless SonyModelID is an old body
        self.look_id = LOOK_IDS.get(name, 0)
        self.tint = EF.tint(int(MAIN["SonyModelID"]), ints(MAIN["0x74c5"])) if self.look_id == EF.LOOK_SEPIA else None

    def chroma_params(self, render_exposure):
        r18ee = int(np.exp2(render_exposure * 0.0033333333333333335) * 64.0)
        MAIN = self.main
        g = ints(MAIN["0x787e"]) + ints(MAIN["0x787f"]) + ints(MAIN["0x7880"]) + ints(MAIN["0x7881"])
        look = dict(zip((0xf00, 0xf02, 0xf04, 0xf06, 0xf08, 0xf0a, 0xf0c, 0xf0e), g), fmt=0)
        return SS.chroma_suppress_params(look, render_exposure, r18ee,
                                         SS.inverse_lut_positive(self.ylut), SS.inverse_lut_centered(self.gamma),
                                         self.gamma[0x8000:], self.ylut)

    def render(self, lin, detail=None, pointwise=False):
        """detail: None, or dict(sharp=sony_detail.sharpness_params(...), spica=(table, params)) to run
        ZcTaskSharpness / ZcTaskSpica on the full array (only meaningful at full resolution)."""
        atan, _ = SL.load_static_tables()
        planes = [np.ascontiguousarray(lin[..., c], np.float32) for c in range(3)]
        # pointwise=True: LM16 hue index from the pixel itself instead of the 3-tap row blur
        # (identical on flat areas; needed when the input is a list of colours, e.g. a LUT grid)
        ctl = dict(blur_luma=0, blur_chroma=0, scale=0, quadratic=1, thresholds=(0.0, 0.0, 0.0)) if pointwise else None
        enc = SL.render_look(planes, self.table, self.gamma, atan, ctl)     # int16 x3 (LM16 + MainGamma)
        y, cr, cb = SY.rgb2ycc(*enc, self.ycc_params, self.luma, clamp=True)
        if self.area_comp is not None:                                     # ARW 6.0 (0x3600) + 0x780f
            cr, cb = SS.area_comp_apply(cr, cb, self.area_comp)
        y, cr, cb = SS.chroma_suppress_apply(y, cr, cb, self.cs)
        h, w = np.shape(y)
        if detail and detail.get("sharp") is not None:
            import sony_detail as SDT
            y = SDT.sharpness(np.asarray(y, np.uint16), (0, 0, w, h), detail["sharp"])
        y = SS.ygamma_apply(y, self.ylut, self.yoff, self.ygain)
        if detail and detail.get("spica") is not None:
            import sony_detail as SDT
            y = SDT.spica(np.asarray(y, np.uint16), (0, 0, w, h), *detail["spica"])
        r, g, b = SY.ycc2rgb(y, cr, cb)
        if self.look_id & ~1 == 0x10:      # render+0x44 = 0: sRGB output
            r, g, b = EF.effect_apply(*(np.asarray(c, np.uint16) for c in (r, g, b)), self.look_id, self.tint, mode=0)
        return np.stack([r, g, b], -1).astype(np.int32)                    # 0..16383 encoded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arw")
    ap.add_argument("looks", nargs="+")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--out", default="render_output")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(exist_ok=True)
    lin = front_end_sony(a.arw, a.scale)
    looks = load_looks(a.arw)
    from PIL import Image
    for name in a.looks:
        img = Look(name, looks).render(lin)
        Image.fromarray((img >> 6).clip(0, 255).astype(np.uint8)).save(out / f"{name}_s{a.scale}.png")
        print(name, img.shape, img.mean(axis=(0, 1)).round(1))


if __name__ == "__main__":
    main()
