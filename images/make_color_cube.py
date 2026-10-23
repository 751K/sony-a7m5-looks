#!/usr/bin/env python3
"""README figure: where every colour the camera can record lands after a neutral raw
development and after the ST / FL2 looks.

Input: the surface of the object-colour solid (MacAdam limits: every reflectance 0..1 under D65,
10 nm steps) in white-balanced camera RGB via the ARW's D65 matrix, linear, 1.0 = diffuse white.
The matrix stands in for the sensor's spectral response, which the ARW does not carry.
Neutral: camera RGB -> XYZ (the ARW's D65 matrix) -> ProPhoto RGB, no tone curve, clipped.
ST / FL2: the verified look chain (sonylook) in Edit's sRGB and Adobe RGB output modes, exposed so
18 % grey lands at code 0.463 under ST; each output is re-expressed in ProPhoto RGB so all panels
share one set of axes.
Positions are ProPhoto code values (Bradford D65 -> D50); dots are drawn in their sRGB colour.
usage: make_color_cube.py ARW [OUT_DIR]   (writes color_cube_{en,zh}_{dark,light}.png)
"""
import sys
from pathlib import Path

import colour
import numpy as np
import rawpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sonylook"))
sys.path.insert(0, str(HERE.parent / "capture_one"))
import c1_look_profile as C  # noqa: E402
import sony_render as R  # noqa: E402
import sony_sscs as SC  # noqa: E402

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

D65 = np.array([0.95047, 1.0, 1.08883])
WP_D65 = colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]["D65"]
PROPHOTO = colour.RGB_COLOURSPACES["ProPhoto RGB"]
GREY = 0.18
# panel order: input, ST sRGB, ST Adobe / neutral, FL2 sRGB, FL2 Adobe
TEXT = {
    "en": dict(title=("Camera gamut", "Sony ST · sRGB output", "Sony ST · Adobe RGB output",
                      "Neutral raw development", "Sony FL2 · sRGB output", "Sony FL2 · Adobe RGB output"),
               sub=("all object colours under D65 · camera RGB", "plotted in ProPhoto RGB", "plotted in ProPhoto RGB",
                    "matrix, no tone curve · ProPhoto RGB", "plotted in ProPhoto RGB", "plotted in ProPhoto RGB")),
    "zh": dict(title=("相机可拍色域", "Sony ST · sRGB 输出", "Sony ST · Adobe RGB 输出",
                      "RAW 中性显影", "Sony FL2 · sRGB 输出", "Sony FL2 · Adobe RGB 输出"),
               sub=("D65 下所有物体色 · 相机 RGB", "画在 ProPhoto RGB 坐标中", "画在 ProPhoto RGB 坐标中",
                    "矩阵，无色调曲线 · ProPhoto RGB", "画在 ProPhoto RGB 坐标中", "画在 ProPhoto RGB 坐标中")),
}
ADOBE = colour.RGB_COLOURSPACES["Adobe RGB (1998)"]
THEME = {
    "dark": dict(bg="#1b1b1d", ink="#f2f2f2", muted="#9a9a9e", edge="#4a4a4e", ring="#ffffff", dot=None),
    "light": dict(bg="#ffffff", ink="#1f1f22", muted="#6b6b70", edge="#c8c8cc", ring="#1f1f22", dot="#00000033"),
}


def camera_gamut(arw, step=10):
    shape = colour.SpectralShape(380, 730, step)
    cmfs = colour.MSDS_CMFS["CIE 1931 2 Degree Standard Observer"].copy().align(shape)
    ill = colour.SDS_ILLUMINANTS["D65"].copy().align(shape)
    xyz = colour.volume.XYZ_outer_surface(cmfs, ill, point_order="Pulse Wave Width").reshape(-1, 3)
    xyz = np.unique(np.round(xyz / xyz[:, 1].max(), 6), axis=0)
    M = rawpy.imread(str(arw)).rgb_xyz_matrix[:3].astype(np.float64)       # XYZ -> camera
    cam = np.clip((xyz @ M.T) / (D65 @ M.T), 0, None)    # the matrix dips 0.03 below 0 at the ends
    return np.vstack([cam, [[GREY, GREY, GREY]]])


def srgb_encode(lin):
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)


def to_xyz(arw, cam):
    M = rawpy.imread(str(arw)).rgb_xyz_matrix[:3].astype(np.float64)       # XYZ -> camera
    return (cam * (D65 @ M.T)) @ np.linalg.inv(M).T


def prophoto(xyz):
    """XYZ (D65) -> ProPhoto RGB code values, clipped to 0..1."""
    lin = colour.XYZ_to_RGB(xyz, PROPHOTO, illuminant=WP_D65, chromatic_adaptation_transform="Bradford")
    return colour.models.cctf_encoding_ProPhotoRGB(np.clip(lin, 0, 1))


def srgb_from_xyz(xyz):
    return srgb_encode(colour.XYZ_to_sRGB(xyz, apply_cctf_encoding=False))


def srgb_to_xyz(code):
    return colour.sRGB_to_XYZ(np.clip(code, 0, 1))


def looks(arw):
    """{(look, output_mode): Look}; mode 1 = Edit's Adobe RGB output (sRGB -> Adobe RGB matrix in LM16)."""
    data = R.load_looks(str(arw))
    sscs = SC.params_from_arw(str(arw))
    return {(k, m): R.Look(k, data, sscs=sscs, output_mode=m) for k in ("Standard", "FL2") for m in (0, 1)}


def look_xyz(look, mode, lin):
    code = np.clip(C.look_srgb(look, lin), 0, 1)
    if mode == 0:
        return colour.sRGB_to_XYZ(code)
    return colour.RGB_to_XYZ(code, ADOBE, illuminant=WP_D65, apply_cctf_decoding=True)


def exposure(st):
    lo, hi = 500.0, 16000.0
    for _ in range(40):
        w = (lo + hi) / 2
        v = C.look_srgb(st, np.array([[GREY * w] * 3]))[0, 1]
        lo, hi = (w, hi) if v < 0.463 else (lo, w)
    return w


def draw(ax, pts, col, th, title, sub, font):
    ax.set_proj_type("ortho")
    ax.set_facecolor(th["bg"])
    ax.set_axis_off()
    for s in ((0, 0, 0), (1, 1, 0), (1, 0, 1), (0, 1, 1)):           # 12 cube edges
        for k in range(3):
            e = np.array([s, s], float)
            e[1, k] = 1 - e[0, k]
            ax.plot(*e.T, color=th["edge"], lw=0.8)
    ax.plot([0, 1], [0, 1], [0, 1], color=th["muted"], lw=0.8, ls=(0, (3, 3)))
    ax.scatter(*pts[:-1].T, c=np.clip(col[:-1], 0, 1), s=6, depthshade=False,
               linewidths=0.4 if th["dot"] else 0, edgecolors=th["dot"] or "face")
    ax.scatter(*pts[-1], s=70, facecolors="none", edgecolors=th["ring"], linewidths=1.4, depthshade=False)
    for p, lab in (((1.1, 0, 0), "R 1"), ((0, 1.1, 0), "G 1"), ((-0.1, -0.1, 1.08), "B 1"), ((-0.06, -0.06, -0.06), "0")):
        ax.text(*p, lab, color=th["muted"], fontsize=10, ha="center", va="center", fontfamily=font)
    ax.view_init(elev=16, azim=-112)
    ax.set_box_aspect((1, 1, 1))
    ax.set_xlim(0, 1), ax.set_ylim(0, 1), ax.set_zlim(0, 1)
    ax.set_title(f"{title}\n", color=th["ink"], fontsize=13, fontweight="bold", loc="left", fontfamily=font, pad=0)
    ax.text2D(0.0, 1.0, sub, transform=ax.transAxes, color=th["muted"], fontsize=10, va="top", fontfamily=font)


def main():
    arw = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE
    cam = camera_gamut(arw)
    lk = looks(arw)
    w = exposure(lk["Standard", 0])
    neu = to_xyz(arw, cam)
    xyzs = [neu] + [look_xyz(lk[k, m], m, cam * w) for k, m in
                    (("Standard", 0), ("Standard", 1), ("FL2", 0), ("FL2", 1))]
    neu_srgb = srgb_from_xyz(neu)
    pos = [cam, prophoto(xyzs[1]), prophoto(xyzs[2]), prophoto(neu), prophoto(xyzs[3]), prophoto(xyzs[4])]
    cols = [neu_srgb, srgb_from_xyz(xyzs[1]), srgb_from_xyz(xyzs[2]),       # dot colours (sRGB)
            neu_srgb, srgb_from_xyz(xyzs[3]), srgb_from_xyz(xyzs[4])]
    for lang, tx in TEXT.items():
        font = "Hiragino Sans GB" if lang == "zh" else "Helvetica"
        for theme, th in THEME.items():
            fig = plt.figure(figsize=(10.5, 7.4), dpi=150, facecolor=th["bg"])
            for i in range(6):
                ax = fig.add_subplot(2, 3, i + 1, projection="3d")
                draw(ax, pos[i], cols[i], th, tx["title"][i], tx["sub"][i], font)
            fig.subplots_adjust(left=0.02, right=0.98, top=0.92, bottom=0.0, wspace=0.12, hspace=0.18)
            fig.savefig(out / f"color_cube_{lang}_{theme}.png", facecolor=th["bg"])
            plt.close(fig)
    print("white_lin", round(w, 1))


if __name__ == "__main__":
    main()
