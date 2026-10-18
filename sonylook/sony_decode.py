#!/usr/bin/env python3
"""End-to-end Sony ARW decode: raw -> the full-resolution image Edit would export.

Pipeline, in the builder's order:

    RAW -> [RawNR] -> ITP demosaic -> [DRO/Vatr] -> GeoCorr -> SSCS
        -> LinearMatrix16 + MainGamma -> RGB2YCC -> AreaComp -> ChromaSuppress
        -> [Sharpness] -> YGamma -> [Spica] -> YCC2RGB -> [Marble] -> [Effect]

Every stage is the bit-exact port in this directory.  Optional stages follow the
develop settings when Edit saved the ARW (sonylook/develop_settings.py); for a
camera-original ARW they default off (except SSCS, which the builder always
inserts).  Edit's creative-look sliders are applied too: contrast / highlights /
shadows in MainGamma's second curve family, hue + saturation in ZcTaskHueSaturation,
black / white level in the YGamma pixel path, Fineness in Sharpness, and the
exposure setting in both the WB gains and the ChromaSuppress thresholds
(settings ids: see develop_settings.CONTRAST etc. and docs/pipeline.md §10.27).
The tone-curve tool (0x9000-0x9007, R/G/B/Y) and peripheral illumination
(0x8030-0x8032, 0x900e/0x900f) run at the end of the look chain (§10.28).

The output is the decoder rectangle (DefaultCropOrigin/Size), e.g. 7008x4672 for
the A7M5, as uint16 0..16383 in the requested output color space
("srgb" or "adobe", LinearMatrix16 output_mode 0/1).  sony_export.write_image()
turns that into Edit's 8/16-bit TIFF or JPEG with the bundled ICC profile.

Not ported (and not running for the current files): ToneCurve, ShadeCorrection,
3DLut construction, the 4.1 picker helpers.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import arw_looks as AL          # noqa: E402
import decoder_params as DP     # noqa: E402
import develop_settings as DS   # noqa: E402
import sony_detail as SDT       # noqa: E402
import sony_dro as DRO          # noqa: E402
import sony_export as EXP       # noqa: E402
import sony_frontend as F       # noqa: E402
import sony_itp as ITP          # noqa: E402
import sony_lens as LN          # noqa: E402
import sony_marble as MB        # noqa: E402
import sony_rawnr as N          # noqa: E402
import sony_render as R         # noqa: E402
import sony_sscs as SC          # noqa: E402
import sr2                      # noqa: E402

GEO_STRIP = 512
DRO_STRIP = 256


def _ints(v):
    if isinstance(v, (list, tuple)):
        return [int(x) for x in v]
    return [int(x) for x in str(v).split()]


def _sr2_tags(arw):
    return {k: (v[1] if isinstance(v[1], list) else [v[1]])
            for k, v in sr2.sr2_subifd(str(arw)).items()}


def decode(arw, look=None, *, output="srgb", raw=None, nr=None, dro=None, marble=None,
           detail=True, lens=True, sscs=True, pad=32, strip=512, verbose=False,
           rust=None):
    """Decode an ARW to the exported image (H, W, 3) uint16 0..16383.

    output: "srgb" or "adobe" (AdobeRGB).  The look's LinearMatrix16 table is built
            with output_mode 0/1, param8 0: the working space stays sRGB (the look
            tables are authored there) and mode 1 applies Sony's sRGB -> AdobeRGB
            linear matrix.  BW/Sepia also pass it to the Effect stage (mode 0/1).
    look:   look name from arw_looks.look_ifds ("Standard", "FL2", ...); None follows the
            settings (0x8000: 1 = the look the camera shot with, SR2 0x7770; else a look id).
    raw:    optional full visible CFA image (uint16); read with rawpy if None.
    nr/dro/marble/lens/sscs: None = follow the develop settings: Edit's saved session
            when present, else the values Edit starts from for an unsaved ARW
            (develop_settings.camera_defaults).  SSCS always runs unless disabled.  detail: True/False (or
            None = same as True); runs Sharpness and Spica with the settings values.
    pad:    raw margin in px around the decoder rect for the spatial stages
            (default 32).  Marble writes only its 16-px-inset region; the rest of
            the image keeps the pre-Marble (YCC2RGB) render.
    strip:  row-band height for the look chain, Sharpness/Spica and Marble
            (0 = whole array, higher peak memory).
    """
    t0 = time.perf_counter()
    timings = {}
    arw = Path(arw)
    output = EXP.normalize_space(output)
    use_rust = (os.environ.get("SONYLOOK_RUST", "0") == "1") if rust is None else bool(rust)

    main = AL.main_tags(str(arw))
    tags = _sr2_tags(arw)
    st = DS.effective_settings(arw)
    black = _ints(main["0x7310"])
    levels = _ints(main["0x7313"])
    s7311 = int(main["0x7311"])
    k7809 = int(main["0x7809"])
    dec = DP.decoder_params(arw)
    iso = int(dec["iso"])
    exposure = int(st.get(DS.EXPOSURE, 0))
    # Edit sliders (all 0 by default): MainGamma tone family, HueSaturation,
    # YGamma black/white levels.
    tone = dict(contrast=int(st.get(DS.CONTRAST, 0)),
                highlight=int(st.get(DS.HIGHLIGHT_LEVEL, 0)),
                shadow=int(st.get(DS.SHADOW_LEVEL, 0)),
                hue=int(st.get(DS.HUE, 0)),
                saturation=int(st.get(DS.SATURATION, 0)),
                white_level=int(st.get(DS.WHITE_LEVEL, 0)),
                black_level=int(st.get(DS.BLACK_LEVEL, 0)),
                exposure=exposure)
    # Tone curve tool (R, G, B, Y) and peripheral illumination.
    def _seq(v):
        return v if isinstance(v, (list, tuple)) else (v,)
    tone_curves = [(_seq(st.get(DS.TONE_CURVE_X[k], (0, 511))),
                    _seq(st.get(DS.TONE_CURVE_Y[k], (0, 511)))) for k in range(4)]
    shade = dict(amount=int(st.get(DS.SHADE_AMOUNT, 80)),
                 radius=int(st.get(DS.SHADE_RADIUS, 0)),
                 strength=int(st.get(DS.SHADE_STRENGTH, 0)),
                 flag=int(st.get(DS.SHADE_FLAG, 0)),
                 angle=int(st.get(DS.SHADE_ANGLE, 0)))

    if raw is None:
        import rawpy
        with rawpy.imread(str(arw)) as r:
            assert r.raw_pattern.tolist() == [[0, 1], [3, 2]] and r.color_desc == b"RGBG", \
                "only RGGB sensors are wired through the full pipeline"
            raw = np.array(r.raw_image_visible, np.uint16, copy=True)
    raw = np.asarray(raw)

    # --- white balance (mode 3 samples the full decoder image) -----------------
    mode = int(st.get(DS.WB_MODE, 1))
    if mode == 3:
        sr, sb = F.wb_pick_state(arw, raw, black, levels)
        gains = F.wb_gains(levels, k7809, 0, exposure, mode=3,
                           state_r=sr, state_b=sb, rb_factor=int(st.get(DS.RB_FACTOR, 0)))
    else:
        gains = F.wb_gains(levels, k7809, int(st.get(DS.WB_TINT, 0)),
                           exposure, mode=mode,
                           temperature=st.get(DS.WB_TEMPERATURE, 6000),
                           temp_table=F.wb_temperature_table(main),
                           presets=F.wb_presets(main), rb_factor=int(st.get(DS.RB_FACTOR, 0)))
    timings["wb"] = time.perf_counter() - t0

    # --- decoder rectangle, optionally expanded into the raw margins ----------
    x0, y0, x1, y1 = DP.crop_rect(arw)
    if (x0 | y0) & 1:
        raise ValueError("odd decoder-rect origin breaks the RGGB phase")
    raw_crop = raw[y0:y1, x0:x1]              # decoder rect, for the DRO grid
    Hr, Wr = raw.shape
    ex0, ey0 = max(x0 - pad, 0), max(y0 - pad, 0)
    ex1, ey1 = min(x1 + pad, Wr), min(y1 + pad, Hr)
    ox, oy = ex0 - x0, ey0 - y0               # expanded array origin in crop coords
    raw = np.ascontiguousarray(raw[ey0:ey1, ex0:ex1])

    # --- RawNR (dev+0x218 / 0x801e) -------------------------------------------
    t = time.perf_counter()
    if nr is None:
        nr = int(st.get(DS.NR_ON, 0)) == 1
    if nr:
        nr_look = N.look_from_sr2(sr2.sr2_subifd(str(arw)))
        raw = N.rawnr(raw, nr_look, iso=iso)
    timings["rawnr"] = time.perf_counter() - t

    # --- ITP (1024 tiling, 12 px halo) ----------------------------------------
    t = time.perf_counter()
    planes = list(ITP.itp_full_image(raw, black, gains, s7311, rust=use_rust))
    timings["itp"] = time.perf_counter() - t

    # --- DRO / Vatr (dev+0x238 / 0x8022) --------------------------------------
    t = time.perf_counter()
    if dro is None:
        dro = int(st.get(DS.DRO_ON, 0)) == 1
    if dro:
        pts = DRO.curve_points_from_arw(arw)
        if pts is None:
            raise NotImplementedError("DRO on but the ARW has no camera curve (0x781b/0x781c)")
        G = DRO.build_grid(raw_crop, *pts, black=(black[0], black[1], black[3]))
        H, W = planes[0].shape
        out = [np.empty((H, W), np.uint16) for _ in range(3)]
        for ya in range(0, H, DRO_STRIP):
            yb = min(ya + DRO_STRIP, H)
            X, Y = np.meshgrid(np.arange(ox, ox + W, dtype=np.int64),
                               np.arange(oy + ya, oy + yb, dtype=np.int64))
            ch = DRO.vatr([p[ya:yb] for p in planes], X, Y, G)
            for k in range(3):
                out[k][ya:yb] = ch[k]
        planes = out
    timings["dro"] = time.perf_counter() - t

    # --- GeoCorr (distortion + lateral CA), in strips -------------------------
    t = time.perf_counter()
    if lens:
        L = LN.lens_from_arw(arw, dist_setting=int(st.get(DS.DISTORTION, -1)),
                             ca_setting=int(st.get(DS.CA_CORRECTION, 1)))
        H, W = planes[0].shape
        rust_geo, rust_lens = None, None
        if use_rust:
            import fused_rust as FR
            if FR.available():
                rust_geo, rust_lens = FR, FR.lens_config(L)
        out = [np.empty((H, W), np.uint16) for _ in range(3)]
        for ya in range(0, H, GEO_STRIP):
            yb = min(ya + GEO_STRIP, H)
            if rust_geo is not None:
                ch = rust_geo.geocorr_u16(planes, (ox, oy),
                                          (ox, oy + ya, ox + W, oy + yb), rust_lens)
            else:
                ch = LN.geocorr_u16(planes, (ox, oy), (ox, oy + ya, ox + W, oy + yb), L)
            for k in range(3):
                out[k][ya:yb] = ch[k]
        planes = out
    timings["geocorr"] = time.perf_counter() - t

    # --- SSCS (the builder always inserts it, right after GeoCorr) ------------
    # Run it inside Look.render, which applies it band by band; doing it here on the
    # whole full-resolution image costs several GB of int64 temporaries.
    sscs_params = SC.params_from_tags(tags) if sscs else None

    lin = np.stack(planes, -1).astype(np.uint16)
    del planes

    # --- look chain, with Sharpness/Spica and Marble --------------------------
    t = time.perf_counter()
    detail_cfg = None
    if detail is None:
        detail = True
    if detail:
        P = SDT.sharpness_params(dev208=int(st.get(DS.SHARPNESS_GAIN, 0)),
                                 dev20c=int(st.get(DS.SHARPNESS_OVERSHOOT, 0)),
                                 dev210=int(st.get(DS.SHARPNESS_UNDERSHOOT, 0)),
                                 dev214=int(st.get(DS.SHARPNESS_THRESHOLD, 0)),
                                 dev2c4=int(st.get(DS.SHARPNESS_FINENESS, 50)))
        # Sharpness and Spica both read ApConGain (dev+0x208) and Fineness (dev+0x2c4)
        detail_cfg = dict(sharp=P, spica=(SDT.load_srsettings(), SDT.spica_params(iso)),
                          dev208=int(st.get(DS.SHARPNESS_GAIN, 0)),
                          dev2c4=int(st.get(DS.SHARPNESS_FINENESS, 50)))

    mlook = MB.look_from_tags(tags)
    mdev = DS.marble_dev(st)
    if marble is None:
        marble = (mdev["d218"] == 1 and mlook.s16(0x109C) != 0) or mdev["d2c0"] != 0
    marble_cfg = dict(look=mlook, dev=mdev, dec=dec) if marble else None

    looks = R.load_looks(str(arw))
    if look is None:
        sel = int(st.get(DS.LOOK_SELECT, 1))
        if sel == 1:
            look = str(main.get("0x7770", "Standard")).strip()
        else:
            look = next((n for n, i in R.LOOK_IDS.items() if i == sel and n in looks), None)
            if look is None:
                raise ValueError(f"look id {sel:#x} (setting 0x8000) not in this ARW")
    # Fade: dev+0x2bc = 10 x Fade (setting 0x901b).  Edit's default table has no FL2/FL3
    # rows, so an unsaved file renders those with Fade 0; IN / SH use 3 / 6.
    if DS.FADE in st:
        fade_level = DS.fade_dev(st)           # dev+0x2bc as saved (10 x the slider)
        fade = None
    else:
        fade_level = None
        fade = 0 if look in ("FL2", "FL3") else None
    lk = R.Look(look, looks, fade=fade, fade_level=fade_level,
                output_mode=1 if output == "adobe" else 0,
                tone_curves=tone_curves, shade=shade, use_rust=use_rust, **tone)
    rt = {}
    if marble:
        # One render pass: Marble runs on the YCC2RGB result, and the 16-px border keeps the
        # pre-Marble value of that same array (the old code rendered the whole look chain twice).
        img = lk.render(lin, detail=detail_cfg, marble=marble_cfg,
                        sscs=sscs_params, marble_inset=16, timings=rt,
                        strip=strip)
    else:
        img = lk.render(lin, detail=detail_cfg, marble=None,
                        sscs=sscs_params, timings=rt, strip=strip)
    # crop back to the decoder rectangle
    ty, tx = y0 - ey0, x0 - ex0
    img = img[ty:ty + (y1 - y0), tx:tx + (x1 - x0)]
    for k, v in rt.items():
        timings[f"render.{k}"] = v
    timings["render"] = time.perf_counter() - t
    timings["total"] = time.perf_counter() - t0
    if verbose:
        print({k: round(v, 1) for k, v in timings.items()}, file=sys.stderr)
    return img


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Decode an ARW through the full Sony pipeline")
    ap.add_argument("arw")
    ap.add_argument("--look", default=None, help="default: the look in the settings / the one shot with")
    ap.add_argument("--out", default=None,
                    help="output .tif/.tiff (16-bit), .jpg (8-bit) or .png (8-bit)")
    ap.add_argument("--color-space", default="srgb", choices=["srgb", "adobe"],
                    help="output color space (default srgb)")
    ap.add_argument("--bits", type=int, default=None, choices=[8, 16],
                    help="bit depth (default: 16 for TIFF, 8 otherwise)")
    ap.add_argument("--quality", type=int, default=95, help="JPEG quality 1..100")
    ap.add_argument("--no-nr", action="store_true")
    ap.add_argument("--no-dro", action="store_true")
    ap.add_argument("--no-marble", action="store_true")
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--no-lens", action="store_true")
    ap.add_argument("--no-sscs", action="store_true")
    ap.add_argument("--pad", type=int, default=32, help="expand the decoder rect by N px of raw margin")
    ap.add_argument("--strip", type=int, default=512, help="row-band height for the render (0 = whole image)")
    ap.add_argument("--rust", action="store_true",
                    help="use the optional Rust fused render core (needs rust/sonylook_core built)")
    a = ap.parse_args()
    img = decode(a.arw, a.look, output=a.color_space,
                 nr=False if a.no_nr else None, dro=False if a.no_dro else None,
                 marble=False if a.no_marble else None, detail=not a.no_detail,
                 lens=not a.no_lens, sscs=not a.no_sscs, pad=a.pad, strip=a.strip, verbose=True,
                 rust=True if a.rust else None)
    print(img.shape, img.dtype, int(img.min()), int(img.max()))
    if a.out:
        out = EXP.write_image(img, a.out, color_space=a.color_space,
                              bits=a.bits, quality=a.quality)
        print("->", out)


if __name__ == "__main__":
    main()
