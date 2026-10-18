//! ZcTaskITP (demosaic), one tile at a time.
//!
//! Direct port of sonylook/sony_itp.py for the whole-tile ROI that
//! itp_full_image always uses.  The tiling grid and the edge extension stay in
//! Python; this module only replaces itp_process().
//!
//! Every f32 operation keeps the Python evaluation order.  Fused multiply-adds
//! use f32::mul_add (hardware fmadd on aarch64).

use std::slice;
use std::sync::atomic::{AtomicUsize, Ordering};

#[derive(Clone)]
pub struct Plane {
    pub w: usize,
    pub h: usize,
    pub d: Vec<f32>,
}

impl Plane {
    fn new(w: usize, h: usize) -> Self {
        Plane {
            w,
            h,
            d: vec![0.0; w * h],
        }
    }
    #[inline(always)]
    fn at(&self, y: usize, x: usize) -> f32 {
        self.d[y * self.w + x]
    }
    #[inline(always)]
    fn set(&mut self, y: usize, x: usize, v: f32) {
        self.d[y * self.w + x] = v;
    }
    /// Sony's getter: zero outside the plane (negative coordinates included).
    #[inline(always)]
    fn get(&self, y: i32, x: i32) -> f32 {
        if y >= 0 && x >= 0 && (y as usize) < self.h && (x as usize) < self.w {
            self.d[y as usize * self.w + x as usize]
        } else {
            0.0
        }
    }
}

const K1_32: f32 = f32::from_bits(0x3d00_0000); // 1/32
const K1_88: f32 = f32::from_bits(0x3c3a_2e8c); // 1/88

#[inline(always)]
fn fma(a: f32, b: f32, c: f32) -> f32 {
    a.mul_add(b, c)
}

#[inline(always)]
fn pair4(a: f32, b: f32, c: f32, d: f32) -> f32 {
    (a + b) + (c + d)
}

#[inline(always)]
fn avg(a: f32, b: f32) -> f32 {
    (a + b) * 0.5
}

#[inline(always)]
fn mix121(a: f32, b: f32, c: f32) -> f32 {
    fma(c, 6.0, a + b) * 0.125
}

// ---------------------------------------------------------------------------
// ZcTaskITP scaling (FUN_1000f13bc)

fn itp_scale(
    raw: &[u16],
    w: usize,
    h: usize,
    black: &[i32; 4],
    gains: &[f32; 4],
    scale_7311: i32,
) -> Plane {
    let f17 = (scale_7311 as f32) * 3.0517578e-05f32;
    let k = [
        f17 * gains[0],
        f17 * gains[1],
        f17 * gains[2],
        f17 * gains[3],
    ];
    let mut p = Plane::new(w, h);
    for y in 0..h {
        for x in 0..w {
            let idx = (x & 1) | ((y & 1) << 1);
            let d = (raw[y * w + x] as i64 - black[idx] as i64) as f32;
            p.d[y * w + x] = (k[idx] * 0.0625f32) * d;
        }
    }
    p
}

// ---------------------------------------------------------------------------
// weighted_sum / fill_candidates

#[inline(always)]
fn weighted_sum(src: &Plane, x: i32, y: i32, center_dx: i32) -> f32 {
    let g = |yy: i32, xx: i32| src.get(yy, xx);
    let p11 = g(y - 2, x);
    let p12 = g(y - 2, x + 1);
    let p14 = g(y - 2, x + 3);
    let p13 = g(y - 2, x + 4);
    let p16 = g(y - 1, x - 2);
    let p15 = g(y - 1, x + 2);
    let p18 = g(y + 1, x - 2);
    let p17 = g(y + 1, x + 2);
    let p21 = g(y + 2, x - 2);
    let p20 = g(y + 2, x - 1);
    let p23 = g(y + 2, x + 1);
    let p22 = g(y + 2, x + 2);
    let p25 = g(y - 2, x + 2);
    let p24 = g(y - 1, x - 1);
    let p27 = g(y - 1, x);
    let p26 = g(y - 1, x + 1);
    let p29 = g(y, x);
    let p28 = g(y, x + 1);
    let p31 = g(y, x + 3);
    let p30 = g(y, x + 4);
    let p33 = g(y + 1, x - 1);
    let p32 = g(y + 1, x);
    let p35 = g(y + 1, x + 1);
    let p34 = g(y + 2, x);

    let mut s = (p11 + p12) + (p14 + p13);
    let mut t = (p16 + p15) + p18;
    s += t;
    t = ((p17 + p21) + p20) + p23;
    s += t;
    s += p22;

    let mut u = (p24 + p25) + (p27 + p26);
    let mut v = (p29 + p28) + p31;
    u += v;
    v = ((p30 + p33) + p32) + p35;
    u += v;
    u += p34;

    s = fma(u, 6.0, s);
    s + g(y, x + center_dx) * 36.0
}

fn fill_candidates(dst: &mut Plane, src: &Plane) {
    let w = dst.w;
    let h = dst.h;
    if w > 4 && h > 4 {
        for y in (2..h - 2).step_by(2) {
            for x in (2..w - 2).step_by(2) {
                let v = weighted_sum(src, x as i32, y as i32, 0) * K1_32;
                dst.set(y, x, v);
            }
            for x in (3..w - 1).step_by(2) {
                let v = weighted_sum(src, x as i32, y as i32, 2) * K1_32;
                dst.set(y, x, v);
            }
        }
        for y in (3..h - 1).step_by(2) {
            for x in (2..w - 2).step_by(2) {
                let v = weighted_sum(src, x as i32, y as i32, 2) * K1_88;
                dst.set(y, x, v);
            }
            for x in (3..w - 1).step_by(2) {
                let v = weighted_sum(src, x as i32, y as i32, 0) * K1_88;
                dst.set(y, x, v);
            }
        }
    }
    let mut xs = (2..w.saturating_sub(2)).step_by(2);
    let xend = match xs.next() {
        Some(_) => {
            let mut last = 2;
            for x in xs {
                last = x;
            }
            last + 2
        }
        None => 2,
    };
    for y in (2..h.saturating_sub(2)).step_by(2) {
        dst.set(y, 0, src.at(y, 0));
        dst.set(y, 1, src.at(y, 1));
        if xend + 1 < w {
            dst.set(y, xend, src.at(y, 2));
            dst.set(y, xend + 1, src.at(y, 3));
        }
    }
    for y in (3..h.saturating_sub(1)).step_by(2) {
        dst.set(y, 0, src.at(y, 0));
        dst.set(y, 1, src.at(y, 1));
        if xend + 1 < w {
            dst.set(y, xend, src.at(y, 2));
            dst.set(y, xend + 1, src.at(y, 3));
        }
    }
}

// ---------------------------------------------------------------------------
// second differences

fn h_second_diff(out_abs: &mut Plane, src: &Plane) {
    let (w, h) = (src.w, src.h);
    if w <= 4 {
        return;
    }
    for y in 0..h {
        for x in 2..w - 2 {
            let c = src.at(y, x);
            let v = fma(c, -2.0, src.at(y, x - 2) + src.at(y, x + 2)) * -0.25;
            let par = ((x ^ y) & 1) as f32;
            out_abs.set(y, x, v.abs() * par);
        }
    }
}

fn v_second_diff(out_abs: &mut Plane, src: &Plane) {
    let (w, h) = (src.w, src.h);
    if h <= 4 {
        return;
    }
    for y in 2..h - 2 {
        for x in 0..w {
            let c = src.at(y, x);
            let v = fma(c, -2.0, src.at(y - 2, x) + src.at(y + 2, x)) * -0.25;
            let par = ((x ^ y) & 1) as f32;
            out_abs.set(y, x, v.abs() * par);
        }
    }
}

// ---------------------------------------------------------------------------
// nine_tap

fn dot9(k: &[f32; 9], vals: &[f32; 9]) -> f32 {
    let mut s = k[0] * vals[0];
    for i in 1..9 {
        s = fma(k[i], vals[i], s);
    }
    s
}

fn nine_tap(out_a: &mut Plane, out_b: &mut Plane, src: &Plane, k1: &[f32; 9], k2: &[f32; 9]) {
    let (w, h) = (src.w, src.h);
    if w <= 8 || h <= 8 {
        return;
    }
    let mut vv = [0.0f32; 9];
    let mut hh = [0.0f32; 9];
    // even rows
    for y in (4..h - 4).step_by(2) {
        for x in (4..w - 4).step_by(2) {
            for i in 0..9 {
                vv[i] = src.get(y as i32 - 4 + i as i32, x as i32);
                hh[i] = src.get(y as i32, x as i32 - 4 + i as i32);
            }
            out_b.set(y, x, dot9(k2, &vv));
            out_a.set(y, x, dot9(k2, &hh));
        }
        for x in (5..w - 3).step_by(2) {
            for i in 0..9 {
                vv[i] = src.get(y as i32 - 4 + i as i32, x as i32);
                hh[i] = src.get(y as i32, x as i32 - 4 + i as i32);
            }
            out_b.set(y, x, dot9(k1, &vv));
            out_a.set(y, x, dot9(k1, &hh));
        }
    }
    // odd rows
    for y in (5..h - 3).step_by(2) {
        for x in (4..w - 4).step_by(2) {
            for i in 0..9 {
                vv[i] = src.get(y as i32 - 4 + i as i32, x as i32);
                hh[i] = src.get(y as i32, x as i32 - 4 + i as i32);
            }
            out_b.set(y, x, dot9(k1, &vv));
            out_a.set(y, x, dot9(k1, &hh));
        }
        for x in (5..w - 3).step_by(2) {
            for i in 0..9 {
                vv[i] = src.get(y as i32 - 4 + i as i32, x as i32);
                hh[i] = src.get(y as i32, x as i32 - 4 + i as i32);
            }
            out_b.set(y, x, dot9(k2, &vv));
            out_a.set(y, x, dot9(k2, &hh));
        }
    }
}

// ---------------------------------------------------------------------------
// five_by_five

fn five_by_five(out: &mut Plane, src: &Plane) {
    let (w, h) = (src.w, src.h);
    if w < 6 || h < 6 {
        return;
    }
    let even_at = |y: usize, x: usize| -> f32 {
        let e = [
            src.get(y as i32 - 2, x as i32 + 1),
            src.get(y as i32 - 2, x as i32 - 1),
            src.get(y as i32 - 1, x as i32 - 2),
            src.get(y as i32 - 1, x as i32 + 2),
            src.get(y as i32 + 1, x as i32 - 2),
            src.get(y as i32 + 1, x as i32 + 2),
            src.get(y as i32 + 2, x as i32 - 1),
            src.get(y as i32 + 2, x as i32 + 1),
        ];
        let sum8 = (((e[0] + e[1]) + (e[2] + e[3])) + ((e[4] + e[5]) + e[6])) + e[7];
        let c = [
            src.get(y as i32, x as i32 - 1),
            src.get(y as i32 - 1, x as i32),
            src.get(y as i32, x as i32 + 1),
            src.get(y as i32 + 1, x as i32),
        ];
        fma(pair4(c[0], c[1], c[2], c[3]), 0.3125, sum8 * -0.03125)
    };
    let odd_at = |y: usize, x: usize| -> f32 {
        let corners = [
            src.get(y as i32 - 2, x as i32 + 2),
            src.get(y as i32 - 2, x as i32 - 2),
            src.get(y as i32 + 2, x as i32 - 2),
            src.get(y as i32 + 2, x as i32 + 2),
        ];
        let axial = [
            src.get(y as i32, x as i32 - 2),
            src.get(y as i32 - 2, x as i32),
            src.get(y as i32, x as i32 + 2),
            src.get(y as i32 + 2, x as i32),
        ];
        let diag = [
            src.get(y as i32 - 1, x as i32 + 1),
            src.get(y as i32 - 1, x as i32 - 1),
            src.get(y as i32 + 1, x as i32 - 1),
            src.get(y as i32 + 1, x as i32 + 1),
        ];
        let mut v = pair4(corners[0], corners[1], corners[2], corners[3]) * -0.015625;
        v = fma(pair4(axial[0], axial[1], axial[2], axial[3]), -0.03125, v);
        v = fma(pair4(diag[0], diag[1], diag[2], diag[3]), 0.125, v);
        fma(src.at(y, x), 0.6875, v)
    };
    for y in (2..h - 2).step_by(2) {
        for x in (2..w - 2).step_by(2) {
            out.set(y, x, even_at(y, x));
        }
        for x in (3..w - 1).step_by(2) {
            out.set(y, x, odd_at(y, x));
        }
    }
    for y in (3..h - 1).step_by(2) {
        for x in (2..w - 2).step_by(2) {
            out.set(y, x, odd_at(y, x));
        }
        for x in (3..w - 1).step_by(2) {
            out.set(y, x, even_at(y, x));
        }
    }
}

// ---------------------------------------------------------------------------
// direction_map

#[inline(always)]
fn err3(c: f32, a: f32, b: f32) -> f32 {
    let t = (a - b).abs() + (c - a).abs();
    t + ((c - (a + a)) + b).abs()
}

fn direction_map(out: &mut Plane, src: &Plane, p: &ItpParams) {
    let (w, h) = (src.w, src.h);
    if w <= 8 || h <= 8 {
        return;
    }
    let mut eh = Plane::new(w, h);
    let mut ev = Plane::new(w, h);
    let mut e1 = Plane::new(w, h);
    let mut e2 = Plane::new(w, h);
    // pass 1
    for y in 2..h - 4 {
        for x in 2..w - 4 {
            eh.set(y, x, err3(src.at(y, x), src.at(y, x + 2), src.at(y, x + 4)));
            ev.set(y, x, err3(src.at(y, x), src.at(y + 2, x), src.at(y + 4, x)));
            e2.set(
                y,
                x,
                err3(src.at(y, x), src.at(y + 1, x + 1), src.at(y + 2, x + 2)),
            );
        }
        for x in 4..w - 2 {
            e1.set(
                y,
                x,
                err3(src.at(y, x), src.at(y + 1, x - 1), src.at(y + 2, x - 2)),
            );
        }
    }
    let t_even = [
        0.03125f32, 0.0625, 0.03125, 0.0625, 0.03125, 0.0625, 0.03125, 0.0625,
    ];
    let t_odd = [
        0.0625f32, 0.03125, 0.0625, 0.03125, 0.0625, 0.03125, 0.0625, 0.03125,
    ];
    let k1 = p.k1;
    let k2 = p.k2;
    let k3 = p.k3;
    let k4 = p.k4;
    let k5 = p.k5;
    let k6 = p.k6;
    for y in 4..h - 4 {
        let row_par = y & 1;
        for x in 4..w - 4 {
            let xb = (x - 4) & 7;
            let sc = if row_par == 0 { t_even[xb] } else { t_odd[xb] };
            // f42 from D1
            let t1 = e1.at(y - 2, x - 1);
            let t2 = e1.at(y - 2, x - 2);
            let t3 = e1.at(y - 2, x);
            let t4 = e1.at(y - 1, x - 2);
            let t5 = e1.at(y - 1, x - 1);
            let t6 = e1.at(y - 1, x);
            let t7 = e1.at(y, x - 2);
            let t8 = e1.at(y, x - 1);
            let t9 = e1.at(y, x);
            let tf = (t1 + t2) + (t3 + t4);
            let d17 = tf as f64 + 4.0 * t5 as f64;
            let d18 = ((t6 as f64 + t7 as f64) + t8 as f64) + t9 as f64;
            let tt = (d17 + d18) as f32;
            let mut f42 = fma(0.25, tt, -k1);
            f42 = f42.max(k2);
            // f45 from D2 (t8 is D1)
            let t1 = e2.at(y - 2, x + 1);
            let t2 = e2.at(y - 2, x);
            let t3 = e2.at(y - 2, x + 2);
            let t4 = e2.at(y - 1, x);
            let t5 = e2.at(y - 1, x + 1);
            let t6 = e2.at(y - 1, x + 2);
            let t7 = e2.at(y, x);
            let t8 = e1.at(y, x + 1);
            let t9 = e2.at(y, x + 2);
            let tf = (t1 + t2) + (t3 + t4);
            let d17 = tf as f64 + 4.0 * t5 as f64;
            let d18 = ((t6 as f64 + t7 as f64) + t8 as f64) + t9 as f64;
            let tt = (d17 + d18) as f32;
            let mut f45 = fma(0.25, tt, -k1);
            f45 = f45.max(k2);
            let denom = f45 + f42 + 0.01f32;
            let zero = denom == 0.0;

            // vertical sum
            let a_m1 = ev.at(y - 3, x - 2);
            let a_0 = ev.at(y - 3, x - 1);
            let a_p1 = ev.at(y - 3, x);
            let a_p2 = ev.at(y - 3, x + 1);
            let a_p3 = ev.at(y - 3, x + 2);
            let b_m2 = ev.at(y - 2, x - 2);
            let b_0 = ev.at(y - 2, x);
            let b_p1 = ev.at(y - 2, x + 1);
            let b_p2 = ev.at(y - 2, x + 2);
            let c_m2 = ev.at(y - 1, x - 2);
            let c_m1 = ev.at(y - 1, x - 1);
            let c_0 = ev.at(y - 1, x);
            let c_p1 = ev.at(y - 1, x + 1);
            let c_p2 = ev.at(y - 1, x + 2);
            let t32 = fma(a_0, 2.0, a_m1);
            let mut d20 = b_0 as f64 + a_p1 as f64;
            d20 += c_0 as f64;
            let mut d22 = c_m1 as f64 + a_p2 as f64;
            d22 += c_p1 as f64;
            let mut d21 = a_p3 as f64 + t32 as f64;
            d21 = b_m2 as f64 * 9.0 + d21;
            d21 = b_p1 as f64 * 8.0 + d21;
            let mut d23 = b_p2 as f64 + c_m2 as f64;
            d23 += c_p2 as f64;
            d21 += d23;
            d20 = d20 * 6.0 + d21;
            d20 = d22 * 2.0 + d20;
            let mut f45b = fma(sc, d20 as f32, -k1);
            f45b = f45b.max(k2);

            // horizontal sum
            let p_m1 = eh.at(y - 1, x - 3);
            let p_0 = eh.at(y - 1, x - 2);
            let p_p1 = eh.at(y - 1, x - 1);
            let q_m1 = eh.at(y - 2, x - 3);
            let q_0 = eh.at(y - 2, x - 2);
            let q_p1 = eh.at(y - 2, x - 1);
            let s_m1 = eh.at(y, x - 3);
            let s_0 = eh.at(y, x - 2);
            let s_p1 = eh.at(y, x - 1);
            let t_m1 = eh.at(y + 1, x - 3);
            let t_0 = eh.at(y + 1, x - 2);
            let t_p1 = eh.at(y + 1, x - 1);
            let u_m1 = eh.at(y + 2, x - 3);
            let u_0 = eh.at(y + 2, x - 2);
            let u_p1 = eh.at(y + 2, x - 1);
            let fq = fma(p_m1, 2.0, (q_0 + q_m1) + q_p1);
            let mut d21 = p_0 as f64 * 8.0 + fq as f64;
            d21 += (t_0 * 8.0) as f64;
            let mut d24 = s_0 as f64 + s_m1 as f64;
            d24 += s_p1 as f64;
            d21 = d24 * 6.0 + d21;
            let mut d22 = t_m1 as f64 + p_p1 as f64;
            d22 += t_p1 as f64;
            let mut d23 = u_m1 as f64 + u_0 as f64;
            d23 += u_p1 as f64;
            d21 += d23;
            d21 = d22 * 2.0 + d21;
            let mut f43b = fma(sc, d21 as f32, -k1);
            f43b = f43b.max(k2);

            let num = (f43b + f45b) as f64 + 0.01;
            let num = num as f32;
            let quot = if zero { 1.0 } else { num / denom };
            let v = ((k3 - quot) + k4) * k5;
            let res = if v > k3 { k3 } else { v.max(k6) };
            out.set(y, x, res);
        }
    }
}

// ---------------------------------------------------------------------------
// candidates / weights

fn directional_weight(dst: &mut Plane, a: &Plane, b: &Plane, coef: &[f32; 5]) {
    let (k0, k1, eps) = (coef[0], coef[1], coef[2]);
    for y in 0..dst.h {
        for x in 0..dst.w {
            let av = a.at(y, x);
            let bv = b.at(y, x);
            let mut t1 = av - k0;
            if t1 <= 0.0 {
                t1 = 0.0;
            }
            t1 += eps;
            let mut t2 = bv - k1;
            if t2 <= 0.0 {
                t2 = 0.0;
            }
            let den = (t1 + eps) + t2;
            dst.set(y, x, if den > 0.0 { t1 / den } else { 0.5 });
        }
    }
}

fn blend_factor(dst: &mut Plane, a: &Plane, b: &Plane, p: &ItpParams) {
    let k = p.blend_k;
    let lo = p.blend_lo;
    let hi = p.blend_hi;
    let half = p.blend_half;
    for y in 0..dst.h {
        for x in 0..dst.w {
            let av = a.at(y, x);
            let bv = b.at(y, x);
            let num = k + av * 8.0;
            let den = k + bv * 8.0;
            let r = num / den;
            let inv_hi = 1.0f32 / r;
            let inv = if r > 1.0 { inv_hi } else { r };
            let v = lo + (inv - lo) * half;
            let res = if v < 1.0 { v } else { 1.0 };
            dst.set(y, x, if lo <= r { res } else { hi });
        }
    }
}

fn build_candidates(
    src: &Plane,
    table1: &[f32; 5],
    table2: &[f32; 5],
    p: &ItpParams,
) -> (Plane, Plane, Plane) {
    let (w, h) = (src.w, src.h);
    let mut abs_h = Plane::new(w, h);
    let mut abs_v = Plane::new(w, h);
    let mut p8 = Plane::new(w, h);
    let mut p9 = Plane::new(w, h);
    h_second_diff(&mut abs_h, src);
    fill_candidates(&mut p8, &abs_h);
    v_second_diff(&mut abs_v, src);
    fill_candidates(&mut p9, &abs_v);
    let mut out_a = Plane::new(w, h);
    let mut out_b = Plane::new(w, h);
    let mut out_c = Plane::new(w, h);
    directional_weight(&mut out_a, &p8, &p9, table1);
    directional_weight(&mut out_b, &p8, &p9, table2);
    blend_factor(&mut out_c, &p8, &p9, p);
    (out_a, out_b, out_c)
}

// ---------------------------------------------------------------------------
// green refinement

fn mix_121_average(out_a: &mut Plane, out_b: &mut Plane, src: &Plane) {
    let (w, h) = (src.w, src.h);
    if w <= 4 || h <= 4 {
        return;
    }
    for y in (2..h - 3).step_by(2) {
        for x in (2..w - 2).step_by(2) {
            out_b.set(y, x, avg(src.at(y - 1, x), src.at(y + 1, x)));
            out_a.set(y, x, avg(src.at(y, x - 1), src.at(y, x + 1)));
        }
        for x in (3..w - 1).step_by(2) {
            out_b.set(
                y,
                x,
                mix121(src.at(y + 2, x), src.at(y - 2, x), src.at(y, x)),
            );
            out_a.set(
                y,
                x,
                mix121(src.at(y, x + 2), src.at(y, x - 2), src.at(y, x)),
            );
        }
    }
    for y in (3..h - 2).step_by(2) {
        for x in (2..w - 2).step_by(2) {
            out_b.set(
                y,
                x,
                mix121(src.at(y + 2, x), src.at(y - 2, x), src.at(y, x)),
            );
            out_a.set(
                y,
                x,
                mix121(src.at(y, x + 2), src.at(y, x - 2), src.at(y, x)),
            );
        }
        for x in (3..w - 1).step_by(2) {
            out_b.set(y, x, avg(src.at(y - 1, x), src.at(y + 1, x)));
            out_a.set(y, x, avg(src.at(y, x - 1), src.at(y, x + 1)));
        }
    }
}

fn pair_average(oa: &mut Plane, ob: &mut Plane, a1: &Plane, b1: &Plane, a2: &Plane, b2: &Plane) {
    for y in 0..oa.h {
        for x in 0..oa.w {
            oa.set(y, x, (a2.at(y, x) + a1.at(y, x)) * 0.5);
            ob.set(y, x, (b2.at(y, x) + b1.at(y, x)) * 0.5);
        }
    }
}

fn lerp1(
    dst: &mut Plane,
    base: &Plane,
    other: &Plane,
    weight: &Plane,
    y0: usize,
    y1: usize,
    x0: usize,
    x1: usize,
) {
    for y in y0..y1 {
        for x in x0..x1 {
            let b = base.at(y, x);
            dst.set(y, x, fma(weight.at(y, x), other.at(y, x) - b, b));
        }
    }
}

fn lerp2(dst: &mut Plane, base: &Plane, other: &Plane, w1: &Plane, w2: &Plane) {
    for y in 0..dst.h {
        for x in 0..dst.w {
            let a = w1.at(y, x);
            let c = w2.at(y, x);
            let w = if a > c { a } else { c };
            let b = base.at(y, x);
            dst.set(y, x, fma(w, other.at(y, x) - b, b));
        }
    }
}

fn refine_green(
    src: &Plane,
    w6: &Plane,
    w7: &Plane,
    k1: &[f32; 9],
    k2: &[f32; 9],
    p: &ItpParams,
) -> Plane {
    let (w, h) = (src.w, src.h);
    let mut mix_a = Plane::new(w, h);
    let mut mix_b = Plane::new(w, h);
    mix_121_average(&mut mix_a, &mut mix_b, src);
    let mut tap_a = Plane::new(w, h);
    let mut tap_b = Plane::new(w, h);
    nine_tap(&mut tap_a, &mut tap_b, src, k1, k2);
    let mut avg_a = Plane::new(w, h);
    let mut avg_b = Plane::new(w, h);
    pair_average(&mut avg_a, &mut avg_b, &mix_a, &mix_b, &tap_a, &tap_b);
    let mut g0 = Plane::new(w, h);
    lerp1(
        &mut g0,
        &avg_a,
        &avg_b,
        w6,
        4,
        h.saturating_sub(4),
        0,
        w.saturating_sub(8),
    );

    let mut mosaic = Plane::new(w, h);
    for y in (0..h).step_by(2) {
        for x in (1..w).step_by(2) {
            mosaic.set(y, x, src.at(y, x));
        }
    }
    for y in (1..h).step_by(2) {
        for x in (0..w).step_by(2) {
            mosaic.set(y, x, src.at(y, x));
        }
    }
    let mut five = Plane::new(w, h);
    five_by_five(&mut five, &mosaic);
    let mut dm = Plane::new(w, h);
    direction_map(&mut dm, &mosaic, p);
    let mut out = Plane::new(w, h);
    lerp2(&mut out, &g0, &five, w7, &dm);
    out
}

// ---------------------------------------------------------------------------
// chroma

fn h_121(dst: &mut Plane, src: &Plane) {
    let (w, h) = (src.w, src.h);
    for y in 0..h {
        for x in 0..2.min(w) {
            dst.set(y, x, src.at(y, x));
        }
        if w > 4 {
            for x in 2..w - 2 {
                let v = (fma(src.at(y, x), 6.0, src.at(y, x - 2)) + src.at(y, x + 2)) * 0.125;
                dst.set(y, x, v);
            }
        }
        if w >= 2 {
            for x in w - 2..w {
                dst.set(y, x, src.at(y, x));
            }
        }
    }
}

fn v_121(dst: &mut Plane, src: &Plane) {
    let (w, h) = (src.w, src.h);
    if h <= 4 || w <= 4 {
        return;
    }
    for y in 2..h - 2 {
        for x in 2..w - 2 {
            let v = (fma(src.at(y, x), 6.0, src.at(y - 2, x)) + src.at(y + 2, x)) * 0.125;
            dst.set(y, x, v);
        }
    }
}

fn h_average(dst: &mut Plane, src: &Plane) {
    let (w, h) = (src.w, src.h);
    if w <= 4 {
        return;
    }
    for y in 0..h {
        for x in 2..w - 2 {
            dst.set(y, x, (src.at(y, x - 1) + src.at(y, x + 1)) * 0.5);
        }
    }
}

fn v_average(dst: &mut Plane, src: &Plane) {
    let (w, h) = (src.w, src.h);
    if h <= 4 {
        return;
    }
    for y in 2..h - 2 {
        for x in 0..w {
            dst.set(y, x, (src.at(y - 1, x) + src.at(y + 1, x)) * 0.5);
        }
    }
}

#[inline(always)]
fn soft_weight(a: f32, b: f32, c: f32) -> f32 {
    let den = (a - c).abs() + (b - c).abs();
    if den == 0.0 {
        0.5
    } else {
        (b - c).abs() / den
    }
}

#[inline(always)]
fn blend(base: f32, other: f32, a: f32, b: f32, w: f32) -> f32 {
    let t = a + b;
    let u = other - t;
    fma(w, u, base)
}

fn chroma_candidates(
    h121: &Plane,
    v121: &Plane,
    havg: &Plane,
    vavg: &Plane,
) -> (Plane, Plane, Plane, Plane) {
    let (w, h) = (h121.w, h121.h);
    let mut oa = Plane::new(w, h);
    let mut ob = Plane::new(w, h);
    let mut oc = Plane::new(w, h);
    let mut od = Plane::new(w, h);
    if w <= 4 || h <= 4 {
        return (oa, ob, oc, od);
    }
    let hh = h121;
    let vv = v121;
    let ha = havg;
    let va = vavg;
    // block 1: even rows
    for y in (2..h - 2).step_by(2) {
        for x in (2..w - 2).step_by(2) {
            let hd = ha.at(y + 1, x) - hh.at(y + 1, x);
            let wt = soft_weight(hh.at(y - 1, x), hh.at(y + 1, x), ha.at(y, x));
            oa.set(y, x, hh.at(y, x) - ha.at(y, x));
            ob.set(y, x, vv.at(y, x) - va.at(y, x));
            oc.set(y, x, blend(hd, ha.at(y - 1, x), hh.at(y - 1, x), hd, wt));
            let vd = va.at(y, x + 1) - vv.at(y, x + 1);
            let wt = soft_weight(vv.at(y, x - 1), vv.at(y, x + 1), va.at(y, x));
            od.set(y, x, blend(vd, va.at(y, x - 1), vv.at(y, x - 1), vd, wt));
        }
        for x in (3..w - 1).step_by(2) {
            let hd = hh.at(y + 1, x) - ha.at(y + 1, x);
            let wt = soft_weight(ha.at(y - 1, x), ha.at(y + 1, x), hh.at(y, x));
            oa.set(y, x, ha.at(y, x) - hh.at(y, x));
            let vd = vv.at(y, x + 1) - va.at(y, x + 1);
            let wv = soft_weight(va.at(y, x - 1), va.at(y, x + 1), vv.at(y, x));
            ob.set(y, x, blend(vd, vv.at(y, x - 1), va.at(y, x - 1), vd, wv));
            oc.set(y, x, blend(hd, hh.at(y - 1, x), ha.at(y - 1, x), hd, wt));
            od.set(y, x, va.at(y, x) - vv.at(y, x));
        }
    }
    // block 2: odd rows
    for y in (3..h - 1).step_by(2) {
        for x in (2..w - 2).step_by(2) {
            let hd = hh.at(y + 1, x) - ha.at(y + 1, x);
            let wt = soft_weight(ha.at(y - 1, x), ha.at(y + 1, x), hh.at(y, x));
            oa.set(y, x, blend(hd, hh.at(y - 1, x), ha.at(y - 1, x), hd, wt));
            ob.set(y, x, va.at(y, x) - vv.at(y, x));
            oc.set(y, x, ha.at(y, x) - hh.at(y, x));
            let vd = vv.at(y, x + 1) - va.at(y, x + 1);
            let wv = soft_weight(va.at(y, x - 1), va.at(y, x + 1), vv.at(y, x));
            od.set(y, x, blend(vd, vv.at(y, x - 1), va.at(y, x - 1), vd, wv));
        }
        for x in (3..w - 1).step_by(2) {
            let hd = ha.at(y + 1, x) - hh.at(y + 1, x);
            let wt = soft_weight(hh.at(y - 1, x), hh.at(y + 1, x), ha.at(y, x));
            oa.set(y, x, blend(hd, ha.at(y - 1, x), hh.at(y - 1, x), hd, wt));
            let vd = va.at(y, x + 1) - vv.at(y, x + 1);
            let wv = soft_weight(vv.at(y, x - 1), vv.at(y, x + 1), va.at(y, x));
            ob.set(y, x, blend(vd, va.at(y, x - 1), vv.at(y, x - 1), vd, wv));
            oc.set(y, x, hh.at(y, x) - ha.at(y, x));
            od.set(y, x, vv.at(y, x) - va.at(y, x));
        }
    }
    (oa, ob, oc, od)
}

fn build_chroma(src: &Plane, weight: &Plane) -> (Plane, Plane) {
    let (w, h) = (src.w, src.h);
    let mut h121 = Plane::new(w, h);
    h_121(&mut h121, src);
    let mut v121 = Plane::new(w, h);
    v_121(&mut v121, src);
    let mut havg = Plane::new(w, h);
    h_average(&mut havg, src);
    let mut vavg = Plane::new(w, h);
    v_average(&mut vavg, src);
    let (oa, ob, oc, od) = chroma_candidates(&h121, &v121, &havg, &vavg);
    let mut rg = Plane::new(w, h);
    lerp1(&mut rg, &oa, &ob, weight, 0, h, 0, w);
    let mut bg = Plane::new(w, h);
    lerp1(&mut bg, &oc, &od, weight, 0, h, 0, w);
    (rg, bg)
}

// ---------------------------------------------------------------------------
// FFI

#[repr(C)]
#[derive(Clone, Copy)]
pub struct ItpParams {
    pub k1: f32,
    pub k2: f32,
    pub k3: f32,
    pub k4: f32,
    pub k5: f32,
    pub k6: f32,
    pub blend_k: f32,
    pub blend_lo: f32,
    pub blend_hi: f32,
    pub blend_half: f32,
}

/// Debug hook: write one intermediate plane (f32) for a tile.
#[no_mangle]
pub unsafe extern "C" fn itp_debug_u16(
    raw: *const u16,
    w: usize,
    h: usize,
    black: *const i32,
    gains: *const f32,
    scale_7311: i32,
    table1: *const f32,
    table2: *const f32,
    k1: *const f32,
    k2: *const f32,
    params: *const ItpParams,
    stage: i32,
    out: *mut f32,
) -> i32 {
    if raw.is_null() || w < 12 || h < 12 || params.is_null() {
        return -1;
    }
    let black = [*black, *black.add(1), *black.add(2), *black.add(3)];
    let gains = [*gains, *gains.add(1), *gains.add(2), *gains.add(3)];
    let table1: [f32; 5] = slice::from_raw_parts(table1, 5).try_into().unwrap();
    let table2: [f32; 5] = slice::from_raw_parts(table2, 5).try_into().unwrap();
    let k1: [f32; 9] = slice::from_raw_parts(k1, 9).try_into().unwrap();
    let k2: [f32; 9] = slice::from_raw_parts(k2, 9).try_into().unwrap();
    let p = &*params;
    let raw = slice::from_raw_parts(raw, w * h);
    let src = itp_scale(raw, w, h, &black, &gains, scale_7311);
    let mut abs_h = Plane::new(w, h);
    let mut abs_v = Plane::new(w, h);
    let mut p8 = Plane::new(w, h);
    match stage {
        0 => p8 = src.clone(),
        1 => {
            h_second_diff(&mut abs_h, &src);
            p8 = abs_h;
        }
        2 => {
            v_second_diff(&mut abs_v, &src);
            p8 = abs_v;
        }
        3 => {
            h_second_diff(&mut abs_h, &src);
            fill_candidates(&mut p8, &abs_h);
        }
        18 | 19 | 20 => {
            for y in (0..h).step_by(2) {
                for x in (1..w).step_by(2) {
                    p8.set(y, x, src.at(y, x));
                }
            }
            for y in (1..h).step_by(2) {
                for x in (0..w).step_by(2) {
                    p8.set(y, x, src.at(y, x));
                }
            }
            if stage == 19 {
                let mut five = Plane::new(w, h);
                five_by_five(&mut five, &p8);
                p8 = five;
            } else if stage == 20 {
                let mut dm = Plane::new(w, h);
                direction_map(&mut dm, &p8, p);
                p8 = dm;
            }
        }
        10..=17 => {
            let mut h121 = Plane::new(w, h);
            let mut v121 = Plane::new(w, h);
            let mut ha = Plane::new(w, h);
            let mut va = Plane::new(w, h);
            h_121(&mut h121, &src);
            v_121(&mut v121, &src);
            h_average(&mut ha, &src);
            v_average(&mut va, &src);
            match stage {
                10 => p8 = h121,
                11 => p8 = v121,
                12 => p8 = ha,
                13 => p8 = va,
                _ => {
                    let (oa, ob, oc, od) = chroma_candidates(&h121, &v121, &ha, &va);
                    p8 = match stage {
                        14 => oa,
                        15 => ob,
                        16 => oc,
                        _ => od,
                    };
                }
            }
        }
        _ => {
            let (ca, cb, cc) = build_candidates(&src, &table1, &table2, p);
            match stage {
                4 => p8 = ca,
                5 => p8 = cb,
                6 => p8 = cc,
                7 => p8 = refine_green(&src, &ca, &cc, &k1, &k2, p),
                8 | 9 => {
                    let (rg, bg) = build_chroma(&src, &cb);
                    p8 = if stage == 8 { rg } else { bg };
                }
                22..=27 => {
                    let mut mix_a = Plane::new(w, h);
                    let mut mix_b = Plane::new(w, h);
                    mix_121_average(&mut mix_a, &mut mix_b, &src);
                    let mut tap_a = Plane::new(w, h);
                    let mut tap_b = Plane::new(w, h);
                    nine_tap(&mut tap_a, &mut tap_b, &src, &k1, &k2);
                    let mut avg_a = Plane::new(w, h);
                    let mut avg_b = Plane::new(w, h);
                    pair_average(&mut avg_a, &mut avg_b, &mix_a, &mix_b, &tap_a, &tap_b);
                    p8 = match stage {
                        22 => avg_a,
                        23 => avg_b,
                        24 => tap_a,
                        25 => tap_b,
                        26 => mix_a,
                        _ => mix_b,
                    };
                }
                _ => return -1,
            }
        }
    }
    let o = slice::from_raw_parts_mut(out, w * h);
    o.copy_from_slice(&p8.d);
    0
}

/// One tile: raw is h x w uint16 (including the halo).  Outputs are full
/// h x w planes; the caller takes the [halo, halo+tile) inset.
#[no_mangle]
pub unsafe extern "C" fn itp_process_u16(
    raw: *const u16,
    w: usize,
    h: usize,
    black: *const i32,
    gains: *const f32,
    scale_7311: i32,
    table1: *const f32,
    table2: *const f32,
    k1: *const f32,
    k2: *const f32,
    params: *const ItpParams,
    r_out: *mut u16,
    g_out: *mut u16,
    b_out: *mut u16,
) -> i32 {
    if raw.is_null() || w < 12 || h < 12 || params.is_null() {
        return -1;
    }
    let black = [*black, *black.add(1), *black.add(2), *black.add(3)];
    let gains = [*gains, *gains.add(1), *gains.add(2), *gains.add(3)];
    let table1 = slice::from_raw_parts(table1, 5);
    let table2 = slice::from_raw_parts(table2, 5);
    let k1 = slice::from_raw_parts(k1, 9);
    let k2 = slice::from_raw_parts(k2, 9);
    let table1: [f32; 5] = table1.try_into().unwrap();
    let table2: [f32; 5] = table2.try_into().unwrap();
    let k1: [f32; 9] = k1.try_into().unwrap();
    let k2: [f32; 9] = k2.try_into().unwrap();
    let p = &*params;
    let raw = slice::from_raw_parts(raw, w * h);
    let ro = slice::from_raw_parts_mut(r_out, w * h);
    let go = slice::from_raw_parts_mut(g_out, w * h);
    let bo = slice::from_raw_parts_mut(b_out, w * h);
    itp_tile_into(
        raw, w, h, &black, &gains, scale_7311, &table1, &table2, &k1, &k2, p, ro, go, bo,
    );
    0
}

/// One ITP tile into caller-provided planes.
pub(crate) unsafe fn itp_tile_into(
    raw: &[u16],
    w: usize,
    h: usize,
    black: &[i32; 4],
    gains: &[f32; 4],
    scale_7311: i32,
    table1: &[f32; 5],
    table2: &[f32; 5],
    k1: &[f32; 9],
    k2: &[f32; 9],
    p: &ItpParams,
    r_out: &mut [u16],
    g_out: &mut [u16],
    b_out: &mut [u16],
) {
    let src = itp_scale(raw, w, h, black, gains, scale_7311);
    let (cand_a, cand_b, cand_c) = build_candidates(&src, table1, table2, p);
    let g = refine_green(&src, &cand_a, &cand_c, k1, k2, p);
    let (rg, bg) = build_chroma(&src, &cand_b);
    for i in 0..w * h {
        let gv = g.d[i];
        r_out[i] = (rg.d[i] + gv).min(16383.0).max(0.0) as u16;
        g_out[i] = gv.min(16383.0).max(0.0) as u16;
        b_out[i] = (bg.d[i] + gv).min(16383.0).max(0.0) as u16;
    }
}

// ---------------------------------------------------------------------------
// Whole-image driver: edge extension, tile grid and parallel tile processing.

const ITP_TABLE: [f32; 5] = [1.0, 1.0, 0.125, 0.5, 0.5];
const K9_A: [f32; 9] = [-0.0625, 0.0, 0.25, 0.0, 0.625, 0.0, 0.25, 0.0, -0.0625];
const K9_B: [f32; 9] = [
    0.0,
    -0.0556640625,
    0.0,
    0.5556640625,
    0.0,
    0.5556640625,
    0.0,
    -0.0556640625,
    0.0,
];

fn edge_indices(n: usize, pad: usize) -> Vec<usize> {
    let nn = n as i64;
    let mut out = Vec::with_capacity(n + 2 * pad);
    for idx in -(pad as i64)..(nn + pad as i64) {
        let v = if idx < 0 {
            (idx & 1) as usize
        } else if idx >= nn {
            ((nn - 1) - (((nn - 1) ^ idx) & 1)) as usize
        } else {
            idx as usize
        };
        out.push(v);
    }
    out
}

/// Run ITP over the whole raw crop.  `raw` is h x w uint16; the halo extension
/// and the 1024x1024 tile grid are built here, and tiles run on scoped threads.
#[no_mangle]
pub unsafe extern "C" fn itp_full_image_u16(
    raw: *const u16,
    w: usize,
    h: usize,
    black: *const i32,
    gains: *const f32,
    scale_7311: i32,
    params: *const ItpParams,
    r_out: *mut u16,
    g_out: *mut u16,
    b_out: *mut u16,
    tile: usize,
    halo: usize,
    threads: usize,
) -> i32 {
    if raw.is_null() || params.is_null() || w < 12 || h < 12 || tile == 0 || halo == 0 {
        return -1;
    }
    let black = [*black, *black.add(1), *black.add(2), *black.add(3)];
    let gains = [*gains, *gains.add(1), *gains.add(2), *gains.add(3)];
    let p = *params;
    let src = slice::from_raw_parts(raw, w * h);

    let rows = edge_indices(h, halo);
    let cols = edge_indices(w, halo);
    let pw = w + 2 * halo;
    let ph = h + 2 * halo;
    let mut padded = vec![0u16; pw * ph];
    for (yy, &ry) in rows.iter().enumerate() {
        for (xx, &cx) in cols.iter().enumerate() {
            padded[yy * pw + xx] = src[ry * w + cx];
        }
    }

    let mut jobs = Vec::new();
    let mut y0 = 0usize;
    while y0 < h {
        let y1 = (y0 + tile).min(h);
        let mut x0 = 0usize;
        while x0 < w {
            let x1 = (x0 + tile).min(w);
            jobs.push((y0, x0, y1, x1));
            x0 += tile;
        }
        y0 += tile;
    }

    let nthreads = threads.max(1).min(8).min(jobs.len());
    let next = AtomicUsize::new(0);
    let next = &next;
    let padded = &padded;
    let jobs = &jobs;
    let r_addr = r_out as usize;
    let g_addr = g_out as usize;
    let b_addr = b_out as usize;

    std::thread::scope(|s| {
        for _ in 0..nthreads {
            s.spawn(move || {
                let mut crop: Vec<u16> = Vec::new();
                let mut or: Vec<u16> = Vec::new();
                let mut og: Vec<u16> = Vec::new();
                let mut ob: Vec<u16> = Vec::new();
                loop {
                    let j = next.fetch_add(1, Ordering::Relaxed);
                    if j >= jobs.len() {
                        break;
                    }
                    let (ty0, tx0, ty1, tx1) = jobs[j];
                    let ch = ty1 - ty0;
                    let cw = tx1 - tx0;
                    let crop_w = cw + 2 * halo;
                    let crop_h = ch + 2 * halo;
                    let nc = crop_w * crop_h;
                    crop.resize(nc, 0);
                    or.resize(nc, 0);
                    og.resize(nc, 0);
                    ob.resize(nc, 0);
                    for yy in 0..crop_h {
                        let src_row = (ty0 + yy) * pw + tx0;
                        crop[yy * crop_w..(yy + 1) * crop_w]
                            .copy_from_slice(&padded[src_row..src_row + crop_w]);
                    }
                    itp_tile_into(
                        &crop, crop_w, crop_h, &black, &gains, scale_7311, &ITP_TABLE, &ITP_TABLE,
                        &K9_A, &K9_B, &p, &mut or, &mut og, &mut ob,
                    );
                    let orp = r_addr as *mut u16;
                    let ogp = g_addr as *mut u16;
                    let obp = b_addr as *mut u16;
                    for yy in 0..ch {
                        let dst = (ty0 + yy) * w + tx0;
                        let sb = (yy + halo) * crop_w + halo;
                        for xx in 0..cw {
                            *orp.add(dst + xx) = or[sb + xx];
                            *ogp.add(dst + xx) = og[sb + xx];
                            *obp.add(dst + xx) = ob[sb + xx];
                        }
                    }
                }
            });
        }
    });
    0
}
