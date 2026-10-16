#!/usr/bin/env python3
"""Build Capture One input ICC profiles that reproduce Sony Creative Looks (A7M5).

Capture One feeds an input profile with white-balanced camera RGB, gamma 1.8 encoded
(its own SonyA7M5-*.icm: neutral input v gives Y = v^1.8, a = b = 0), and expects Lab (D50).
Sony's look chain (pixel_pipeline: LM16 0x780f -> MainGamma -> RGB2YCC -> AreaComp ->
ChromaSuppress -> YGamma -> YCC2RGB, all per-pixel and bit-verified) takes white-balanced
camera RGB on its ITP scale and returns display-encoded RGB (sRGB, as Edit exports).

So each profile node is   v -> Sony linear = SCALE * v^1.8 -> look -> sRGB -> XYZ D50 -> Lab,
and Capture One with the "Linear Response" curve shows the look's sRGB result.
SCALE: Sony G = 0.515625 (raw - 512); C1 normalises (raw - black) / (white - black) = /15871,
so SCALE = 0.515625 * 15871 = 8183.6 was the first assumption. The real encoding was measured with a
probe profile on the synthetic chart (fit_c1_encoding.py -> c1_encoding_table.json) and is used when present: roughly
linear in the shadows (v ~ lin / 1950) and compressive above, not a gamma-1.8 power law.

usage: c1_look_profile.py ARW [--grid 65] [--looks Standard,FL2,...] [--out DIR]
       c1_look_profile.py --probes [--out DIR]     (calibration profiles, no ARW needed)
"""
import argparse
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sonylook"))
import sony_render as R  # noqa: E402

SCALE = 0.515625 * (16383 - 512)
GAMMA_IN = 1.8
SHAPER = 0.5                    # grid coordinate s = v ** SHAPER (denser nodes in the shadows)
NAMES = {"Standard": "ST", "Vivid": "VV", "Neutral": "NT", "Portrait": "PT", "FL": "FL", "VV2": "VV2",
         "IN": "IN", "SH": "SH", "BW": "BW", "Sepia": "SE", "FL2": "FL2", "FL3": "FL3"}

# sRGB (D65) -> XYZ, then Bradford D65 -> D50 (as in the ICC sRGB profile)
M_SRGB = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                   [0.0193339, 0.1191920, 0.9503041]])
BRAD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])
D65 = np.array([0.95047, 1.0, 1.08883]); D50 = np.array([0.96422, 1.0, 0.82521])
CHAD = np.linalg.inv(BRAD) @ np.diag((BRAD @ D50) / (BRAD @ D65)) @ BRAD


def srgb_to_lab_d50(rgb):
    c = np.clip(rgb, 0, 1)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ (CHAD @ M_SRGB).T
    t = xyz / D50
    f = np.where(t > (6 / 29) ** 3, np.cbrt(t), t / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def lab_d50_to_srgb(lab):
    fy = (lab[..., 0] + 16) / 116
    f = np.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], -1)
    t = np.where(f > 6 / 29, f ** 3, 3 * (6 / 29) ** 2 * (f - 4 / 29))
    lin = (t * D50) @ np.linalg.inv(CHAD @ M_SRGB).T
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)


def look_srgb(look, lin):
    """lin: (..., 3) Sony-scale linear camera RGB -> sRGB in 0..1 via the verified look chain."""
    shp = lin.shape
    flat = np.clip(lin.reshape(-1, 1, 3), 0, 16383).astype(np.float32)
    out = look.render(flat, pointwise=True).reshape(shp).astype(np.float64)
    return out / 16383.0


ENCODING = HERE / "c1_encoding_table.json"


def v_to_lin(v):
    """Inverse of Capture One's profile-input encoding. Measured (fit_c1_encoding.py, chart probe export):
    table of log(Sony linear) -> log(v), monotone, extrapolated with the end slopes. Falls back to the
    gamma-1.8 assumption when the table is missing."""
    v = np.asarray(v, np.float64)
    if not ENCODING.exists():
        return SCALE * v ** GAMMA_IN
    import json
    E = json.loads(ENCODING.read_text())
    ll, lv = np.array(E["log_lin"]), np.array(E["log_v"])
    lv = np.maximum.accumulate(lv + np.arange(len(lv)) * 1e-9)       # strictly increasing for inversion
    y = np.log(np.clip(v, 1e-9, None))
    x = np.interp(y, lv, ll)
    x = np.where(y < lv[0], ll[0] + (y - lv[0]) / E["slope_low"], x)
    x = np.where(y > lv[-1], ll[-1] + (y - lv[-1]) / E["slope_high"], x)
    return np.where(v > 0, np.exp(x), 0.0)


def build_clut(look, grid):
    s = np.arange(grid) / (grid - 1)
    v = s ** (1 / SHAPER)
    lin1 = v_to_lin(v)
    R_, G_, B_ = np.meshgrid(lin1, lin1, lin1, indexing="ij")
    lin = np.stack([R_, G_, B_], -1)
    return srgb_to_lab_d50(look_srgb(look, lin))           # (g, g, g, 3), first index = R (ICC order)


# ---------------------------------------------------------------------------------------------
# ICC v2.4 writer (scnr, RGB -> Lab, A2B0 lut16Type)

def _tag_desc(text):
    a = text.encode("ascii") + b"\0"
    return b"desc" + bytes(4) + struct.pack(">I", len(a)) + a + bytes(4 + 4) + bytes(2 + 1 + 67)


def _tag_text(text):
    return b"text" + bytes(4) + text.encode("ascii") + b"\0"


def _tag_xyz(xyz):
    return b"XYZ " + bytes(4) + struct.pack(">3i", *(int(round(c * 65536)) for c in xyz))


def _tag_lut16(clut_lab, shaper_n=1025):
    g = clut_lab.shape[0]
    hdr = b"mft2" + bytes(4) + bytes([3, 3, g, 0])
    hdr += struct.pack(">9i", 65536, 0, 0, 0, 65536, 0, 0, 0, 65536)
    hdr += struct.pack(">HH", shaper_n, 2)
    x = np.arange(shaper_n) / (shaper_n - 1)
    shaper = np.round(np.clip(x ** SHAPER, 0, 1) * 65535)
    intab = np.concatenate([shaper] * 3).astype(">u2").tobytes()     # big-endian after concatenating
    L = np.clip(clut_lab[..., 0] / 100 * 65280, 0, 65535)
    a = np.clip((clut_lab[..., 1] + 128) * 256, 0, 65535)
    b = np.clip((clut_lab[..., 2] + 128) * 256, 0, 65535)
    table = np.round(np.stack([L, a, b], -1)).astype(">u2").tobytes()
    outtab = np.array([0, 65535] * 3, ">u2").tobytes()
    return hdr + intab + table + outtab


def write_icc(path, name, clut_lab):
    tags = [(b"desc", _tag_desc(name)), (b"cprt", _tag_text("Sony look port (research prototype)")),
            (b"wtpt", _tag_xyz(D50)), (b"A2B0", _tag_lut16(clut_lab))]
    n = len(tags)
    off = 128 + 4 + 12 * n
    table, body = b"", b""
    for sig, data in tags:
        while (off + len(body)) % 4:
            body += b"\0"
        table += sig + struct.pack(">II", off + len(body), len(data))
        body += data
    size = off + len(body)
    hdr = struct.pack(">I4sI4s4s4s", size, b"\0\0\0\0", 0x02400000, b"scnr", b"RGB ", b"Lab ")
    hdr += bytes(12) + b"acsp" + bytes(4) + bytes(4) + bytes(4) + bytes(4) + bytes(8) + struct.pack(">I", 0)
    hdr += struct.pack(">3i", *(int(round(c * 65536)) for c in D50)) + bytes(4)
    hdr = hdr.ljust(128, b"\0")
    Path(path).write_bytes(hdr + struct.pack(">I", n) + table + body)


# ---------------------------------------------------------------------------------------------
# evaluation of a written profile the way an ICC engine does (shaper -> trilinear/tetrahedral CLUT)

def read_lut16(path):
    d = Path(path).read_bytes()
    n = struct.unpack_from(">I", d, 128)[0]
    tags = {struct.unpack_from(">4s", d, 132 + 12 * i)[0]: struct.unpack_from(">II", d, 136 + 12 * i)
            for i in range(n)}
    off, _ = tags[b"A2B0"]
    g = d[off + 10]
    nin, nout = struct.unpack_from(">HH", d, off + 48)
    p = off + 52
    inT = np.frombuffer(d, ">u2", 3 * nin, p).reshape(3, nin) / 65535; p += 3 * nin * 2
    clut = np.frombuffer(d, ">u2", g ** 3 * 3, p).reshape(g, g, g, 3).astype(np.float64)
    lab = np.stack([clut[..., 0] / 65280 * 100, clut[..., 1] / 256 - 128, clut[..., 2] / 256 - 128], -1)
    return inT, lab


def apply_lut16(inT, lab, v, mode="tetra"):
    g = lab.shape[0]
    x = np.linspace(0, 1, inT.shape[1])
    s = np.stack([np.interp(np.clip(v[..., k], 0, 1), x, inT[k]) for k in range(3)], -1) * (g - 1)
    i = np.minimum(s.astype(np.int64), g - 2); f = s - i
    if mode == "trilinear":
        out = 0
        for dr in (0, 1):
            for dg in (0, 1):
                for db in (0, 1):
                    w = (f[..., 0] if dr else 1 - f[..., 0]) * (f[..., 1] if dg else 1 - f[..., 1]) * \
                        (f[..., 2] if db else 1 - f[..., 2])
                    out = out + w[..., None] * lab[i[..., 0] + dr, i[..., 1] + dg, i[..., 2] + db]
        return out
    # tetrahedral (LittleCMS style)
    fr, fg, fb = f[..., 0], f[..., 1], f[..., 2]
    c = lambda a, b_, c_: lab[i[..., 0] + a, i[..., 1] + b_, i[..., 2] + c_]
    c000, c111 = c(0, 0, 0), c(1, 1, 1)
    out = np.zeros(v.shape)
    conds = [(fr >= fg) & (fg >= fb), (fr >= fb) & (fb > fg), (fb > fr) & (fr >= fg),
             (fg > fr) & (fr >= fb), (fg >= fb) & (fb > fr), (fb > fg) & (fg > fr)]
    seqs = [((1, 0, 0), (1, 1, 0), fr, fg, fb), ((1, 0, 0), (1, 0, 1), fr, fb, fg),
            ((0, 0, 1), (1, 0, 1), fb, fr, fg), ((0, 1, 0), (1, 1, 0), fg, fr, fb),
            ((0, 1, 0), (0, 1, 1), fg, fb, fr), ((0, 0, 1), (0, 1, 1), fb, fg, fr)]
    for m, (p1, p2, a, b_, c_) in zip(conds, seqs):
        c1, c2 = c(*p1), c(*p2)
        val = c000 + (c1 - c000) * a[..., None] + (c2 - c1) * b_[..., None] + (c111 - c2) * c_[..., None]
        out = np.where(m[..., None], val, out)
    return out


def probe_cluts(grid):
    """Calibration profiles (input v on the profile's own grid):
    Probe: sRGB out = v (shows what C1 feeds the profile); Probe-Sq: v^2 (anything after the profile?);
    Probe-Mono: Rec.709 luma of v on all channels (does C1 add colour after the profile?)."""
    s = np.arange(grid) / (grid - 1)
    v = s ** (1 / SHAPER)
    V = np.stack(np.meshgrid(v, v, v, indexing="ij"), -1)
    mono = np.repeat((V @ np.array([0.2126, 0.7152, 0.0722]))[..., None], 3, -1)
    return {k: srgb_to_lab_d50(np.clip(x, 0, 1)) for k, x in (("Probe", V), ("Probe-Sq", V ** 2), ("Probe-Mono", mono))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arw", nargs="?", help="any A7M5 ARW (look parameters are read from it)")
    ap.add_argument("--probes", action="store_true")
    ap.add_argument("--grid", type=int, default=65)
    ap.add_argument("--looks", default=",".join(NAMES))
    ap.add_argument("--out", default=str(HERE.parent / "profiles" / "capture_one"))
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    if a.probes:
        for name, lab in probe_cluts(a.grid).items():
            write_icc(out / f"SonyA7M5-{name}.icc", f"SonyA7M5 {name}", lab)
            print(f"SonyA7M5-{name}.icc")
        return
    if not a.arw:
        ap.error("ARW required (or --probes)")
    looks = R.load_looks(a.arw)
    for name in a.looks.split(","):
        look = R.Look(name, looks)
        lab = build_clut(look, a.grid)
        fn = out / f"SonyA7M5-Look-{NAMES[name]}.icc"
        write_icc(fn, f"SonyA7M5 Look {NAMES[name]}", lab)
        print(fn.name, fn.stat().st_size, "L range", lab[..., 0].min().round(2), lab[..., 0].max().round(2))


if __name__ == "__main__":
    main()
