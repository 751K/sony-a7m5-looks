//! One-time LUT construction used by the look.
//!
//! * MainGamma: 65536-entry int16 LUT (sony_look.build_main_gamma).
//! * Effect: the four gamma/sRGB 16384-entry int16 tables
//!   (sony_effect.tables, FUN_1000db9f4).
//!
//! These are pure table builders; the pixel code only reads the result.

use std::slice;

/// MainGamma LUT.  `second` is the 1025-entry contrast/highlight/shadow curve.
#[no_mangle]
pub unsafe extern "C" fn build_main_gamma_i16(
    x_knots: *const i32,
    y_knots: *const i32,
    n: i32,
    second: *const i32,
    out: *mut i16,
) -> i32 {
    if x_knots.is_null() || y_knots.is_null() || second.is_null() || out.is_null() || n < 1 {
        return -1;
    }
    let n = n as usize;
    let x = slice::from_raw_parts(x_knots, n);
    let y = slice::from_raw_parts(y_knots, n);
    let second = slice::from_raw_parts(second, 1025);
    let lut = slice::from_raw_parts_mut(out, 0x10000);

    let mut x_prev = 0i32;
    let mut y_prev = 0i32;
    let mut x_cur = x[0];
    let mut y_cur = y[0];
    let mut idx = 0usize;
    // The reference LUT is zero-initialised; index 0x8000 (input 0) is not
    // written by the loop and feeds the mirror at 0x7fff.
    lut[0x8000] = 0;

    for i in 1..0x8000usize {
        let xx = (i as i32) * 0x80;
        if xx > x_cur {
            let mut clamp = false;
            if idx < n && x[idx] <= xx {
                loop {
                    if idx == n - 1 {
                        clamp = true;
                        break;
                    }
                    if x[idx + 1] > xx {
                        idx += 1;
                        break;
                    }
                    idx += 1;
                }
            } else if idx >= n {
                clamp = true;
            }
            if clamp {
                x_prev = x[n - 1];
                x_cur = x_prev;
                y_prev = y[n - 1];
                y_cur = y_prev;
            } else {
                x_prev = x_cur;
                x_cur = x[idx];
                y_prev = y_cur;
                y_cur = y[idx];
            }
        }
        let yy = if x_cur - x_prev != 0 {
            let v = ((y_prev as f32 - y_cur as f32) * (x_cur - xx) as f32) as f32;
            let v = (v / (x_cur - x_prev) as f32) + y_cur as f32;
            v.trunc() as i32
        } else {
            y_prev
        };

        let mut u = ((yy as i64) >> 2) & 0xffffffff;
        if yy < 0x3ff00 {
            let ti = (yy >> 8) as i64;
            let ti = if ti < 0 { 1025 + ti } else { ti };
            let t0 = second[ti as usize] as f32;
            let t1 = second[(ti as usize + 1).min(1024)] as f32;
            let mut frac = (((yy as i64) >> 2) + ((yy as i64) >> 8) * -0x40) & 0xffffffff;
            if frac & (1 << 31) != 0 {
                frac -= 1 << 32;
            }
            let v = (frac as f32 * 0.015625f32).mul_add(t1 - t0, t0);
            u = (v.trunc() as i64) & 0xffffffff;
        } else {
            let t0 = second[1023] as f32;
            let mut frac = (((yy as i64) >> 2) + ((yy as i64) >> 8) * -0x40) & 0xffffffff;
            if frac & (1 << 31) != 0 {
                frac -= 1 << 32;
            }
            let v = (frac as f32 * 0.015625f32).mul_add(0.0, t0);
            u = (v.trunc() as i64) & 0xffffffff;
        }
        lut[0x8000 + i] = (((u >> 2) & 0xffff) as u16) as i16;
    }
    for k in 0..0x8000usize {
        let v = (-(lut[0xffff - k] as i32)) & 0xffff;
        lut[k] = v as u16 as i16;
    }
    0
}

#[inline(always)]
fn bits(u: u32) -> f32 {
    f32::from_bits(u)
}

#[inline(always)]
fn q14(x: f32) -> i16 {
    if !x.is_finite() {
        return 0;
    }
    let v = ((x as f64) * 16384.0).trunc() as i32;
    v.min(0x7fff) as i16
}

/// The four Effect colour-space tables (FUN_1000db9f4).
#[no_mangle]
pub unsafe extern "C" fn effect_tables_i16(
    g_dec: *mut i16,
    g_enc: *mut i16,
    s_enc: *mut i16,
    s_dec: *mut i16,
) -> i32 {
    if g_dec.is_null() || g_enc.is_null() || s_enc.is_null() || s_dec.is_null() {
        return -1;
    }
    let g_dec = slice::from_raw_parts_mut(g_dec, 16384);
    let g_enc = slice::from_raw_parts_mut(g_enc, 16384);
    let s_enc = slice::from_raw_parts_mut(s_enc, 16384);
    let s_dec = slice::from_raw_parts_mut(s_dec, 16384);

    let k_g = bits(0x400cc000); // 2.19921875
    let k_ge = bits(0x3ee8cf59); // 1/2.19921875
    let k_s = bits(0x3ed55555); // 1/2.4
    let b_enc = bits(0x3f870a3d);
    let c_enc = bits(0xbd6147ae);
    let lin_enc = bits(0x3a4eb852);
    let a_dec = bits(0x3872a76f);
    let b_dec = bits(0x3d55891a);
    let k_dec = bits(0x4019999a); // 2.4
    let lin_dec = bits(0x369e8391);
    let x_scale = bits(0x38800000); // 6.1035156e-05

    for v in 0..16384usize {
        let x = (v as f32) * x_scale;
        if x > 0.0 {
            g_dec[v] = q14(x.powf(k_g));
            g_enc[v] = q14(x.powf(k_ge));
        }
        if x > 0.0031308f32 {
            s_enc[v] = q14(x.powf(k_s).mul_add(b_enc, c_enc));
        } else {
            s_enc[v] = q14((v as f32) * lin_enc);
        }
        if x > 0.04045f32 {
            s_dec[v] = q14((v as f32).mul_add(a_dec, b_dec).powf(k_dec));
        } else {
            s_dec[v] = q14((v as f32) * lin_dec);
        }
    }
    0
}
