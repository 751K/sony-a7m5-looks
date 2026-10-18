//! Fused pointwise front of the Sony look chain, bit-for-bit equal to the
//! NumPy reference in `sonylook/` (which is itself verified against the ARM64
//! code under Unicorn).
//!
//! Scope: blur_rows -> hue_index -> LinearMatrix16 -> MainGamma -> RGB2YCC ->
//! AreaComp -> ChromaSuppress, Sharpness -> YGamma -> Spica -> YCC2RGB,
//! ZcTaskGeoCorr, ZcTaskITP (whole image or one tile per call, in `itp`), ZcTaskMarble (one
//! slab per call, luma path, in `marble`), ZcTaskSSCS (integer path, in
//! `sscs`), and the MainGamma / Effect table construction (in `tables`).  Input
//! is the YCC-stage uint16 band (decoder path), output is the Y / Cr / Cb
//! planes that the Python band loop feeds to Marble/Effect.
//!
//! All float32 operations keep the original order.  Fused multiply-adds use
//! `f32::mul_add`, which lowers to the hardware FMA on aarch64 and therefore
//! has the same single rounding as the ARM `fmadd` instruction.

#![allow(clippy::too_many_arguments)]

mod itp;
mod marble;
mod sscs;
mod tables;

use std::slice;

#[repr(C)]
pub struct FrontParams {
    pub table: *const f32, // 1024 x 18
    pub gamma: *const i16, // 65536, index 0x8000 is input 0
    pub atan: *const f32,  // 512
    pub luma: [i32; 3],    // 0x7801 raw values
    pub ycc: [i32; 8],     // 0x7842 + 0x7844 selected parameters
    pub area: [f32; 16],   // FUN_1000b3ee8 parameters
    pub area_flag: i32,
    pub thr: [f32; 3], // LM16 quadratic dead-zone thresholds
    pub w9: i64,
    pub w12: i64,
    pub w8: i64,
    pub w11: i64,
}

#[inline(always)]
fn i16v(v: i64) -> i32 {
    (v as u16 as i16) as i32
}

/// `_shear` from sony_ycc.py: 9-bit field, sign-extended.
#[inline(always)]
fn shear(v: i32) -> f32 {
    let u = (((v as u32) & 0xffff) >> 2) & 0x1ff;
    let s = if u < 0x100 {
        u as i32
    } else {
        u as i32 - 0x200
    };
    s as f32
}

/// Horizontal [1 8 14 8 1]/32 blur with the same edge handling as blur_rows().
#[inline]
fn blur_row(src: &[u16], dst: &mut [f32], w: usize) {
    #[inline(always)]
    fn s(src: &[u16], i: usize) -> f32 {
        src[i] as f32
    }
    const K: f32 = 1.0 / 32.0;
    dst[0] = s(src, 1).mul_add(8.0, s(src, 0).mul_add(23.0, s(src, 2))) * K;
    dst[1] = s(src, 2).mul_add(
        8.0,
        s(src, 1).mul_add(14.0, s(src, 0).mul_add(9.0, s(src, 3))),
    ) * K;
    for x in 2..w - 2 {
        let s6 = s(src, x + 1) + s(src, x - 1);
        let s5 = s(src, x).mul_add(14.0, s(src, x - 2)) + s(src, x + 2);
        dst[x] = s6.mul_add(8.0, s5) * K;
    }
    dst[w - 2] = (s(src, w - 1) + s(src, w - 3)).mul_add(
        8.0,
        s(src, w - 2).mul_add(14.0, s(src, w - 4)) + s(src, w - 1),
    ) * K;
    dst[w - 1] = s(src, w - 1).mul_add(23.0, s(src, w - 2).mul_add(8.0, s(src, w - 3))) * K;
}

#[inline(always)]
fn pack_chroma(v: f32) -> u16 {
    let i = (v + 32768.0).trunc() as i64;
    i.clamp(0, 0xffff) as u16
}

#[inline(always)]
fn scale_chroma(v: u16, f: i32) -> u16 {
    let t = (v as i32 - 0x8000).wrapping_mul(f);
    let t = if t < 0 { t.wrapping_add(0xff) } else { t };
    ((((t as u32) >> 8) ^ 0x8000) & 0xffff) as u16
}

#[repr(C)]
pub struct SpatialParams {
    pub sharp_thr: f32,
    pub sharp_s0: f32,
    pub sharp_s1: f32,
    pub sharp_s9: f32,
    pub sharp_k: f32,
    pub ylut: *const i16, // 65536
    pub ygamma_off: i32,
    pub ygamma_gain: f32,
    pub ygamma_ac: i32,
    pub ygamma_b0: i32,
    pub patterns: *const i32, // 256 x 3: class, dx, dy
    pub coefs: *const f32,    // 100 x 26
    pub spica_thr: u64,
    pub spica_full: i32,
    pub spica_gain_on: i32,
    pub spica_s: f32,
    pub spica_k2: f32,
    pub dev208: i32,
    pub dev2c4: i32,
    // S dict: 0x44..0x58, 0x64..0x78, 0x84..0x98, 0xa4..0xb8, 0xc4
    pub s: [f32; 25],
}

// The pointer fields point at tables that are immutable for the duration of a
// render call; sharing them across Spica's row threads is safe.
unsafe impl Sync for SpatialParams {}

const CROSS: [(i32, i32); 9] = [
    (0, -2),
    (0, -1),
    (-2, 0),
    (-1, 0),
    (0, 0),
    (1, 0),
    (2, 0),
    (0, 1),
    (0, 2),
];

const TAPS: [(i32, i32); 25] = [
    (0, -3),
    (-1, -2),
    (0, -2),
    (1, -2),
    (-2, -1),
    (-1, -1),
    (0, -1),
    (1, -1),
    (2, -1),
    (-3, 0),
    (-2, 0),
    (-1, 0),
    (0, 0),
    (1, 0),
    (2, 0),
    (3, 0),
    (-2, 1),
    (-1, 1),
    (0, 1),
    (1, 1),
    (2, 1),
    (-1, 2),
    (0, 2),
    (1, 2),
    (0, 3),
];

/// ZcTaskSharpness 0x1000ef77c, on the whole band minus the 2-px task border.
fn sharpness(y: &[u16], w: usize, h: usize, p: &SpatialParams) -> Vec<u16> {
    let mut out = y.to_vec();
    if w < 5 || h < 5 {
        return out;
    }
    let hr = h - 4;
    let wr = w - 4;
    let mut src = vec![0.0f32; hr * wr];
    for yy in 0..hr {
        for xx in 0..wr {
            src[yy * wr + xx] = y[(yy + 2) * w + xx + 2] as f32;
        }
    }
    let wts = [1.0f32, 6.0, 15.0, 20.0, 15.0, 6.0, 1.0];
    let mut hb = vec![0.0f32; hr * wr];
    for yy in 0..hr {
        for xx in 0..wr {
            let mut acc = 0.0f32;
            for o in -3i32..=3 {
                let sx = xx as i32 + o;
                if sx >= 0 && sx < wr as i32 {
                    acc = src[yy * wr + sx as usize].mul_add(wts[(3 - o) as usize], acc);
                }
            }
            hb[yy * wr + xx] = acc;
        }
    }
    let mut cab = vec![0.0f32; hr * wr];
    for yy in 0..hr {
        for xx in 0..wr {
            let mut acc = 0.0f32;
            for o in -3i32..=3 {
                let sy = yy as i32 + o;
                if sy >= 0 && sy < hr as i32 {
                    acc = hb[sy as usize * wr + xx].mul_add(wts[(3 - o) as usize], acc);
                }
            }
            cab[yy * wr + xx] = acc;
        }
    }
    for yy in 0..hr {
        let cy = yy + 2;
        for xx in 0..wr {
            let cx = xx + 2;
            let l = (4 * y[cy * w + cx] as i64
                + y[(cy - 1) * w + cx] as i64
                + y[(cy + 1) * w + cx] as i64
                + y[cy * w + cx - 1] as i64
                + y[cy * w + cx + 1] as i64) as f32;
            let s = src[yy * wr + xx];
            let mut s7 = cab[yy * wr + xx] * -0.000244140625f32;
            s7 = l.mul_add(0.125f32, s7);
            let s16 = (-l).mul_add(0.125f32, s);
            let d = s7.mul_add(p.sharp_s9, s16);
            let e = if d > p.sharp_thr {
                p.sharp_s0 * d
            } else if d < -p.sharp_thr {
                p.sharp_s1 * d
            } else {
                0.0
            };
            let mut o = p.sharp_k.mul_add(e, s);
            if o < 0.0 {
                o = 0.0;
            } else if o > 32767.0 {
                o = 32767.0;
            }
            out[cy * w + cx] = o.trunc() as i64 as u16;
        }
    }
    out
}

/// ZcTaskYGamma 0x1000feb68 (no exposure compensation in this path).
fn ygamma(y: &mut [u16], p: &SpatialParams) {
    let w13 = if p.ygamma_ac < 0 {
        p.ygamma_ac + 3
    } else {
        p.ygamma_ac
    };
    let nq = i16v((-(w13 >> 2)) as i64);
    let s2 = (nq as f32) * 63.998047f32;
    let s3 = 1.0f32 / ((0x200 - p.ygamma_b0 - nq) as f32);
    let s0 = i16v(p.ygamma_off as i64) as f32;
    let s1 = p.ygamma_gain;
    let lut = unsafe { slice::from_raw_parts(p.ylut, 0x10000) };
    for v in y.iter_mut() {
        let x = lut[0x8000 + *v as usize] as u16 as f32;
        let s7 = (x - s2) * 512.0f32;
        let s6 = if s2 < x { s7 } else { 0.0 };
        let s6 = s6.mul_add(s3, -s0);
        let s6 = s6.mul_add(s1, s0);
        let s6 = if s6 < 0.0 {
            0.0
        } else if s6 > 16383.0 {
            16383.0
        } else {
            s6
        };
        *v = s6.trunc() as i64 as u16;
    }
}

#[inline(always)]
fn spica_side(p: f32, s: &[f32; 25], b: usize) -> f32 {
    let a = s[b];
    let a2 = s[b + 1];
    let bb = s[b + 2];
    let b2 = s[b + 3];
    let y0 = s[b + 4];
    let y1 = s[b + 5];
    let mut r = y0;
    if p >= a && p < a2 {
        r = ((y0 - y1) * (a2 - p)) / (a2 - a) + y1;
    }
    if p >= a2 {
        r = if p >= bb {
            if p < b2 {
                ((y1 - y0) * (b2 - p)) / (b2 - bb) + y0
            } else {
                y0
            }
        } else {
            y1
        };
    }
    r
}

#[inline(always)]
fn spica_curve(p: f32, s: &[f32; 25], b: usize) -> f32 {
    let a = s[b];
    let a2 = s[b + 1];
    let bb = s[b + 2];
    let b2 = s[b + 3];
    let y0 = s[b + 4];
    let y1 = s[b + 5];
    let mut r = y0;
    let lo = p >= a;
    if lo && p < a2 {
        r = ((y0 - y1) * (a2 - p)) / (a2 - a) + y1;
    }
    if lo && p >= a2 {
        r = if p >= bb {
            if p < b2 {
                ((y1 - y0) * (b2 - p)) / (b2 - bb) + y0
            } else {
                y0
            }
        } else {
            y1
        };
    }
    r
}

#[inline(always)]
fn spica_gain(d: f32, rng: f32, mid: f32, p: &SpatialParams) -> f32 {
    if p.spica_gain_on == 0 {
        return 512.0;
    }
    let g1 = if d > 0.0 {
        spica_side(d, &p.s, 0)
    } else {
        spica_side(-d, &p.s, 6)
    };
    let g2 = spica_curve(rng, &p.s, 12);
    let g3 = spica_curve(mid, &p.s, 18);
    let m = (g1 as i64).min(g2 as i64).min(g3 as i64);
    (p.s[24] * 0.00048828125f32) * (m as f32)
}

unsafe fn spica_rows(
    y: &[u16],
    w: usize,
    out_rows: &mut [u16],
    yy0: usize,
    nrows: usize,
    ww: usize,
    p: &SpatialParams,
    patterns: &[i32],
    coefs: &[f32],
    k: f32,
) {
    for yr in 0..nrows {
        let yy = yy0 + yr;
        let row_out = yr * w;
        for xx in 0..ww {
            let cy = yy + 3;
            let cx = xx + 3;
            let mut vals = [0i32; 9];
            let mut mn = i32::MAX;
            let mut mx = i32::MIN;
            for i in 0..9 {
                let (dx, dy) = CROSS[i];
                let idx = ((cy as i32 + dy) as usize) * w + (cx as i32 + dx) as usize;
                let v = *y.get_unchecked(idx) as i32;
                vals[i] = v;
                if v < mn {
                    mn = v;
                }
                if v > mx {
                    mx = v;
                }
            }
            let rng = mx - mn;
            let half = rng / 2;
            let mut bits = 0i32;
            for i in 0..9 {
                if half <= vals[i] - mn {
                    bits |= 1 << i;
                }
            }
            let low = bits & 0xff;
            let mut idx = (if bits & 0x100 != 0 {
                (!low) & 0xff
            } else {
                low
            }) as usize;
            if (rng as u64) < p.spica_thr {
                idx = 0;
            }
            let ci = *patterns.get_unchecked(idx * 3) as usize;
            let dxs = *patterns.get_unchecked(idx * 3 + 1);
            let dys = *patterns.get_unchecked(idx * 3 + 2);
            // One load per tap: the four quadrant variants differ only by the
            // sign of the row/column offset, which is constant for this pixel.
            let cbase = ci * 26 + 1;
            let cx_i = cx as i32;
            let cy_i = cy as i32;
            let mut acc = 0.0f32;
            for t in 0..25 {
                let (a, b) = TAPS[t];
                let ry = if dys < 0 { -b } else { b };
                let rx = if dxs < 0 { -a } else { a };
                let idx = ((cy_i + ry) as usize) * w + (cx_i + rx) as usize;
                let val = *y.get_unchecked(idx) as f32;
                let ct = *coefs.get_unchecked(cbase + t);
                acc = if t == 0 {
                    ct * val
                } else {
                    ct.mul_add(val, acc)
                };
            }
            let yc = *y.get_unchecked(cy * w + cx) as i32;
            let mut t_ = if p.spica_full == 0 {
                (acc as i64) >> 9
            } else {
                let d = (acc - ((yc << 9) as f32)) * p.spica_s;
                let mid = ((mn + mx) / 2) as f32;
                let g = spica_gain(d * 0.0078125f32, rng as f32, mid, p);
                let m = 0.001953125f32 * (g * 0.001953125f32);
                (d * m) as i64 + yc as i64
            };
            t_ = t_.min(0x3fff).max(0);
            let ycv = yc as f32;
            let o = k.mul_add(((t_ as i16) as f32) - ycv, ycv);
            let o = o.min(32767.0).max(0.0);
            *out_rows.get_unchecked_mut(row_out + cx) = o.trunc() as i64 as u16;
        }
    }
}

/// ZcTaskSpica 0x100100b60 on the sub-band [y0, y1).
///
/// Rows are independent (the 3-px support is inside the sub-band), so large
/// bands are split across scoped threads.  Set SONYLOOK_THREADS to cap them.
unsafe fn spica(y: &[u16], w: usize, h: usize, p: &SpatialParams, threads_arg: usize) -> Vec<u16> {
    let mut out = y.to_vec();
    if w < 7 || h < 7 {
        return out;
    }
    let ww = w - 6;
    let hh = h - 6;
    let patterns = slice::from_raw_parts(p.patterns, 256 * 3);
    let coefs = slice::from_raw_parts(p.coefs, 100 * 26);
    let k = (p.spica_k2 * 0.0002f32) * ((p.dev208 + 100) as f32) * ((100 - p.dev2c4) as f32);
    let start = 3 * w;
    let threads = if threads_arg > 0 {
        threads_arg
    } else {
        std::env::var("SONYLOOK_THREADS")
            .ok()
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(6)
    };
    let threads = threads.max(1).min(if hh >= 64 { 6 } else { 1 });
    if threads == 1 {
        spica_rows(
            y,
            w,
            &mut out[start..start + hh * w],
            0,
            hh,
            ww,
            p,
            patterns,
            coefs,
            k,
        );
    } else {
        let chunk = (hh + threads - 1) / threads;
        std::thread::scope(|s| {
            let mut yy0 = 0usize;
            for c in out[start..start + hh * w].chunks_mut(chunk * w) {
                let nr = c.len() / w;
                let y0 = yy0;
                yy0 += nr;
                s.spawn(move || spica_rows(y, w, c, y0, nr, ww, p, patterns, coefs, k));
            }
        });
    }
    out
}

#[inline(always)]
fn div10000(x: i64) -> i64 {
    let q = x.abs() / 10000;
    if x < 0 {
        -q
    } else {
        q
    }
}

/// ZcTaskYCC2RGB 0x1000fb500.
fn ycc2rgb(
    y: &[u16],
    cr: &[u16],
    cb: &[u16],
    r_out: &mut [u16],
    g_out: &mut [u16],
    b_out: &mut [u16],
) {
    for i in 0..y.len() {
        let yy = y[i] as i64;
        let crv = cr[i] as i64 - 0x8000;
        let cbv = cb[i] as i64 - 0x8000;
        let r = div10000(crv * 0x36c4 + yy * 10000);
        let g = div10000(crv * -0x1be5 + yy * 10000 + cbv * -0xd71);
        let b = div10000(cbv * 0x4538 + yy * 10000);
        r_out[i] = r.clamp(0, 0x3fff) as u16;
        g_out[i] = g.clamp(0, 0x3fff) as u16;
        b_out[i] = b.clamp(0, 0x3fff) as u16;
    }
}

/// Sharpness -> YGamma -> Spica -> YCC2RGB for one band.
///
/// `c0`/`ya`/`yb`/`g0`/`g1` are the original band coordinates from
/// Look.render: the band is [c0, c1), the output rows are [ya, yb), and Spica
/// runs on [g0, g1).  The returned planes have (yb-ya) rows.
#[no_mangle]
pub unsafe extern "C" fn fused_spatial_u16(
    y_in: *const u16,
    cr_in: *const u16,
    cb_in: *const u16,
    width: usize,
    band_h: usize,
    c0: usize,
    ya: usize,
    yb: usize,
    g0: usize,
    g1: usize,
    threads: usize,
    params: *const SpatialParams,
    r_out: *mut u16,
    g_out: *mut u16,
    b_out: *mut u16,
) -> i32 {
    if width < 7 || band_h < 7 || params.is_null() || yb <= ya || g1 <= g0 {
        return -1;
    }
    let p = &*params;
    let n = width * band_h;
    let y = slice::from_raw_parts(y_in, n);
    let cr = slice::from_raw_parts(cr_in, n);
    let cb = slice::from_raw_parts(cb_in, n);

    let sharp = sharpness(y, width, band_h, p);
    let mut yg = sharp;
    ygamma(&mut yg, p);

    let sg0 = g0 - c0;
    let sg1 = g1 - c0;
    let sub = &yg[sg0 * width..sg1 * width];
    let sp = spica(sub, width, sg1 - sg0, p, threads);

    // spica() was run on the sub-band [g0, g1), so its rows are offset by g0;
    // cr/cb still belong to the chain band [c0, c1).
    let sy0 = ya - g0;
    let sy1 = yb - g0;
    let oy = &sp[sy0 * width..sy1 * width];
    let oy0 = ya - c0;
    let oy1 = yb - c0;
    let ocr = &cr[oy0 * width..oy1 * width];
    let ocb = &cb[oy0 * width..oy1 * width];
    let nout = (oy1 - oy0) * width;
    let r = slice::from_raw_parts_mut(r_out, nout);
    let g = slice::from_raw_parts_mut(g_out, nout);
    let b = slice::from_raw_parts_mut(b_out, nout);
    ycc2rgb(oy, ocr, ocb, r, g, b);
    0
}

#[inline(always)]
fn area_edge(a: f32, b: f32, p: &[f32; 16], c0: usize, c1: usize, k0: usize, k1: usize) -> f32 {
    let x0 = p[c0] + a;
    let x1 = p[c1] + b;
    let v = x1.mul_add(p[k1], x0 * p[k0]);
    if v <= 0.0 {
        0.0
    } else {
        v
    }
}

/// Process one band.  Returns 0 on success, -1 for an unusable width.
#[no_mangle]
pub unsafe extern "C" fn fused_front_u16(
    r_ptr: *const u16,
    g_ptr: *const u16,
    b_ptr: *const u16,
    width: usize,
    height: usize,
    in_stride: usize,
    params: *const FrontParams,
    y_out: *mut u16,
    cr_out: *mut u16,
    cb_out: *mut u16,
) -> i32 {
    if width < 5 || height == 0 || in_stride == 0 || params.is_null() {
        return -1;
    }
    let p = &*params;
    let table = slice::from_raw_parts(p.table, 1024 * 18);
    let gamma = slice::from_raw_parts(p.gamma, 0x10000);
    let atan = slice::from_raw_parts(p.atan, 512);

    // --- constants derived once ------------------------------------------------
    let c0 = i16v(((p.luma[0] as i64) << 7) & 0xffff);
    let c1 = i16v(((p.luma[1] as i64) << 7) & 0xffff);
    let c2 = i16v(((p.luma[2] as i64) << 7) & 0xffff);

    let yp = [
        i16v(p.ycc[0] as i64),
        i16v(p.ycc[1] as i64),
        i16v(p.ycc[2] as i64),
        i16v(p.ycc[3] as i64),
        i16v(p.ycc[4] as i64),
        i16v(p.ycc[5] as i64),
        i16v(p.ycc[6] as i64),
        i16v(p.ycc[7] as i64),
    ];
    let d8 = shear(yp[1]);
    let d9 = shear(yp[3]);
    let d10 = shear(yp[0]);
    let d11 = shear(yp[2]);
    const C256: f32 = 0.00390625; // 1/256
    const G256: f32 = 0.015625; // 1/64
    let g_cr_pos = (((yp[5] as u32) & 0xffff) >> 3 & 0xff) as f32 * G256;
    let g_cr_neg = (((yp[7] as u32) & 0xffff) >> 3 & 0xff) as f32 * G256;
    let g_cb_pos = (((yp[4] as u32) & 0xffff) >> 3 & 0xff) as f32 * G256;
    let g_cb_neg = (((yp[6] as u32) & 0xffff) >> 3 & 0xff) as f32 * G256;

    const C_Q: f32 = f32::from_bits(0x3880_0000); // 6.1035156e-05

    let mut br = vec![0.0f32; width];
    let mut bg = vec![0.0f32; width];
    let mut bb = vec![0.0f32; width];
    let mut rrow = vec![0u16; width];
    let mut grow = vec![0u16; width];
    let mut brow = vec![0u16; width];

    for y in 0..height {
        let off = y * width * in_stride;
        for x in 0..width {
            let i = off + x * in_stride;
            rrow[x] = *r_ptr.add(i);
            grow[x] = *g_ptr.add(i);
            brow[x] = *b_ptr.add(i);
        }
        blur_row(&rrow, &mut br, width);
        blur_row(&grow, &mut bg, width);
        blur_row(&brow, &mut bb, width);
        let off = y * width;

        let yo = slice::from_raw_parts_mut(y_out.add(off), width);
        let cro = slice::from_raw_parts_mut(cr_out.add(off), width);
        let cbo = slice::from_raw_parts_mut(cb_out.add(off), width);

        for x in 0..width {
            let r = rrow[x] as f32;
            let g = grow[x] as f32;
            let b = brow[x] as f32;

            // --- hue index (0x1000abe38) -----------------------------------
            let luma64 = (((br[x] as f64) * 109.0 + (bg[x] as f64) * 366.0)
                + (bb[x] as f64) * 37.0)
                * (1.0 / 512.0);
            let yv = luma64 as f32;
            let a = br[x] - yv;
            let bc = bb[x] - yv;
            let fa = a.abs();
            let fb = bc.abs();
            let big_a = fa > fb;
            let a_pos = !(a < 0.0);
            let b_pos = !(bc < 0.0);
            let offh = if big_a {
                if a_pos {
                    if b_pos {
                        -256.0
                    } else {
                        256.0
                    }
                } else if b_pos {
                    768.0
                } else {
                    -768.0
                }
            } else if a_pos {
                if b_pos {
                    0.0
                } else {
                    -512.0
                }
            } else if b_pos {
                -1024.0
            } else {
                512.0
            };
            let num = if big_a { fb } else { fa } as f64;
            let den = if big_a {
                if a == 0.0 {
                    1.0
                } else {
                    fa as f64
                }
            } else if bc == 0.0 {
                1.0
            } else {
                fb as f64
            };
            let mut ii = (num * 512.0 / den + 0.5).trunc() as i64;
            if ii > 0x1ff {
                ii = 0x1ff;
            }
            if ii < 0 {
                ii = 0;
            }
            let hue = (atan[ii as usize] + offh).abs();

            // --- LinearMatrix16 (0x1000ac6dd4) ------------------------------
            let mut hi = hue.trunc() as i64;
            if hi < 0 || hi > 0x3fe {
                hi = 0x3ff;
            }
            let t = &table[(hi as usize) * 18..(hi as usize) * 18 + 18];
            let sq0 = r * r;
            let sq1 = g * g;
            let sq2 = b * b;
            let mut enc = [0i32; 3];
            for row in 0..3 {
                let b0 = t[3 * row];
                let b1 = t[3 * row + 1];
                let b2 = t[3 * row + 2];
                let n0 = t[9 + 3 * row];
                let n1 = t[9 + 3 * row + 1];
                let n2 = t[9 + 3 * row + 2];
                let lin = b2.mul_add(b, b1.mul_add(g, b0 * r));
                let q = n2.mul_add(sq2, n1.mul_add(sq1, n0 * sq0)) * C_Q;
                let s = q + lin;
                let thr = p.thr[row];
                let v = if q < 0.0 {
                    lin
                } else if q >= thr {
                    s - thr
                } else if q <= -thr {
                    s + thr
                } else {
                    lin
                };
                let v = if v < 0.0 {
                    0.0
                } else if v > 16383.0 {
                    16383.0
                } else {
                    v
                };

                // --- MainGamma (0x1000e96a0) -------------------------------
                let gi = v.trunc() as usize;
                let frac = v - v.floor();
                let t0 = gamma[0x8000 + gi] as f32;
                let t1 = gamma[0x8000 + gi + 1] as f32;
                let o = (t1 - t0).mul_add(frac, t0);
                enc[row] = (o.trunc() as i32 as i16) as i32;
            }

            // --- RGB2YCC (0x1000ee0cc) -------------------------------------
            let rr = enc[0];
            let gg = enc[1];
            let bbb = enc[2];
            let sum =
                (rr as i64) * (c0 as i64) + (gg as i64) * (c1 as i64) + (bbb as i64) * (c2 as i64);
            let yv16 = (((sum as u32) >> 13) & 0xffff) as u16;

            let rg = rr - gg;
            let bgc = bbb - gg;
            let s3 = rg as f32;
            let s4 = bgc as f32;
            let k_cb = if rg < 0 { d9 * C256 } else { d8 * C256 };
            let cb0 = k_cb.mul_add(s3, s4);
            let k_cr = if bgc < 0 { d11 * C256 } else { d10 * C256 };
            let cr0 = k_cr.mul_add(s4, s3);
            let gcr = if cr0 < 0.0 { g_cr_neg } else { g_cr_pos };
            let gcb = if cb0 < 0.0 { g_cb_neg } else { g_cb_pos };
            let mut cr = (cr0 * 0.5) * gcr;
            let mut cb = (cb0 * 0.5) * gcb;
            cr = cr.min(8191.0).max(-8192.0);
            cb = cb.min(8191.0).max(-8192.0);
            let mut crp = pack_chroma(cr);
            let mut cbp = pack_chroma(cb);

            // --- AreaComp (0x1000b3ee8 flag-1 branch) ----------------------
            if p.area_flag == 1 {
                let a16 = (crp as i32 - 0x8000) as f32;
                let b16 = (cbp as i32 - 0x8000) as f32;
                let d0 = area_edge(a16, b16, &p.area, 0, 1, 4, 6);
                let d1 = area_edge(a16, b16, &p.area, 0, 1, 5, 7);
                let d2 = area_edge(a16, b16, &p.area, 2, 3, 8, 10);
                let d3 = area_edge(a16, b16, &p.area, 2, 3, 9, 11);
                let m = d0.min(d1).min(d2).min(d3);
                let o1 = a16 as f64 + 32768.0 + (m * p.area[12]) as f64;
                let o2 = b16 as f64 + 32768.0 + (m * p.area[13]) as f64;
                crp = ((o1.trunc() as i64) & 0xffff) as u16;
                cbp = ((o2.trunc() as i64) & 0xffff) as u16;
            }

            // --- ChromaSuppress (0x1000e6da0) ------------------------------
            let ys = (yv16 as i16) as i64;
            let lo = (p.w9 - ys) as i32;
            let hiw = (ys - p.w12) as i32;
            let m = if lo > 0 {
                lo.wrapping_mul(p.w8 as i32)
            } else {
                hiw.wrapping_mul(p.w11 as i32)
            };
            let mut f = 0xff - (m >> 12);
            f = f.max(0);
            f = f.min(0xff);
            if !(lo > 0 || hiw > 0) {
                f = 0xff;
            }
            let yo16 = if ys > 0 { yv16 } else { 0 };
            yo[x] = yo16;
            cro[x] = scale_chroma(crp, f);
            cbo[x] = scale_chroma(cbp, f);
        }
    }
    0
}

// ---------------------------------------------------------------------------
// ZcTaskGeoCorr: distortion + lateral CA, u16 bilinear path.
// Mirrors sony_lens.geocorr_u16 / _coords_base / _channel_coords / bilinear_u16.

#[repr(C)]
pub struct LensParams {
    pub cx: f32,
    pub cy: f32,
    pub s: f32,
    pub s2: f32,
    pub step: f32,
    pub n: i32,
    pub table_r: *const f32,
    pub table_g: *const f32,
    pub table_b: *const f32,
    /// off[k] = (x, y), k = 0 R, 1 G, 2 B
    pub off: [f32; 6],
}

#[inline(always)]
unsafe fn bilinear_u16(plane: *const u16, pw: usize, ph: usize, fx: f32, fy: f32) -> u16 {
    let ix = fx.trunc() as i64;
    let iy = fy.trunc() as i64;
    let wmax = pw as i64 - 1;
    let hmax = ph as i64 - 1;
    let x0 = if ix <= wmax { ix.max(0) } else { wmax };
    let x1 = if x0 + 1 < wmax { x0 + 1 } else { wmax };
    let y0 = if iy <= hmax { iy.max(0) } else { hmax };
    let y1 = if y0 + 1 < hmax { y0 + 1 } else { hmax };
    let v00 = *plane.add(y0 as usize * pw + x0 as usize) as f32;
    let v01 = *plane.add(y0 as usize * pw + x1 as usize) as f32;
    let v10 = *plane.add(y1 as usize * pw + x0 as usize) as f32;
    let v11 = *plane.add(y1 as usize * pw + x1 as usize) as f32;
    let ax = fx - x0 as f32;
    let bx = x1 as f32 - fx;
    let top = bx.mul_add(v00, ax * v01);
    let bot = ax.mul_add(v11, bx * v10);
    let v = bot.mul_add(fy - y0 as f32, top * (y1 as f32 - fy));
    (v.trunc() as i64 & 0xffff) as u16
}

#[no_mangle]
pub unsafe extern "C" fn geocorr_u16_rust(
    r_in: *const u16,
    g_in: *const u16,
    b_in: *const u16,
    pw: usize,
    ph: usize,
    origin_x: i32,
    origin_y: i32,
    x0: i32,
    y0: i32,
    x1: i32,
    y1: i32,
    params: *const LensParams,
    r_out: *mut u16,
    g_out: *mut u16,
    b_out: *mut u16,
) -> i32 {
    if params.is_null() || pw == 0 || ph == 0 || x1 <= x0 || y1 <= y0 {
        return -1;
    }
    let p = &*params;
    let n = p.n as usize;
    if n < 2 {
        return -1;
    }
    let tr = slice::from_raw_parts(p.table_r, n);
    let tg = slice::from_raw_parts(p.table_g, n);
    let tb = slice::from_raw_parts(p.table_b, n);
    let tabs = [tr, tg, tb];
    let planes = [r_in, g_in, b_in];
    let outs = [r_out, g_out, b_out];
    let ow = (x1 - x0) as usize;
    let oh = (y1 - y0) as usize;
    let oxf = origin_x as f32;
    let oyf = origin_y as f32;

    for yy in 0..oh {
        let py = y0 as f32 + yy as f32;
        let dy = py - p.cy;
        for xx in 0..ow {
            let px = x0 as f32 + xx as f32;
            let dx = px - p.cx;
            let r = dy.mul_add(dy, dx * dx).sqrt();
            let t = (p.s2 * r) / p.step;
            let i = t.max(0.0).floor() as i64;
            let inside = i < (p.n - 1) as i64;
            let ic = i.clamp(0, (p.n - 2) as i64) as usize;
            let frac = t - ic as f32;
            let o = yy * ow + xx;
            for k in 0..3 {
                let tab = tabs[k];
                let a = tab[ic];
                let b = tab[ic + 1];
                let f = if inside {
                    (b - a).mul_add(frac, a)
                } else {
                    tab[n - 1]
                };
                let offx = p.off[2 * k];
                let offy = p.off[2 * k + 1];
                let termx = f * ((offx + px) - p.cx);
                let sx = termx.mul_add(p.s, p.cx);
                let termy = f * ((offy + py) - p.cy);
                let sy = termy.mul_add(p.s, p.cy);
                let v = bilinear_u16(planes[k], pw, ph, sx - oxf, sy - oyf);
                *outs[k].add(o) = v;
            }
        }
    }
    0
}
