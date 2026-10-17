#!/usr/bin/env python3
"""End-to-end Sony ARW decode: raw -> the full-resolution image Edit would export.

Pipeline, in the builder's order:

    RAW -> [RawNR] -> ITP demosaic -> [DRO/Vatr] -> GeoCorr -> SSCS
        -> LinearMatrix16 + MainGamma -> RGB2YCC -> AreaComp -> ChromaSuppress
        -> [Sharpness] -> YGamma -> [Spica] -> YCC2RGB -> [Marble] -> [Effect]

Every stage is the bit-exact port in this directory.  Optional stages follow the
develop settings when Edit saved the ARW (sonylook/develop_settings.py); for a
camera-original ARW they default off (except SSCS, which the builder always
inserts).  The output is the decoder rectangle (DefaultCropOrigin/Size), e.g.
7008x4672 for the A7M5, as uint16 0..16383.

Not ported (and not running for the current files): ToneCurve, ShadeCorrection,
3DLut construction, the 4.1 picker helpers.
"""
from __future__ import annotations

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


def decode(arw, look=None, *, raw=None, nr=None, dro=None, marble=None, detail=True,
           lens=True, sscs=True, pad=32, verbose=False):
    """Decode an ARW to the exported image (H, W, 3) uint16 0..16383.

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
    """
    t0 = time.perf_counter()
    timings = {}
    arw = Path(arw)

    main = AL.main_tags(str(arw))
    tags = _sr2_tags(arw)
    st = DS.effective_settings(arw)
    black = _ints(main["0x7310"])
    levels = _ints(main["0x7313"])
    s7311 = int(main["0x7311"])
    k7809 = int(main["0x7809"])
    dec = DP.decoder_params(arw)
    iso = int(dec["iso"])

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
        gains = F.wb_gains(levels, k7809, 0, int(st.get(DS.EXPOSURE, 0)), mode=3,
                           state_r=sr, state_b=sb, rb_factor=int(st.get(DS.RB_FACTOR, 0)))
    else:
        gains = F.wb_gains(levels, k7809, int(st.get(DS.WB_TINT, 0)),
                           int(st.get(DS.EXPOSURE, 0)), mode=mode,
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
    planes = list(ITP.itp_full_image(raw, black, gains, s7311))
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
        out = [np.empty((H, W), np.uint16) for _ in range(3)]
        for ya in range(0, H, GEO_STRIP):
            yb = min(ya + GEO_STRIP, H)
            ch = LN.geocorr_u16(planes, (ox, oy), (ox, oy + ya, ox + W, oy + yb), L)
            for k in range(3):
                out[k][ya:yb] = ch[k]
        planes = out
    timings["geocorr"] = time.perf_counter() - t

    # --- SSCS (the builder always inserts it, right after GeoCorr) ------------
    t = time.perf_counter()
    if sscs:
        planes = list(SC.sscs_int(*planes, SC.params_from_tags(tags)))
    timings["sscs"] = time.perf_counter() - t

    lin = np.stack(planes, -1).astype(np.uint16)

    # --- look chain, with Sharpness/Spica and Marble --------------------------
    t = time.perf_counter()
    detail_cfg = None
    if detail is None:
        detail = True
    if detail:
        P = SDT.sharpness_params(dev208=int(st.get(0x801A, 0)), dev20c=int(st.get(0x801B, 0)),
                                 dev210=int(st.get(0x801C, 0)), dev214=int(st.get(0x801D, 0)))
        detail_cfg = dict(sharp=P, spica=(SDT.load_srsettings(), SDT.spica_params(iso)))

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
        fade = int(st[DS.FADE]) // 10          # the slider is whole steps
    else:
        fade = 0 if look in ("FL2", "FL3") else None
    lk = R.Look(look, looks, fade=fade)
    if marble:
        # Marble writes only the region inset by 16 px; everywhere else the image
        # keeps its YCC2RGB value (the pre-Marble render, Effect included).  Render
        # both and composite, instead of inventing an edge extension.
        pre = np.asarray(lk.render(lin, detail=detail_cfg, marble=None), np.uint16)
        img = np.asarray(lk.render(lin, detail=detail_cfg, marble=marble_cfg), np.uint16)
        m = 16
        H, W = img.shape[:2]
        valid = np.zeros((H, W), bool)
        valid[m:H - m, m:W - m] = True
        img = np.where(valid[..., None], img, pre)
    else:
        img = np.asarray(lk.render(lin, detail=detail_cfg, marble=None), np.uint16)
    # crop back to the decoder rectangle
    ty, tx = y0 - ey0, x0 - ex0
    img = img[ty:ty + (y1 - y0), tx:tx + (x1 - x0)]
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
    ap.add_argument("--out", default=None, help="output .tif (16-bit) or .png (8-bit)")
    ap.add_argument("--no-nr", action="store_true")
    ap.add_argument("--no-dro", action="store_true")
    ap.add_argument("--no-marble", action="store_true")
    ap.add_argument("--no-detail", action="store_true")
    ap.add_argument("--no-lens", action="store_true")
    ap.add_argument("--no-sscs", action="store_true")
    ap.add_argument("--pad", type=int, default=32, help="expand the decoder rect by N px of raw margin")
    a = ap.parse_args()
    img = decode(a.arw, a.look, nr=False if a.no_nr else None, dro=False if a.no_dro else None,
                 marble=False if a.no_marble else None, detail=not a.no_detail,
                 lens=not a.no_lens, sscs=not a.no_sscs, pad=a.pad, verbose=True)
    print(img.shape, img.dtype, int(img.min()), int(img.max()))
    if a.out:
        out = Path(a.out)
        if out.suffix.lower() in (".tif", ".tiff"):
            import tifffile
            tifffile.imwrite(out, img, photometric="rgb")
        else:
            from PIL import Image
            Image.fromarray((img >> 6).clip(0, 255).astype(np.uint8)).save(out)
        print("->", out)


if __name__ == "__main__":
    main()
