#!/usr/bin/env python3
"""ctypes binding for the optional Rust fused front (rust/sonylook_core).

The Rust kernel covers the pointwise part of the band loop:

    blur_rows -> hue index -> LinearMatrix16 -> MainGamma
    -> RGB2YCC -> AreaComp -> ChromaSuppress

and returns the Y / Cr / Cb planes that the Python band loop then feeds to
Sharpness, YGamma, Spica and YCC2RGB; the spatial chain itself is also in the
Rust library.  geocorr_u16() is the u16 GeoCorr path (sony_lens.geocorr_u16),
itp_full_image() / itp_process_tile() the ITP demosaic, marble_slab() the
Marble luma path, sscs_u16() the SSCS integer path, and build_main_gamma() /
effect_tables() the one-time LUTs.
The NumPy implementation in this directory stays the reference; this module is
only loaded when the shared library exists and SONYLOOK_RUST=1 / rust=True is
set.

Build:
    cargo build --release --manifest-path rust/sonylook_core/Cargo.toml

Set SONYLOOK_RUST_LIB to override the library path.
"""
from __future__ import annotations

import ctypes
import os
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

u16_p = ctypes.POINTER(ctypes.c_uint16)


class FrontParams(ctypes.Structure):
    _fields_ = [
        ("table", ctypes.c_void_p),   # 1024 x 18 float32
        ("gamma", ctypes.c_void_p),   # 65536 int16
        ("atan", ctypes.c_void_p),    # 512 float32
        ("luma", ctypes.c_int32 * 3),
        ("ycc", ctypes.c_int32 * 8),
        ("area", ctypes.c_float * 16),
        ("area_flag", ctypes.c_int32),
        ("thr", ctypes.c_float * 3),
        ("w9", ctypes.c_int64),
        ("w12", ctypes.c_int64),
        ("w8", ctypes.c_int64),
        ("w11", ctypes.c_int64),
    ]


class SpatialParams(ctypes.Structure):
    _fields_ = [
        ("sharp_thr", ctypes.c_float),
        ("sharp_s0", ctypes.c_float),
        ("sharp_s1", ctypes.c_float),
        ("sharp_s9", ctypes.c_float),
        ("sharp_k", ctypes.c_float),
        ("ylut", ctypes.c_void_p),      # 65536 int16
        ("ygamma_off", ctypes.c_int32),
        ("ygamma_gain", ctypes.c_float),
        ("ygamma_ac", ctypes.c_int32),
        ("ygamma_b0", ctypes.c_int32),
        ("patterns", ctypes.c_void_p),  # 256 x 3 int32
        ("coefs", ctypes.c_void_p),     # 100 x 26 float32
        ("spica_thr", ctypes.c_uint64),
        ("spica_full", ctypes.c_int32),
        ("spica_gain_on", ctypes.c_int32),
        ("spica_s", ctypes.c_float),
        ("spica_k2", ctypes.c_float),
        ("dev208", ctypes.c_int32),
        ("dev2c4", ctypes.c_int32),
        ("s", ctypes.c_float * 25),
    ]


@dataclass
class Config:
    params: FrontParams
    keep: tuple            # arrays referenced by the struct must stay alive


@dataclass
class SpatialConfig:
    params: SpatialParams
    keep: tuple


class LensParams(ctypes.Structure):
    _fields_ = [
        ("cx", ctypes.c_float),
        ("cy", ctypes.c_float),
        ("s", ctypes.c_float),
        ("s2", ctypes.c_float),
        ("step", ctypes.c_float),
        ("n", ctypes.c_int32),
        ("table_r", ctypes.c_void_p),
        ("table_g", ctypes.c_void_p),
        ("table_b", ctypes.c_void_p),
        ("off", ctypes.c_float * 6),
    ]


@dataclass
class LensConfig:
    params: LensParams
    keep: tuple


class ItpParams(ctypes.Structure):
    _fields_ = [
        ("k1", ctypes.c_float),
        ("k2", ctypes.c_float),
        ("k3", ctypes.c_float),
        ("k4", ctypes.c_float),
        ("k5", ctypes.c_float),
        ("k6", ctypes.c_float),
        ("blend_k", ctypes.c_float),
        ("blend_lo", ctypes.c_float),
        ("blend_hi", ctypes.c_float),
        ("blend_half", ctypes.c_float),
    ]


@dataclass
class ItpConfig:
    params: ItpParams
    table1: "np.ndarray"
    table2: "np.ndarray"
    k1: "np.ndarray"
    k2: "np.ndarray"


class MarbleParams(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_int32),
        ("d218", ctypes.c_int32),
        ("d224", ctypes.c_int32),
        ("d2c0", ctypes.c_int32),
        ("iso", ctypes.c_int32),
        ("f0", ctypes.c_int32),
        ("d14c", ctypes.c_int32),
        ("f22780", ctypes.c_float),
        ("f22784", ctypes.c_float),
        ("d1b0", ctypes.c_int32),
        ("look", ctypes.c_void_p),
        ("srgb_dec", ctypes.c_void_p),
        ("gamma_dec", ctypes.c_void_p),
        ("srgb_enc", ctypes.c_void_p),
        ("gamma_enc", ctypes.c_void_p),
        ("m_fwd", ctypes.c_int32 * 9),
        ("m_inv", ctypes.c_int32 * 9),
    ]


@dataclass
class MarbleConfig:
    params: MarbleParams
    keep: tuple


class SscsParams(ctypes.Structure):
    _fields_ = [
        ("t", ctypes.c_int32),
        ("tr", ctypes.c_int32),
        ("tg", ctypes.c_int32),
        ("tb", ctypes.c_int32),
        ("wr", ctypes.c_int32),
        ("wg", ctypes.c_int32),
        ("wb", ctypes.c_int32),
    ]


@dataclass
class SscsConfig:
    params: SscsParams


def _lib_path() -> Path:
    env = os.environ.get("SONYLOOK_RUST_LIB")
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    name = "libsonylook_core.dylib" if os.uname().sysname == "Darwin" else "libsonylook_core.so"
    return root / "rust" / "sonylook_core" / "target" / "release" / name


_lib = None
_load_error = None     # the first load failure, re-raised without retrying


def _load():
    global _lib, _load_error
    if _lib is not None:
        return _lib
    if _load_error is not None:
        raise _load_error
    try:
        _lib = _bind(_lib_path())
    except (OSError, AttributeError) as exc:
        # OSError: missing / unloadable library.  AttributeError: a stale build that
        # lacks one of the symbols below (ctypes raises it on lib.<name>).
        _load_error = OSError(f"Rust library unusable: {exc}")
        _load_error.__cause__ = exc
        raise _load_error
    return _lib


def _bind(path):
    if not path.exists():
        raise FileNotFoundError(f"Rust library not built: {path}")
    lib = ctypes.CDLL(str(path))
    lib.fused_front_u16.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # r, g, b
        ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,   # width, height, stride
        ctypes.POINTER(FrontParams),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # y, cr, cb
    ]
    lib.fused_front_u16.restype = ctypes.c_int32
    lib.fused_spatial_u16.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # y, cr, cb
        ctypes.c_size_t, ctypes.c_size_t,                    # width, band height
        ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,   # c0, ya, yb
        ctypes.c_size_t, ctypes.c_size_t,                    # g0, g1
        ctypes.c_size_t,                                     # Spica threads (0 = env)
        ctypes.POINTER(SpatialParams),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # r, g, b
    ]
    lib.fused_spatial_u16.restype = ctypes.c_int32
    lib.geocorr_u16_rust.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # input planes
        ctypes.c_size_t, ctypes.c_size_t,                    # plane width/height
        ctypes.c_int32, ctypes.c_int32,                      # origin x/y
        ctypes.c_int32, ctypes.c_int32,                      # region x0/y0
        ctypes.c_int32, ctypes.c_int32,                      # region x1/y1
        ctypes.POINTER(LensParams),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # output planes
    ]
    lib.geocorr_u16_rust.restype = ctypes.c_int32
    lib.itp_process_u16.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,   # raw, w, h
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,    # black, gains, scale
        ctypes.c_void_p, ctypes.c_void_p,                    # table1, table2
        ctypes.c_void_p, ctypes.c_void_p,                    # k1, k2
        ctypes.POINTER(ItpParams),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # r, g, b
    ]
    lib.itp_process_u16.restype = ctypes.c_int32
    lib.itp_debug_u16.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ItpParams), ctypes.c_int32, ctypes.c_void_p,
    ]
    lib.itp_debug_u16.restype = ctypes.c_int32
    lib.itp_full_image_u16.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,   # raw, w, h
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,    # black, gains, scale
        ctypes.POINTER(ItpParams),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # r, g, b
        ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,   # tile, halo, threads
    ]
    lib.itp_full_image_u16.restype = ctypes.c_int32
    lib.marble_slab_u16.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # r, g, b
        ctypes.c_size_t, ctypes.c_size_t,                    # w, h
        ctypes.POINTER(MarbleParams),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,   # outputs
        ctypes.c_int32, ctypes.c_int32, ctypes.c_void_p,     # debug stage/plane/out
    ]
    lib.marble_slab_u16.restype = ctypes.c_int32
    lib.sscs_int_u16.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.POINTER(SscsParams),
        ctypes.c_void_p, ctypes.c_size_t,
    ]
    lib.sscs_int_u16.restype = ctypes.c_int32
    lib.build_main_gamma_i16.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    lib.build_main_gamma_i16.restype = ctypes.c_int32
    lib.effect_tables_i16.argtypes = [ctypes.c_void_p] * 4
    lib.effect_tables_i16.restype = ctypes.c_int32
    return lib


def available() -> bool:
    """True if the library loads and has every symbol; a failure is cached."""
    try:
        _load()
        return True
    except OSError:
        return False


def config_from_look(look, atan) -> Config:
    """Build the parameter block for a sony_render.Look instance."""
    table = np.ascontiguousarray(look.table, np.float32)
    gamma = np.ascontiguousarray(look.gamma, np.int16)
    atan = np.ascontiguousarray(atan, np.float32)
    p = FrontParams()
    p.table = table.ctypes.data
    p.gamma = gamma.ctypes.data
    p.atan = atan.ctypes.data
    luma = [int(v) for v in look.luma]
    ycc = [int(v) for v in look.ycc_params]
    area, flag = look.area_comp if look.area_comp is not None else ([0.0] * 16, 0)
    cs = look.cs
    for i in range(3):
        p.luma[i] = luma[i]
    for i in range(8):
        p.ycc[i] = ycc[i]
    for i in range(16):
        p.area[i] = float(area[i])
    p.area_flag = int(flag)
    for i in range(3):
        p.thr[i] = 0.0
    p.w9, p.w12, p.w8, p.w11 = (int(cs[0]), int(cs[1]), int(cs[2]), int(cs[3]))
    return Config(params=p, keep=(table, gamma, atan))


def front_u16(z, cfg: Config):
    """z: (H, W, 3) uint16 (interleaved). Returns (Y, Cr, Cb) uint16 planes."""
    z = np.ascontiguousarray(z, np.uint16)
    if z.ndim != 3 or z.shape[2] != 3:
        raise ValueError("front_u16 expects (H, W, 3)")
    h, w, _ = z.shape
    y = np.empty((h, w), np.uint16)
    cr = np.empty((h, w), np.uint16)
    cb = np.empty((h, w), np.uint16)
    base = z.ctypes.data
    lib = _load()
    rc = lib.fused_front_u16(
        base, base + 2, base + 4,
        w, h, 3, ctypes.byref(cfg.params),
        y.ctypes.data, cr.ctypes.data, cb.ctypes.data,
    )
    if rc != 0:
        raise RuntimeError(f"fused_front_u16 failed: {rc}")
    return y, cr, cb


# S-dict keys used by spica_gain, in struct order.
_SPICA_KEYS = (0x44, 0x48, 0x4c, 0x50, 0x54, 0x58,
               0x64, 0x68, 0x6c, 0x70, 0x74, 0x78,
               0x84, 0x88, 0x8c, 0x90, 0x94, 0x98,
               0xa4, 0xa8, 0xac, 0xb0, 0xb4, 0xb8,
               0xc4)


def spatial_config_from_look(look, detail) -> SpatialConfig:
    sharp = detail["sharp"]
    T, S = detail["spica"]
    ylut = np.ascontiguousarray(look.ylut, np.int16)
    patterns = np.ascontiguousarray(T["patterns"], np.int32)
    coefs = np.ascontiguousarray(T["coefs"], np.float32)
    p = SpatialParams()
    p.sharp_thr = float(sharp["thr"])
    p.sharp_s0 = float(sharp["s0"])
    p.sharp_s1 = float(sharp["s1"])
    p.sharp_s9 = float(sharp["s9"])
    p.sharp_k = float(sharp["k"])
    p.ylut = ylut.ctypes.data
    p.ygamma_off = int(look.yoff)
    p.ygamma_gain = float(look.ygain)
    p.ygamma_ac = int(look.black_level)   # ygamma_apply dev2ac
    p.ygamma_b0 = int(look.white_level)   # ygamma_apply dev2b0
    p.patterns = patterns.ctypes.data
    p.coefs = coefs.ctypes.data
    p.spica_thr = int(S["thr"]) & 0xFFFFFFFFFFFFFFFF
    p.spica_full = int(S["full"])
    p.spica_gain_on = 1 if S["gain_on"] else 0
    p.spica_s = float(S["s"])
    p.spica_k2 = float(S["k2"])
    p.dev208 = int(detail.get("dev208", 0))     # ApConGain (0x801a), Spica gain
    p.dev2c4 = int(detail.get("dev2c4", 50))    # Fineness (0x901d), Spica gain
    for i, k in enumerate(_SPICA_KEYS):
        p.s[i] = float(S[k])
    return SpatialConfig(params=p, keep=(ylut, patterns, coefs))


def spatial_u16(y, cr, cb, c0, ya, yb, g0, g1, cfg: SpatialConfig, threads=0):
    """Sharpness -> YGamma -> Spica -> YCC2RGB for one render band.

    y/cr/cb are the band [c0, c1) planes; the returned R/G/B planes hold rows
    [ya, yb).  g0..g1 are the Spica sub-band (already clamped to the image).
    """
    y = np.ascontiguousarray(y, np.uint16)
    cr = np.ascontiguousarray(cr, np.uint16)
    cb = np.ascontiguousarray(cb, np.uint16)
    h, w = y.shape
    oh = yb - ya
    r = np.empty((oh, w), np.uint16)
    g = np.empty((oh, w), np.uint16)
    b = np.empty((oh, w), np.uint16)
    rc = _load().fused_spatial_u16(
        y.ctypes.data, cr.ctypes.data, cb.ctypes.data,
        w, h, c0, ya, yb, g0, g1, int(threads), ctypes.byref(cfg.params),
        r.ctypes.data, g.ctypes.data, b.ctypes.data,
    )
    if rc != 0:
        raise RuntimeError(f"fused_spatial_u16 failed: {rc}")
    return r, g, b


def lens_config(L) -> LensConfig:
    """Build the parameter block for a sony_lens.lens_from_arw() dict."""
    tabs = [np.ascontiguousarray(t, np.float32) for t in L["tables"]]
    p = LensParams()
    p.cx = float(L["cx"])
    p.cy = float(L["cy"])
    p.s = float(L["s"])
    p.s2 = float(L["s2"])
    p.step = float(L["step"])
    p.n = int(L["n"])
    p.table_r = tabs[0].ctypes.data
    p.table_g = tabs[1].ctypes.data
    p.table_b = tabs[2].ctypes.data
    for k in range(3):
        ox, oy = L["off"][k]
        p.off[2 * k] = float(ox)
        p.off[2 * k + 1] = float(oy)
    return LensConfig(params=p, keep=tuple(tabs))


def geocorr_u16(planes, origin, region, cfg: LensConfig):
    """ZcTaskGeoCorr on three uint16 planes.

    planes: 3 x (H, W) covering the whole-image rect whose (0,0) is `origin`;
    region: (x0, y0, x1, y1) in whole-image coordinates.  Returns 3 planes.
    """
    ps = [np.ascontiguousarray(p, np.uint16) for p in planes]
    ph, pw = ps[0].shape
    x0, y0, x1, y1 = region
    ow, oh = x1 - x0, y1 - y0
    outs = [np.empty((oh, ow), np.uint16) for _ in range(3)]
    rc = _load().geocorr_u16_rust(
        ps[0].ctypes.data, ps[1].ctypes.data, ps[2].ctypes.data, pw, ph,
        int(origin[0]), int(origin[1]), x0, y0, x1, y1,
        ctypes.byref(cfg.params),
        outs[0].ctypes.data, outs[1].ctypes.data, outs[2].ctypes.data,
    )
    if rc != 0:
        raise RuntimeError(f"geocorr_u16_rust failed: {rc}")
    return outs


def itp_config(params=None, table1=None, table2=None, k1=None, k2=None) -> ItpConfig:
    """Parameter block for the ITP kernel; defaults are sony_itp's constants."""
    import sony_itp as ITP
    params = ITP.PARAMS_DEFAULT if params is None else params
    table1 = ITP.TABLE_DEFAULT if table1 is None else table1
    table2 = ITP.TABLE_DEFAULT if table2 is None else table2
    k1 = ITP.K9_A if k1 is None else k1
    k2 = ITP.K9_B if k2 is None else k2
    f = lambda off: struct.unpack_from("<f", params, off)[0]
    p = ItpParams()
    p.k1 = f(0x11)
    p.k2 = f(0x15)
    p.k3 = f(0x1d)
    p.k4 = f(0x21)
    p.k5 = f(0x25)
    p.k6 = f(0x19)
    p.blend_k = f(1)
    p.blend_lo = f(5)
    p.blend_hi = f(9)
    p.blend_half = f(0xd)
    return ItpConfig(
        params=p,
        table1=np.ascontiguousarray(table1, np.float32),
        table2=np.ascontiguousarray(table2, np.float32),
        k1=np.ascontiguousarray(k1, np.float32),
        k2=np.ascontiguousarray(k2, np.float32),
    )


def itp_process_tile(raw, black, gains, scale_7311, cfg: ItpConfig):
    """One ITP tile: raw (h, w) uint16 including the halo; returns 3 planes."""
    raw = np.ascontiguousarray(raw, np.uint16)
    h, w = raw.shape
    outs = [np.empty((h, w), np.uint16) for _ in range(3)]
    bl = (ctypes.c_int32 * 4)(*[int(x) for x in black])
    ga = (ctypes.c_float * 4)(*[float(x) for x in gains])
    rc = _load().itp_process_u16(
        raw.ctypes.data, w, h, bl, ga, int(scale_7311),
        cfg.table1.ctypes.data, cfg.table2.ctypes.data,
        cfg.k1.ctypes.data, cfg.k2.ctypes.data, ctypes.byref(cfg.params),
        outs[0].ctypes.data, outs[1].ctypes.data, outs[2].ctypes.data,
    )
    if rc != 0:
        raise RuntimeError(f"itp_process_u16 failed: {rc}")
    return outs


def itp_full_image(raw, black, gains, scale_7311, cfg: ItpConfig,
                   tile=1024, halo=12, threads=6):
    """Whole-image ITP: edge extension, tiling and parallel tiles all in Rust."""
    raw = np.ascontiguousarray(raw, np.uint16)
    h, w = raw.shape
    outs = [np.zeros((h, w), np.uint16) for _ in range(3)]
    bl = (ctypes.c_int32 * 4)(*[int(x) for x in black])
    ga = (ctypes.c_float * 4)(*[float(x) for x in gains])
    rc = _load().itp_full_image_u16(
        raw.ctypes.data, w, h, bl, ga, int(scale_7311), ctypes.byref(cfg.params),
        outs[0].ctypes.data, outs[1].ctypes.data, outs[2].ctypes.data,
        int(tile), int(halo), int(threads),
    )
    if rc != 0:
        raise RuntimeError(f"itp_full_image_u16 failed: {rc}")
    return outs


def itp_debug_tile(raw, black, gains, scale_7311, stage, cfg: ItpConfig):
    """One intermediate ITP plane (f32) for a tile; for verification only."""
    raw = np.ascontiguousarray(raw, np.uint16)
    h, w = raw.shape
    out = np.empty((h, w), np.float32)
    bl = (ctypes.c_int32 * 4)(*[int(x) for x in black])
    ga = (ctypes.c_float * 4)(*[float(x) for x in gains])
    rc = _load().itp_debug_u16(
        raw.ctypes.data, w, h, bl, ga, int(scale_7311),
        cfg.table1.ctypes.data, cfg.table2.ctypes.data,
        cfg.k1.ctypes.data, cfg.k2.ctypes.data, ctypes.byref(cfg.params),
        int(stage), out.ctypes.data,
    )
    if rc != 0:
        raise RuntimeError(f"itp_debug_u16 failed: {rc}")
    return out


def marble_config(look, dev, dec, mode) -> MarbleConfig:
    """Parameter block for the Marble luma path (bd = sony_marble.Look)."""
    import sony_effect as E
    T = E.tables()
    lookb = np.frombuffer(bytes(look.b), np.uint8)
    if lookb.size < 0x200:            # Rust reads look+0x1000..0x11ff unchecked
        raise ValueError(f"Marble look struct needs 0x200 bytes, got {lookb.size:#x}")
    tabs = [np.ascontiguousarray(T[k], np.int16)
            for k in ("srgb_dec", "gamma_dec", "srgb_enc", "gamma_enc")]
    p = MarbleParams()
    p.mode = int(mode)
    p.d218 = int(dev["d218"])
    p.d224 = int(dev["d224"])
    p.d2c0 = int(dev["d2c0"])
    if dec is None:
        p.iso, p.f0, p.d14c, p.d1b0 = 0, 0, -999, 0
        p.f22780 = p.f22784 = 1.0
    else:
        p.iso = int(dec["iso"])
        p.f0 = int(dec["f0"])
        p.d14c = int(dec["d14c"])
        p.f22780 = float(dec["f22780"])
        p.f22784 = float(dec["f22784"])
        p.d1b0 = int(dec["d1b0"])
    p.look = lookb.ctypes.data
    p.srgb_dec, p.gamma_dec, p.srgb_enc, p.gamma_enc = (t.ctypes.data for t in tabs)
    mf = [int(x) for row in E.M_FWD[mode] for x in row]
    mi = [int(x) for row in E.M_INV[mode] for x in row]
    for i in range(9):
        p.m_fwd[i] = mf[i]
        p.m_inv[i] = mi[i]
    return MarbleConfig(params=p, keep=(lookb, *tabs))


def marble_slab(r, g, b, cfg: MarbleConfig):
    """Marble on one slab. Returns (planes, valid_region), or None if unsupported."""
    r = np.ascontiguousarray(r, np.uint16)
    g = np.ascontiguousarray(g, np.uint16)
    b = np.ascontiguousarray(b, np.uint16)
    h, w = r.shape
    outs = [np.empty((h, w), np.uint16) for _ in range(3)]
    rc = _load().marble_slab_u16(
        r.ctypes.data, g.ctypes.data, b.ctypes.data, w, h,
        ctypes.byref(cfg.params),
        outs[0].ctypes.data, outs[1].ctypes.data, outs[2].ctypes.data,
        -1, 0, None,
    )
    if rc == 0:
        return None
    if rc < 0:
        raise RuntimeError(f"marble_slab_u16 failed: {rc}")
    return outs, (16, 16, w - 16, h - 16)


def build_main_gamma(xs, ys, count, second):
    """MainGamma 65536-entry int16 LUT (same inputs as sony_look.build_main_gamma)."""
    x = np.ascontiguousarray(xs, np.int32).reshape(-1)
    y = np.ascontiguousarray(ys, np.int32).reshape(-1)
    s = np.ascontiguousarray(second, np.int32).reshape(-1)
    # Rust reads `count` knots and 1025 second-curve entries without bounds checks.
    if int(count) < 1:
        raise ValueError(f"MainGamma knot count must be >= 1, got {count}")
    if x.size < int(count) or y.size < int(count):
        raise ValueError(f"MainGamma needs {count} knots, got {x.size} x / {y.size} y")
    if s.size < 1025:
        raise ValueError(f"MainGamma second curve needs 1025 entries, got {s.size}")
    out = np.empty(0x10000, np.int16)
    rc = _load().build_main_gamma_i16(
        x.ctypes.data, y.ctypes.data, int(count), s.ctypes.data, out.ctypes.data)
    if rc != 0:
        raise RuntimeError(f"build_main_gamma_i16 failed: {rc}")
    return out


def effect_tables():
    """The four Effect colour-space tables (same keys as sony_effect.tables)."""
    outs = [np.empty(16384, np.int16) for _ in range(4)]
    rc = _load().effect_tables_i16(*(a.ctypes.data for a in outs))
    if rc != 0:
        raise RuntimeError(f"effect_tables_i16 failed: {rc}")
    return dict(gamma_dec=outs[0], gamma_enc=outs[1], srgb_enc=outs[2], srgb_dec=outs[3])


def sscs_config(p) -> SscsConfig:
    """Parameter block for sony_sscs.sscs_int (the dict from params_from_tags)."""
    q = SscsParams()
    q.t = int(p["T"])
    q.tr = int(p["tR"])
    q.tg = int(p["tG"])
    q.tb = int(p["tB"])
    q.wr, q.wg, q.wb = (int(v) for v in p["w"])
    return SscsConfig(params=q)


def sscs_u16(z, cfg: SscsConfig):
    """SSCS integer path on an interleaved (H, W, 3) uint16 band."""
    z = np.ascontiguousarray(z, np.uint16)
    if z.ndim != 3 or z.shape[2] != 3:
        raise ValueError("sscs_u16 expects (H, W, 3)")
    out = np.empty_like(z)
    n = z.shape[0] * z.shape[1]
    rc = _load().sscs_int_u16(
        z.ctypes.data, n, 3, ctypes.byref(cfg.params), out.ctypes.data, 3)
    if rc != 0:
        raise RuntimeError(f"sscs_int_u16 failed: {rc}")
    return out


def marble_debug_slab(r, g, b, stage, plane, cfg: MarbleConfig):
    """One intermediate Marble plane (f32) for verification only."""
    r = np.ascontiguousarray(r, np.uint16)
    g = np.ascontiguousarray(g, np.uint16)
    b = np.ascontiguousarray(b, np.uint16)
    h, w = r.shape
    out = np.empty((h, w), np.float32)
    dummy = np.empty((h, w), np.uint16)
    rc = _load().marble_slab_u16(
        r.ctypes.data, g.ctypes.data, b.ctypes.data, w, h,
        ctypes.byref(cfg.params),
        dummy.ctypes.data, dummy.ctypes.data, dummy.ctypes.data,
        int(stage), int(plane), out.ctypes.data,
    )
    if rc != 1:
        raise RuntimeError(f"marble debug stage {stage} failed: {rc}")
    return out
