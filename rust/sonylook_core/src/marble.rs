//! ZcTaskMarble, one slab at a time.
//!
//! Direct port of sonylook/sony_marble.py for the luma path (dev+0x2c0 != 0).
//! The chroma path (NR on, look+0x109c != 0) and the rare _strength_470 branch
//! are not ported: the FFI returns 0 and the caller falls back to NumPy.
//!
//! All integer operations are wrapping 32/64-bit as in NumPy; f32/f64 fused
//! multiply-adds use the hardware FMA (same single rounding as the ARM code).

use std::slice;

#[repr(C)]
pub struct MarbleParams {
    pub mode: i32,
    pub d218: i32,
    pub d224: i32,
    pub d2c0: i32,
    pub iso: i32,
    pub f0: i32,
    pub d14c: i32,
    pub f22780: f32,
    pub f22784: f32,
    pub d1b0: i32,
    pub look: *const u8, // 0x200 bytes, look struct from +0x1000
    pub srgb_dec: *const i16,
    pub gamma_dec: *const i16,
    pub srgb_enc: *const i16,
    pub gamma_enc: *const i16,
    pub m_fwd: [i32; 9],
    pub m_inv: [i32; 9],
}

const RECIP25: [i64; 26] = [
    32768, 32768, 16384, 10923, 8192, 6554, 5461, 4681, 4096, 3641, 3277, 2979, 2731, 2521, 2341,
    2185, 2048, 1928, 1820, 1725, 1638, 1560, 1489, 1425, 1365, 1311,
];
const LUMA_STRENGTH: [i64; 11] = [0, 20, 44, 68, 92, 117, 142, 167, 192, 217, 242];

struct Look<'a> {
    b: &'a [u8],
}

impl<'a> Look<'a> {
    #[inline(always)]
    fn s16(&self, off: usize) -> i32 {
        let i = off - 0x1000;
        i16::from_le_bytes([self.b[i], self.b[i + 1]]) as i32
    }
    #[inline(always)]
    fn u16(&self, off: usize) -> u32 {
        let i = off - 0x1000;
        u16::from_le_bytes([self.b[i], self.b[i + 1]]) as u32
    }
    #[inline(always)]
    fn u8(&self, off: usize) -> u8 {
        self.b[off - 0x1000]
    }
}

#[inline(always)]
fn cdiv(a: i32, b: i32) -> i32 {
    let q = a.abs() / b.abs();
    if (a >= 0) == (b >= 0) {
        q
    } else {
        -q
    }
}

#[inline(always)]
fn i32w(v: i64) -> i32 {
    v as i32
}

#[inline(always)]
fn sat15(v: i64) -> i64 {
    let u = (v as u64) & 0xffffffff;
    if u >= 0x8000_0000 {
        0xffff
    } else {
        ((u >> 15) & 0xffff) as i64
    }
}

#[inline(always)]
fn strength(dev224: i32, dec: &MarbleParams) -> f32 {
    if dec.d14c == -999 {
        return 0.5; // dec is None in the Python reference
    }
    if dec.d14c == 3 {
        if dev224 > -51 {
            return 1.0;
        }
        return ((2 * dev224 + 200) as u32 as f32) * 0.01;
    }
    if dec.f0 == 0 {
        return 1.0;
    }
    let iso = dec.iso as f32;
    let mut f = if iso >= 1600.0 {
        1.0f32
    } else {
        (((1.0f32 - 0.5f32) * (iso - 100.0f32)) / (1600.0f32 - 100.0f32)) + 0.5f32
    };
    if dev224 >= 20 {
        let s0 = (cdiv(dev224, 20) + 5) as f32;
        let s2 = -s0.mul_add(0.2, -1.0);
        let s0b = s0.mul_add(0.2, -2.0);
        f = -f.mul_add(s0b, s2);
    }
    f
}

fn convert(
    planes: &mut [Vec<i64>; 3],
    w: usize,
    _h: usize,
    dec: &[i16],
    m: &[i32; 9],
    enc: &[i16],
    x0: usize,
    y0: usize,
    x1: usize,
    y1: usize,
) {
    for y in y0..y1 {
        for x in x0..x1 {
            let i = y * w + x;
            let s0 = dec[planes[0][i] as usize] as i64;
            let s1 = dec[planes[1][i] as usize] as i64;
            let s2 = dec[planes[2][i] as usize] as i64;
            for r in 0..3 {
                let o =
                    (m[3 * r] as i64 * s0 + m[3 * r + 1] as i64 * s1 + m[3 * r + 2] as i64 * s2)
                        >> 13;
                let o = o.clamp(0, 0x3fff) as usize;
                planes[r][i] = enc[o] as u16 as i64;
            }
        }
    }
}

fn to_ycc(r: &[i64], g: &[i64], b: &[i64], n: usize) -> (Vec<i64>, Vec<i64>, Vec<i64>) {
    let mut y = vec![0i64; n];
    let mut cb = vec![0i64; n];
    let mut cr = vec![0i64; n];
    for i in 0..n {
        let rr = r[i];
        let gg = g[i];
        let bb = b[i];
        y[i] = ((rr * 0xb44 + gg * 0xdc3 + bb * 0x271) >> 11).min(0xefff) + 0x1000;
        let bv = i32w(rr * -0x6ab + gg * -0x964 + bb * 0x1011) >> 11;
        let cv = i32w(rr * 0x1acc + gg * -0x1ac0 + bb * -9) >> 11;
        cb[i] = ((bv.clamp(-0x8000, 0x7fff) + 0x8000) as u16) as i64;
        cr[i] = ((cv.clamp(-0x8000, 0x7fff) + 0x8000) as u16) as i64;
    }
    (y, cb, cr)
}

fn from_ycc(y: &[i64], cb: &[i64], cr: &[i64], n: usize) -> (Vec<i64>, Vec<i64>, Vec<i64>) {
    let mut r = vec![0i64; n];
    let mut g = vec![0i64; n];
    let mut b = vec![0i64; n];
    for i in 0..n {
        let yy = y[i] - 0x1000;
        let u = cb[i] - 0x8000;
        let v = cr[i] - 0x8000;
        let rv = i32w(yy * 0x2546 + v * 0x1526 + u * -0x59e) >> 15;
        let gv = i32w(yy * 0x254b + v * -0x1118 + u * -0x5b3) >> 15;
        let bv = i32w(yy * 0x2542 + v * -0x136 + u * 0x3a10) >> 15;
        r[i] = rv.clamp(0, 0x3fff) as i64;
        g[i] = gv.clamp(0, 0x3fff) as i64;
        b[i] = bv.clamp(0, 0x3fff) as i64;
    }
    (r, g, b)
}

fn rect_sum(a: &[i64], w: usize, h: usize, bh: usize, bw: usize, nh: usize, nw: usize) -> Vec<i64> {
    let mut out = vec![0i64; nh * nw];
    for by in 0..nh {
        for bx in 0..nw {
            let y0 = by * bh;
            let y1 = (y0 + bh).min(h);
            let x0 = bx * bw;
            let x1 = (x0 + bw).min(w);
            let mut s = 0i64;
            for y in y0..y1 {
                for x in x0..x1 {
                    s += a[y * w + x];
                }
            }
            out[by * nw + bx] = s;
        }
    }
    out
}

fn pad6(a: &[i64], nw: usize, nh: usize) -> Vec<i64> {
    let pw = nw + 12;
    let mut out = vec![0i64; (nh + 12) * pw];
    for y in 0..nh + 12 {
        let sy = (y as i32 - 6).clamp(0, nh as i32 - 1) as usize;
        for x in 0..nw + 12 {
            let sx = (x as i32 - 6).clamp(0, nw as i32 - 1) as usize;
            out[y * pw + x] = a[sy * nw + sx];
        }
    }
    out
}

fn binom3(q: &mut [i64], nw: usize, nh: usize, a: i64) -> Vec<i64> {
    let pw = nw + 12;
    let mut out = vec![0i64; (nh + 12) * pw];
    for y in 0..nh {
        for x in 0..nw {
            let i = (6 + y) * pw + 6 + x;
            let s = |dy: i32, dx: i32| -> i64 { q[(i as i32 + dy * pw as i32 + dx) as usize] };
            let sum = s(-1, -1)
                + 2 * s(-1, 0)
                + s(-1, 1)
                + 2 * s(0, -1)
                + 4 * s(0, 0)
                + 2 * s(0, 1)
                + s(1, -1)
                + 2 * s(1, 0)
                + s(1, 1);
            let t = (sum * (0x100 - a) + a * 0x10 * s(0, 0) + 0x800) as u64 & 0xffffffff;
            out[i] = ((t >> 12) & 0xffff) as i64;
        }
    }
    out
}

unsafe fn luma_path(
    y35: &mut [i64],
    y39: &[i64],
    w4: usize,
    h4: usize,
    thr_y: i64,
    a_y: i64,
    g20: i64,
    g24: i64,
    lo_l: i32,
    hi_l: i32,
    gate: i32,
    g34: i64,
    g38: i64,
    g3c: i64,
    g40: i64,
    debug_stage: i32,
    debug_out: *mut f32,
) {
    let n8w = (w4 + 7) >> 3;
    let n8h = (h4 + 7) >> 3;
    let s = rect_sum(y35, w4, h4, 8, 8, n8h, n8w);
    let m: Vec<i64> = s.iter().map(|v| ((v + 0x20) >> 6).max(1)).collect();
    if debug_stage == 30 {
        debug_write(&m, debug_out);
        return;
    }
    let p = pad6(&m, n8w, n8h);
    let pw = n8w + 12;
    let mut q = vec![0i64; (n8h + 12) * pw];
    // The Python sigma region is q[4 : n8h+8, 4 : n8w+8]: four rows/columns
    // wider than the block grid, and the extra margin feeds binom3's borders.
    for by in 0..n8h + 4 {
        for bx in 0..n8w + 4 {
            let i = (4 + by) * pw + 4 + bx;
            let c = p[i];
            let mut acc = 0i64;
            let mut n = 0i64;
            for dy in [-4i32, -2, 0, 2, 4] {
                for dx in [-4i32, -2, 0, 2, 4] {
                    let v = p[(i as i32 + dy * pw as i32 + dx) as usize];
                    if (c - v).abs() <= thr_y && v != 0 {
                        acc += v;
                        n += 1;
                    }
                }
            }
            let idx = if ((n - 1) & 0xffffffff) > 24 {
                0usize
            } else {
                n as usize
            };
            q[i] = sat15(RECIP25[idx] * acc + 0x4000);
        }
    }
    if debug_stage == 31 {
        debug_write(&q, debug_out);
        return;
    }
    let q = binom3(&mut q, n8w, n8h, a_y);
    if debug_stage == 32 {
        debug_write(&q, debug_out);
        return;
    }

    // bilinear x8 upsample
    let upw = n8w * 8;
    let up =
        |by: usize, bx: usize, dy: usize, dx: usize| -> i64 { q[(6 + by + dy) * pw + 6 + bx + dx] };
    for by in 0..n8h {
        for bx in 0..n8w {
            let d29 = up(by, bx, 0, 0);
            let d30 = up(by, bx, 0, 1);
            let d27 = up(by, bx, 1, 0);
            let d25 = up(by, bx, 1, 1);
            for k in 0..8usize {
                let top = 15 - 2 * k as i64;
                let bot = 1 + 2 * k as i64;
                for j in 0..8usize {
                    let l = 15 - 2 * j as i64;
                    let rw = 1 + 2 * j as i64;
                    let xv = (d30 * rw + d29 * l) * top + bot * (d25 * rw + d27 * l);
                    let oy = by * 8 + k;
                    let ox = bx * 8 + j;
                    if oy < h4 && ox < w4 {
                        y35[oy * w4 + ox] = ((xv + 128) >> 8) & 0xffff;
                    }
                }
            }
        }
    }
    let _ = upw;
    if debug_stage == 33 {
        debug_write(y35, debug_out);
        return;
    }

    // detail gain
    for i in 0..w4 * h4 {
        let yo = y39[i] >> 6;
        let d = yo - (y35[i] >> 6);
        let gain = if d >= 0 { g20 } else { g24 };
        let a = d.abs();
        let a5 = a.min(g3c);
        let a4 = if a >= g38 { a5 - g38 } else { 0 };
        let g2 = (((g34 * 0x400 + a4 * g40) >> 9) + 1) >> 1;
        let g2 = g2.min(0x400);
        let g5 = if gate != 0 { g2 } else { 0x400 };
        let wlo = match hi_l {
            1 => yo * 6,
            2 => yo << 2,
            3 => yo * 3,
            _ => 0x200,
        };
        let whi = match lo_l {
            1 => (yo ^ 0x3ff) * 6,
            2 => (yo << 2) ^ 0xffc,
            3 => (yo ^ 0x3ff) * 3,
            _ => 0x200,
        };
        let w = ((wlo as u64) & 0xffffffff).min((whi as u64) & 0xffffffff) as i64;
        let w = if w > 0x1ff { 0x200 } else { w };
        let v = i32w((((g5 * gain) >> 9) + 1 >> 1) * d * w + yo * 0x80000);
        let r = if v > 0 {
            ((v >> 18) + 1) >> 1
        } else {
            -((((-v) >> 18) + 1) >> 1)
        };
        let r = r.clamp(0, 0x3ff);
        y35[i] = (r as i64) << 6;
    }
}

unsafe fn debug_write(v: &[i64], out: *mut f32) {
    if out.is_null() {
        return;
    }
    let o = slice::from_raw_parts_mut(out, v.len());
    for i in 0..v.len() {
        o[i] = v[i] as f32;
    }
}

#[no_mangle]
pub unsafe extern "C" fn marble_slab_u16(
    r_in: *const u16,
    g_in: *const u16,
    b_in: *const u16,
    w: usize,
    h: usize,
    params: *const MarbleParams,
    r_out: *mut u16,
    g_out: *mut u16,
    b_out: *mut u16,
    debug_stage: i32,
    debug_plane: i32,
    debug_out: *mut f32,
) -> i32 {
    if params.is_null() || w < 10 || h < 10 {
        return -1;
    }
    let p = &*params;
    let look = Look {
        b: slice::from_raw_parts(p.look, 0x200),
    };
    let bnr = p.d218 == 1 && look.s16(0x109c) != 0;
    let lum = p.d2c0;
    // Clarity 0..=109 is the addressable range of the 11-level strength tables
    // (FUN_100126f0c uses q = lum / 10 as an index).  Outside it Edit itself reads
    // past the table, so there is nothing to reproduce: fall back to Python rather
    // than panic on `q as usize` (negative) or overrun the arrays (>= 110).
    if bnr || !(p.f22780 == 1.0 && p.f22784 == 1.0) || lum == 0 || !(0..=109).contains(&lum) {
        return 0; // unsupported / nothing to do: caller falls back
    }
    let dec = slice::from_raw_parts(p.gamma_dec, 0x4000);
    // The initial conversion always encodes through gamma_enc; the final one
    // uses srgb_enc in mode 0 and gamma_enc in mode 1.
    let enc_init = slice::from_raw_parts(p.gamma_enc, 0x4000);
    let m_fwd = &p.m_fwd;
    let m_inv = &p.m_inv;
    let n = w * h;
    let rin = slice::from_raw_parts(r_in, n);
    let gin = slice::from_raw_parts(g_in, n);
    let bin = slice::from_raw_parts(b_in, n);
    let mut planes: [Vec<i64>; 3] = [
        rin.iter().map(|&v| v as i64).collect(),
        gin.iter().map(|&v| v as i64).collect(),
        bin.iter().map(|&v| v as i64).collect(),
    ];

    // initial colour conversion
    {
        let mt = if p.mode == 0 {
            slice::from_raw_parts(p.srgb_dec, 0x4000)
        } else {
            dec
        };
        convert(&mut planes, w, h, mt, m_fwd, enc_init, 0, 0, w, h);
    }
    if debug_stage == 0 {
        debug_write(&planes[debug_plane as usize], debug_out);
        return 1;
    }

    let (y, cb, cr) = to_ycc(&planes[0], &planes[1], &planes[2], n);
    if debug_stage == 1 {
        let v = match debug_plane {
            0 => &y,
            1 => &cb,
            _ => &cr,
        };
        debug_write(v, debug_out);
        return 1;
    }
    drop(planes);
    let f = strength(p.d224, p);

    let rw = w;
    let rh = h;
    let w4 = if rw % 4 == 0 { rw } else { rw - rw % 4 + 4 };
    let h4 = if rh % 4 == 0 { rh } else { rh - rh % 4 + 4 };
    let half4 = w4 / 2;
    let n2 = rw / 2;
    let mut yc = vec![0i64; h4 * w4];
    let mut cbh = vec![0i64; h4 * half4];
    let mut crh = vec![0i64; h4 * half4];
    if rw > 1 {
        for yy in 0..rh {
            for x in 0..2 * n2 {
                yc[yy * w4 + x] = y[yy * rw + x];
            }
            for x in 0..n2 {
                cbh[yy * half4 + x] = (cb[yy * rw + 2 * x] + cb[yy * rw + 2 * x + 1]) >> 1;
                crh[yy * half4 + x] = (cr[yy * rw + 2 * x] + cr[yy * rw + 2 * x + 1]) >> 1;
            }
        }
    }
    if n2 < half4 {
        for yy in 0..rh {
            for x in 2 * n2..w4 {
                yc[yy * w4 + x] = y[yy * rw + (rw - 1)];
            }
            for x in n2..half4 {
                cbh[yy * half4 + x] = cb[yy * rw + (rw - 1)];
                crh[yy * half4 + x] = cr[yy * rw + (rw - 1)];
            }
        }
    }
    if w4 > 1 && rh < h4 {
        // every padded row rh..h4 repeats the last real row (Python: Yc[rh:] = ...)
        for yy in rh..h4 {
            for x in 0..w4 / 2 {
                let v = yc[(rh - 1) * w4 + 2 * x];
                yc[yy * w4 + 2 * x] = v;
                yc[yy * w4 + 2 * x + 1] = v;
            }
            for x in 0..half4 {
                cbh[yy * half4 + x] = cbh[(rh - 1) * half4 + x];
                crh[yy * half4 + x] = crh[(rh - 1) * half4 + x];
            }
        }
    }

    drop(y);
    if debug_stage == 2 {
        match debug_plane {
            0 => debug_write(&yc, debug_out),
            1 => debug_write(&cbh, debug_out),
            _ => debug_write(&crh, debug_out),
        }
        return 1;
    }
    let mut y35 = yc.clone();
    let y39 = yc;
    let cr36 = crh;
    let cb37 = cbh;

    // parameters
    let k67 = if p.d218 != 0 { cdiv(p.d224, 20) + 5 } else { 0 };
    let t = ((p.d224 as i64 * -0x66666667) >> 32) as i32;
    let k24 = if p.d218 != 0 { (t >> 3) - (t >> 31) } else { 5 };
    let flag = if k67 > 5 { p.f0 } else { 0 };
    let (b4, b0) = if flag == 1 {
        let k = k67 * 8 - 40;
        (
            cdiv(k * look.s16(0x10ae), 5) + look.s16(0x10ae),
            cdiv(k * look.s16(0x10b0), 5) + look.s16(0x10b0),
        )
    } else {
        (
            k67 * cdiv(look.s16(0x10ae), 5),
            k67 * cdiv(look.s16(0x10b0), 5),
        )
    };
    let _ = (b4, b0, k24);

    // Same rounding as Sony's C code (FUN_100126f0c): both truncate toward zero and
    // `%` takes the dividend's sign.  The entry guard above keeps 0 <= q <= 10, so the
    // casts into the tables below cannot panic.
    let q = lum / 10;
    let r10 = lum % 10;
    let (gate, lo_l, hi_l, g34, g38, g3c, g40, thr_y, a_y, g20, g24) = if look.u8(0x113e) == 0 {
        // No per-look strength table: LUMA_STRENGTH interpolated at `lum`, scaled by an
        // ISO factor (sony_marble.py, the look+0x113e == 0 branch).
        let i24 = if lum < 100 { q + 1 } else { 10 };
        let base = LUMA_STRENGTH[q as usize];
        let v67 = f64::mul_add(
            (LUMA_STRENGTH[i24 as usize] - base) as f64 * 0.1,
            r10 as f64,
            base as f64,
        ) as i64;
        let iso = p.iso as i64;
        let isof: i64 = if iso < 0xc81 {
            100
        } else if iso < 0x64000 {
            let w10 = f64::mul_add((iso - 0xc800) as f64, -4.7433035714285714e-05, 67.0) as i64;
            let w11 = f64::mul_add((iso - 0x3200) as f64, -0.00044270833333333337, 84.0) as i64;
            let w9 = if iso > 0xc800 { w10 } else { w11 };
            let w10b = f64::mul_add((iso - 0xc80) as f64, -0.0016666666666666668, 100.0) as i64;
            if iso > 0x3200 { w9 } else { w10b }
        } else {
            0x32
        };
        let g = (v67 as f64 * (0.01 * isof as f64)) as i64;
        (0, 0, 0, 0, 0, 0, 0, 0, 0, g, g)
    } else {
        let gate = look.u8(0x116a) as i32;
        let lo_l = look.u8(0x1168) as i32;
        let hi_l = look.u8(0x1169) as i32;
        let g34 = look.u16(0x1172) as i64;
        let g38 = look.u16(0x116e) as i64;
        let g3c = look.u16(0x1170) as i64;
        let g40 = look.u16(0x116c) as i64;
        let thr_y = look.u16(0x1174) as i64;
        let a_y = look.u16(0x1176) as i64;
        let i56 = if lum < 100 { q + 1 } else { 10 };
        let tab = |base: usize, i: i32| -> i64 {
            if i < 10 {
                look.u16(base + 2 * i as usize) as i64
            } else {
                ((look.u16(base + 0x12) * 2 - look.u16(base + 0x10)) & 0xffffffff) as i64
            }
        };
        let lo = tab(0x1140, q);
        let hi = tab(0x1140, i56);
        let g20 = f64::mul_add(r10 as f64 * 0.1, i32w(hi - lo) as f64, lo as f64) as i64;
        let lo = tab(0x1154, q);
        let hi = tab(0x1154, i56);
        let g24 = f64::mul_add(i32w(hi - lo) as f64 * 0.1, r10 as f64, lo as f64) as i64;
        (gate, lo_l, hi_l, g34, g38, g3c, g40, thr_y, a_y, g20, g24)
    };

    luma_path(
        &mut y35,
        &y39,
        w4,
        h4,
        thr_y,
        a_y,
        g20,
        g24,
        lo_l,
        hi_l,
        gate,
        g34,
        g38,
        g3c,
        g40,
        debug_stage,
        debug_out,
    );
    if debug_stage == 3 {
        debug_write(&y35, debug_out);
        return 1;
    }

    drop(y39);
    // back to full width
    let m = (rw + 1) / 2;
    let mut yo = vec![0i64; n];
    let mut cbo = vec![0i64; n];
    let mut cro = vec![0i64; n];
    if rw + 1 > 1 {
        let xend = (2 * m).min(w);
        for yy in 0..rh {
            for x in 0..xend {
                yo[yy * w + x] = y35[yy * w4 + x];
            }
            for dx in 0..2 {
                for x in 0..m {
                    let ox = 2 * x + dx;
                    if ox < w {
                        cbo[yy * w + ox] = cb37[yy * half4 + x];
                        cro[yy * w + ox] = cr36[yy * half4 + x];
                    }
                }
            }
        }
    }

    drop(y35);
    drop(cb37);
    drop(cr36);
    // blend chroma (Cro/Cbo already hold the full-width upsampled values)
    for i in 0..n {
        let o = cr[i] as f32;
        let v = (cro[i] as f32 - o).mul_add(f, o);
        let v = v.min(65535.0).max(0.0);
        cro[i] = (v.trunc() as i64) & 0xffff;
        let o = cb[i] as f32;
        let v = (cbo[i] as f32 - o).mul_add(f, o);
        let v = v.min(65535.0).max(0.0);
        cbo[i] = (v.trunc() as i64) & 0xffff;
    }
    drop(cb);
    drop(cr);
    if debug_stage == 4 {
        let v = match debug_plane {
            0 => &yo,
            1 => &cbo,
            _ => &cro,
        };
        debug_write(v, debug_out);
        return 1;
    }

    let (mut rr, mut gg, mut bb) = from_ycc(&yo, &cbo, &cro, n);
    drop(yo);
    drop(cbo);
    drop(cro);
    if debug_stage == 5 {
        let v = match debug_plane {
            0 => &rr,
            1 => &gg,
            _ => &bb,
        };
        debug_write(v, debug_out);
        return 1;
    }

    // final conversion in the valid region
    let vx0 = 16;
    let vy0 = 16;
    let vx1 = w.saturating_sub(16);
    let vy1 = h.saturating_sub(16);
    {
        let mt = if p.mode == 0 {
            slice::from_raw_parts(p.srgb_enc, 0x4000)
        } else {
            slice::from_raw_parts(p.gamma_enc, 0x4000)
        };
        // mode 0: dec = gamma_dec, enc = srgb_enc; mode 1: dec = gamma_dec, enc = gamma_enc
        let mut refs: [Vec<i64>; 3] = [rr, gg, bb];
        convert(&mut refs, w, h, dec, m_inv, mt, vx0, vy0, vx1, vy1);
        rr = refs[0].clone();
        gg = refs[1].clone();
        bb = refs[2].clone();
    }
    if debug_stage == 6 {
        let v = match debug_plane {
            0 => &rr,
            1 => &gg,
            _ => &bb,
        };
        debug_write(v, debug_out);
        return 1;
    }

    let ro = slice::from_raw_parts_mut(r_out, n);
    let go = slice::from_raw_parts_mut(g_out, n);
    let bo = slice::from_raw_parts_mut(b_out, n);
    for i in 0..n {
        ro[i] = rr[i] as u16;
        go[i] = gg[i] as u16;
        bo[i] = bb[i] as u16;
    }
    1
}
