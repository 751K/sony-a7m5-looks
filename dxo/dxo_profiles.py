#!/usr/bin/env python3
"""DxO PhotoLab probes for the Sony A7M5 looks.

PhotoLab 10 reads DCP camera profiles (Color Rendering > DCP Profile) and applies .cube LUTs in a
selectable 'LUT Color Space' (LUT palette), but has no XMP RGBTable.  As with Camera Raw, the plan is
the linear base DCP ('SonyA7M5 Linear Base', same file as profiles/camera_raw) plus a look LUT, and the
probes measure what PhotoLab feeds the LUT:
  Probe       identity: the export shows the LUT input;
  Probe-Sq    output = input^2: is there anything after the LUT?
  Probe-Mono  output = ProPhoto luma of the encoded input on all channels: does PhotoLab add colour?
(Mono weights assume the LUT colour space is set to ProPhoto RGB.)

With that base, camera RGB is read as ProPhoto, so very saturated camera colours land outside the
spectral locus and PhotoLab compresses them into its working gamut (DxO Wide Gamut) before the LUT.
'SonyA7M5 Linear Base v2' keeps Adobe Standard's ColorMatrix1/2 and uses its D65 ForwardMatrix2 for
both illuminants: working colours are real and the camera -> working map is one fixed matrix,
independent of white balance.  Tone curve identity, DefaultBlackRender none, no tables.
PhotoLab compresses highlights before the LUT from Sony linear ~2900 up (not per channel for colours);
'SonyA7M5 Linear Base v3' is v2 with BaselineExposureOffset -1.5 EV, so the sensor clip (~8183)
stays in the linear range and the look LUT puts the exposure back.

.cube layout: LUT_3D_SIZE n, then n^3 'R G B' lines with R fastest, B slowest.
usage: dxo_profiles.py probes | base2 | base3  -> ../profiles/dxo/
"""
import shutil
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "camera_raw"))
import dcp  # noqa: E402

OUT = HERE.parent / "profiles" / "dxo"
BASE = HERE.parent / "profiles" / "camera_raw" / "SonyA7M5 Linear Base.dcp"
ADOBE_STD = Path("/Library/Application Support/Adobe/CameraRaw/CameraProfiles/Adobe Standard/Sony ILCE-7M5 Adobe Standard.dcp")
MODEL = "Sony ILCE-7M5"
BASE2_NAME = "SonyA7M5 Linear Base v2"
BASE3_NAME = "SonyA7M5 Linear Base v3"
BASE3_OFFSET = -1.5
PROPHOTO_Y = np.array([0.2880402, 0.7118741, 0.0000857])


def write_base2(name=BASE2_NAME, offset=None):
    a = dcp.read(ADOBE_STD)
    fm = a["ForwardMatrix2"] if a["CalibrationIlluminant2"] == [21] else a["ForwardMatrix1"]
    ent = [("UniqueCameraModel", 2, MODEL), ("ProfileName", 2, name),
           ("ColorMatrix1", 10, a["ColorMatrix1"]), ("ColorMatrix2", 10, a["ColorMatrix2"]),
           ("CalibrationIlluminant1", 3, a["CalibrationIlluminant1"]),
           ("CalibrationIlluminant2", 3, a["CalibrationIlluminant2"]),
           ("ForwardMatrix1", 10, fm), ("ForwardMatrix2", 10, fm),
           ("ProfileToneCurve", 11, [0.0, 0.0, 1.0, 1.0]), ("ProfileEmbedPolicy", 4, [0]),
           ("DefaultBlackRender", 4, [1]), ("ProfileCopyright", 2, "sony-a7m5-looks (not an Adobe profile)")]
    if offset is not None:
        ent.append(("BaselineExposureOffset", 10, [offset]))
    path = OUT / f"{name}.dcp"
    dcp.write(path, ent)
    return path


def grid(n):
    x = np.arange(n) / (n - 1)
    return np.stack(np.meshgrid(x, x, x, indexing="ij"), -1)


def write_cube(path, values, title):
    """values: (n, n, n, 3) floats in [0,1] indexed [r, g, b]."""
    n = values.shape[0]
    rows = np.clip(values, 0, 1).transpose(2, 1, 0, 3).reshape(-1, 3)
    head = (f'TITLE "{title}"\nLUT_3D_SIZE {n}\n'
            "DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
    with open(path, "w", newline="\n") as f:
        f.write(head)
        f.writelines("%.6f %.6f %.6f\n" % tuple(r) for r in rows)
    return path


def probes(n=64):
    g = grid(n)
    luma = g @ PROPHOTO_Y
    return {"Probe": g, "Probe-Sq": g ** 2, "Probe-Mono": np.repeat(luma[..., None], 3, -1)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    what = sys.argv[1] if len(sys.argv) > 1 else "probes"
    if what == "base2":
        print(write_base2())
        return
    if what == "base3":
        print(write_base2(BASE3_NAME, BASE3_OFFSET))
        return
    print(shutil.copyfile(BASE, OUT / BASE.name))
    if what == "probes":
        for k, v in probes().items():
            print(write_cube(OUT / f"SonyA7M5 {k}.cube", v, f"SonyA7M5 {k}"))


if __name__ == "__main__":
    main()
