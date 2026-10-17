#!/usr/bin/env python3
"""End-to-end reference renderer: Sony front end + bit-verified Sony look chain.

    RAW --(raw-0x7310 black)*0x7311*WB_RGGBLevels*(0x7809/256)/2^19 --ITP--> --GeoCorr(0x7984/0x7980)--> (front_end_sony)--> camera RGB (int16 scale)
        --LinearMatrix16(0x780f, works directly on WB'd camera RGB)--> --MainGamma(0x7805/0x7806)-->
        --RGB2YCC(0x7842+0x7844, 0x7801)--> --AreaComp--> --ChromaSuppress(0x787e-0x7881)-->
        --YGamma(0x780c/d knee, 0x780e gain)--> --YCC2RGB--> [Marble] --> [Effect: BW / Sepia only]
        --> 14-bit encoded RGB in the output space (sRGB, or AdobeRGB via LM16 output_mode 1)
    optional, both bit-exact: RawNR on the raw before ITP (sony_rawnr.py, setting 0x801e = 1) and
    Marble after YCC2RGB (sony_marble.py, 0x801e = 1 or 0x901c != 0); both are spatial, never in LUTs.

All look parameters come from the ARW itself (arw_looks.py); constant tables from the user's own
Imaging Edge Edit install (edit_binary.py).
Not Sony: the demosaic interpolation (CFA samples are block-averaged instead) and SSCS (near-clip
highlights). YNR and the YCC NR task FUN_1000b83e8 are not inserted for this camera (README 10.15).

usage: sony_render.py ARW LOOK [LOOK ...] [--scale N] [--out DIR]
"""
import argparse
import base64
import sys
import time
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
import sony_sscs as SC          # noqa: E402

CROP = dict(left=12, top=8, width=7008, height=4672)     # Sony default crop (ExifTool)
MAIN_TAGS = ("0x7841", "0x7847", "0x7848", "0x780b", "0x787e", "0x787f", "0x7880", "0x7881", "0x74c5")


LOOK_IDS = {"Standard": 2, "ST": 2, "Vivid": 4, "Neutral": 7, "Portrait": 8, "BW": 0x10, "Sepia": 0x11,
            "FL": 0x12, "VV2": 0x13, "IN": 0x14, "SH": 0x15, "FL2": 0x16, "FL3": 0x17}   # table 0x1002c31e8
# Camera default Fade per look (A7M5 Creative Look menu). Dev+0x2bc = 10 x Fade drives the YGamma offset
# (0x780b) and gain (0x780e). Edit's default table (0x1002e8214) agrees for IN / SH but has no FL2 / FL3
# rows, so Edit renders those two with Fade 0 while the camera uses 1.
CAMERA_FADE = {"IN": 3, "SH": 6, "FL2": 1, "FL3": 1}


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


def front_end_sony(arw, scale=8, lens=True, dro=None, wb_mode=None, temperature=None,
                   tint=None, exposure=None, rb_factor=None):
    """Sony black level / WB / 0x7311 scaling (sony_frontend.py), CFA block-averaged by `scale`.

    Output is white-balanced camera RGB on Sony's int16 scale: no exposure fit, no matrix.
    None means "read from the ARW's develop settings when Edit saved it, else default":
    wb_mode 0x8002 (1 as shot, 2 colour temperature), temperature 0x8013, tint 0x8014,
    exposure 0x8018, rb_factor 0x8015, dro 0x8022.  Explicit arguments override."""
    import rawpy
    import sony_frontend as F
    import develop_settings as DS
    st = DS.read_settings(arw)
    if wb_mode is None:
        wb_mode = int(st.get(DS.WB_MODE, 1))
    if temperature is None:
        temperature = st.get(DS.WB_TEMPERATURE, 6000)
    if tint is None:
        tint = int(st.get(DS.WB_TINT, 0))
    if exposure is None:
        exposure = int(st.get(DS.EXPOSURE, 0))
    if rb_factor is None:
        rb_factor = int(st.get(DS.RB_FACTOR, 0))
    if dro is None:
        dro = int(st.get(DS.DRO_ON, 0)) == 1
    main = AL.main_tags(arw)
    black = ints(main["0x7310"])                                        # BlackLevel
    levels = ints(main["0x7313"])                                       # WB_RGGBLevels
    k7809 = int(main["0x7809"])
    s7311 = int(main["0x7311"])
    with rawpy.imread(str(arw)) as r:
        assert r.raw_pattern.tolist() == [[0, 1], [3, 2]] and r.color_desc == b"RGBG"
        if wb_mode == 3:
            # picker / custom: the box coordinates are in full decoder (RAW) space
            sr, sb = F.wb_pick_state(arw, r.raw_image_visible, black, levels)
            gains = F.wb_gains(levels, k7809, tint, exposure, mode=3, state_r=sr, state_b=sb,
                               rb_factor=rb_factor)
        else:
            gains = F.wb_gains(levels, k7809, tint, exposure, mode=wb_mode, temperature=temperature,
                               temp_table=F.wb_temperature_table(main), presets=F.wb_presets(main),
                               rb_factor=rb_factor)
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
    def __init__(self, name, looks, fade=None, sscs=None, output_mode=0):
        """fade: Creative Look Fade slider 0..9; None = the camera default for this look.
        sscs: None, or sony_sscs params to apply ZcTaskSSCS in every render() call (decoder stage, not
        part of the look; the published profiles leave it out).
        output_mode: 0 = sRGB, 1 = AdobeRGB (render+0x44).  It selects the LinearMatrix16
        static branch: mode 1 / param8 0 is the sRGB -> AdobeRGB linear matrix, so the
        working space (param8 = 0, the look tables are authored in sRGB primaries) is
        unchanged and only the output primaries move.  BW/Sepia also need it: the
        Effect stage converts through the output space (sony_effect mode 0 / 1)."""
        if output_mode not in (0, 1):
            raise ValueError("output_mode must be 0 (sRGB) or 1 (AdobeRGB)")
        self.sscs = sscs
        self.output_mode = int(output_mode)
        g = looks[name]
        self.name = name
        MAIN = self.main = looks["_main"]
        blob = base64.b64decode(g["0x780f"].removeprefix("base64:"))
        self.table = np.array(BT.build_table(D.decode_blob(blob), output_mode=self.output_mode, param8=0, correction_matrix=None), np.float32)
        self.gamma = SL.build_main_gamma(ints(g["0x7805"]), ints(g["0x7806"]), 128)
        arrays = {t: ints(g[t]) for t in ("0x7842", "0x7843", "0x7844", "0x7845", "0x7846")}
        arrays.update({t: ints(MAIN[t]) for t in ("0x7841", "0x7847", "0x7848")})
        self.ycc_params = SY.select_params(arrays, 1)
        self.luma = ints(g.get("0x7801", "19 38 7"))
        knee, slope = int(g.get("0x780c", 0)), int(g.get("0x780d", 0))
        _, self.ylut = SS.build_ygamma([i << 2 for i in range(512)], False, 0, 0, 0, knee, slope, 0,
                                       [0] * 512, [0] * 512, 0, 0)
        self.fade = CAMERA_FADE.get({"Standard": "ST"}.get(name, name), 0) if fade is None else int(fade)
        t780e = ints(g.get("0x780e", "16384 " * 10))
        level = 10 * self.fade
        self.ygain = float(np.float32(np.float32(SS.ygamma_gain_raw(1, t780e, level)) * np.float32(6.1035156e-05)))
        self.yoff = SS.ygamma_offset(1, ints(MAIN["0x780b"]), level)
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

    def render(self, lin, detail=None, pointwise=False, marble=None, sscs=None,
               marble_inset=None, timings=None, strip=512):
        """sscs: None, or sony_sscs.params_from_tags(...) to run ZcTaskSSCS first (Edit always runs it, right
        after GeoCorrection). Integer input uses Sony's u16 path, float input its float32 path.
        detail: None, or dict(sharp=sony_detail.sharpness_params(...), spica=(table, params)) to run
        ZcTaskSharpness / ZcTaskSpica on the full array (only meaningful at full resolution).
        marble: None, or dict(look=sony_marble.Look, dev=dict(d218, d224, d2c0), dec=...) to run
        ZcTaskMarble after YCC2RGB.
        marble_inset: None = leave Marble's output as is (the outer 16 px are not valid);
        truthy = keep the pre-Marble values outside Marble's valid region instead.  This is
        one render pass: Marble runs on the YCC2RGB result that is already in memory, and
        the border comes from that same array (Sony itself leaves it unwritten).
        timings: optional dict filled with per-stage seconds.
        strip: row-band height for the full-resolution path (0/None = whole array at once).
        Sharpness needs 3 rows of context and Spica another 3; Sharpness itself also
        shrinks its task region by 2, so each band runs the pointwise chain on [y-8, y+8)
        and the spatial stages on their exact sub-bands.  Marble runs on slabs with a
        12-block halo.  The result is identical to processing the whole array."""
        atan, _ = SL.load_static_tables()
        tim = timings if timings is not None else {}
        mark = lambda k, t: tim.__setitem__(k, tim.get(k, 0.0) + time.perf_counter() - t)
        sscs = self.sscs if sscs is None else sscs
        lin = np.asarray(lin)
        H, W = lin.shape[:2]
        # pointwise=True: LM16 hue index from the pixel itself instead of the 3-tap row blur
        # (identical on flat areas; needed when the input is a list of colours, e.g. a LUT grid)
        ctl = dict(blur_luma=0, blur_chroma=0, scale=0, quadratic=1,
                   thresholds=(0.0, 0.0, 0.0)) if pointwise else None

        def chain(z):
            """Pointwise part through ChromaSuppress. Returns (Y, Cr, Cb)."""
            if sscs is not None:
                t = time.perf_counter()
                fn = SC.sscs_int if np.issubdtype(z.dtype, np.integer) else SC.sscs_float
                z = np.stack(fn(*(np.ascontiguousarray(z[..., c]) for c in range(3)), sscs), -1)
                mark("sscs", t)
            planes = [np.ascontiguousarray(z[..., c], np.float32) for c in range(3)]
            t = time.perf_counter()
            enc = SL.render_look(planes, self.table, self.gamma, atan, ctl)  # int16 x3 (LM16 + MainGamma)
            mark("look", t)
            t = time.perf_counter()
            y, cr, cb = SY.rgb2ycc(*enc, self.ycc_params, self.luma, clamp=True)
            mark("rgb2ycc", t)
            t = time.perf_counter()
            if self.area_comp is not None:                                 # ARW 6.0 (0x3600) + 0x780f
                cr, cb = SS.area_comp_apply(cr, cb, self.area_comp)
            y, cr, cb = SS.chroma_suppress_apply(y, cr, cb, self.cs)
            mark("chroma", t)
            return y, cr, cb

        def finish(pre):
            """Marble + Effect on the assembled pre-Marble planes; returns the int32 stack."""
            r, g, b = pre
            if marble is not None:
                import sony_marble as MB
                t = time.perf_counter()
                look = marble["look"]
                # Marble is local and Sony leaves a 16-px border unwritten.  Run it on row
                # slabs with enough block context and keep the exact valid rows/columns;
                # the border keeps the pre-Marble values, as with marble_inset.
                B = 8 if look.s16(0x109e) == 1 else 4
                # Measured on a full-resolution file: 12 block rows of context on either
                # side give bit-identical valid rows (5x5 sigma + 3x3 + upsample).
                halo = 12 * B
                step = (strip // B) * B if strip else 0
                if marble_inset and step and H > step + 2 * halo and W > 32:
                    pre0, pre1, pre2 = pre
                    r, g, b = pre0.copy(), pre1.copy(), pre2.copy()
                    for s0 in range(0, H, step):
                        s1 = min(s0 + step, H)
                        oy0, oy1 = max(s0, 16), min(s1, H - 16)
                        if oy0 >= oy1:
                            continue
                        a0, a1 = max(0, s0 - halo), min(H, s1 + halo)
                        (sr, sg, sb), vr = MB.marble(pre0[a0:a1], pre1[a0:a1], pre2[a0:a1],
                                                     look, marble["dev"], marble["dec"],
                                                     mode=self.output_mode)
                        x0v, x1v = vr[0], vr[2]
                        r[oy0:oy1, x0v:x1v] = sr[oy0 - a0:oy1 - a0, x0v:x1v]
                        g[oy0:oy1, x0v:x1v] = sg[oy0 - a0:oy1 - a0, x0v:x1v]
                        b[oy0:oy1, x0v:x1v] = sb[oy0 - a0:oy1 - a0, x0v:x1v]
                else:
                    (mr, mg, mb), vr = MB.marble(*(np.asarray(c, np.uint16) for c in (r, g, b)),
                                                 look, marble["dev"], marble["dec"],
                                                 mode=self.output_mode)
                    if marble_inset:
                        # Marble writes only its valid region; keep the pre-Marble value in the border
                        # (the same composite sony_decode used to make with a second full render).
                        keep = np.ones((H, W), bool)
                        keep[vr[1]:vr[3], vr[0]:vr[2]] = False
                        r, g, b = (np.where(keep, p, q).astype(np.uint16) for p, q in zip(pre, (mr, mg, mb)))
                    else:
                        r, g, b = mr, mg, mb
                mark("marble", t)
            if self.look_id & ~1 == 0x10:      # render+0x44 = 0 sRGB, 1 AdobeRGB
                t = time.perf_counter()
                r, g, b = EF.effect_apply(*(np.asarray(c, np.uint16) for c in (r, g, b)),
                                          self.look_id, self.tint, mode=self.output_mode)
                mark("effect", t)
            out = np.empty((H, W, 3), np.int32)
            out[..., 0] = r
            out[..., 1] = g
            out[..., 2] = b
            return out

        if pointwise or not strip or H <= strip:
            y, cr, cb = chain(lin)
            if detail and detail.get("sharp") is not None:
                import sony_detail as SDT
                t = time.perf_counter()
                y = SDT.sharpness(np.asarray(y, np.uint16), (0, 0, W, H), detail["sharp"])
                mark("sharp", t)
            t = time.perf_counter()
            y = SS.ygamma_apply(y, self.ylut, self.yoff, self.ygain)
            mark("ygamma", t)
            if detail and detail.get("spica") is not None:
                import sony_detail as SDT
                t = time.perf_counter()
                y = SDT.spica(np.asarray(y, np.uint16), (0, 0, W, H), *detail["spica"])
                mark("spica", t)
            t = time.perf_counter()
            r, g, b = SY.ycc2rgb(y, cr, cb)
            mark("ycc2rgb", t)
            return finish([np.asarray(c, np.uint16) for c in (r, g, b)])

        # Full resolution: row bands.  c0..c1 gives Sharpness its 3-row context, and the
        # Spica input is the exact [g0, g1) sub-band (its own 3-row support), so no stage
        # sees a band edge where the whole-image version would not.
        import sony_detail as SDT
        pre = [np.empty((H, W), np.uint16) for _ in range(3)]
        for ya in range(0, H, strip):
            yb = min(ya + strip, H)
            c0, c1 = max(0, ya - 8), min(H, yb + 8)
            y, cr, cb = chain(np.ascontiguousarray(lin[c0:c1]))
            if detail and detail.get("sharp") is not None:
                t = time.perf_counter()
                y = SDT.sharpness(np.asarray(y, np.uint16), (0, 0, W, c1 - c0), detail["sharp"])
                mark("sharp", t)
            t = time.perf_counter()
            y = SS.ygamma_apply(y, self.ylut, self.yoff, self.ygain)
            mark("ygamma", t)
            g0, g1 = max(0, ya - 3), min(H, yb + 3)
            sub = y[g0 - c0:g1 - c0]
            if detail and detail.get("spica") is not None:
                t = time.perf_counter()
                sub = SDT.spica(np.asarray(sub, np.uint16), (0, 0, W, g1 - g0), *detail["spica"])
                mark("spica", t)
            t = time.perf_counter()
            r, g, b = SY.ycc2rgb(sub[ya - g0:yb - g0], cr[ya - c0:yb - c0], cb[ya - c0:yb - c0])
            mark("ycc2rgb", t)
            pre[0][ya:yb] = r
            pre[1][ya:yb] = g
            pre[2][ya:yb] = b
        return finish(pre)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arw")
    ap.add_argument("looks", nargs="+")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--out", default="render_output")
    ap.add_argument("--color-space", default="srgb", choices=["srgb", "adobe"])
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(exist_ok=True)
    lin = front_end_sony(a.arw, a.scale)
    looks = load_looks(a.arw)
    from PIL import Image
    output_mode = 1 if a.color_space == "adobe" else 0
    for name in a.looks:
        img = Look(name, looks, output_mode=output_mode).render(lin)
        Image.fromarray((img >> 6).clip(0, 255).astype(np.uint8)).save(out / f"{name}_s{a.scale}.png")
        print(name, img.shape, img.mean(axis=(0, 1)).round(1))


if __name__ == "__main__":
    main()
