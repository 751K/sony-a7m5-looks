//! ZcTaskSSCS, integer path (0x1000f1238..0x1000f1358).
//!
//! Direct port of sony_sscs.sscs_int.  The float path is not ported; it is only
//! used by the float preview / profile path, which stays on NumPy.

use std::slice;

#[repr(C)]
pub struct SscsParams {
    pub t: i32,
    pub tr: i32,
    pub tg: i32,
    pub tb: i32,
    pub wr: i32,
    pub wg: i32,
    pub wb: i32,
}

#[inline(always)]
fn sdiv(a: i64, d: i64) -> i64 {
    if d == 0 {
        return 0; // NumPy integer division by zero yields 0
    }
    let q = a.abs() / d.abs();
    if (a < 0) != (d < 0) {
        -q
    } else {
        q
    }
}

/// z / out are interleaved RGB; `stride` is normally 3.
#[no_mangle]
pub unsafe extern "C" fn sscs_int_u16(
    z: *const u16,
    n: usize,
    stride: usize,
    params: *const SscsParams,
    out: *mut u16,
    out_stride: usize,
) -> i32 {
    if params.is_null() || stride < 3 || out_stride < 3 {
        return -1;
    }
    let p = &*params;
    let z = slice::from_raw_parts(z, n * stride);
    let out = slice::from_raw_parts_mut(out, n * out_stride);
    let (t, tr, tg, tb, wr, wg, wb) = (
        p.t as i64,
        p.tr as i64,
        p.tg as i64,
        p.tb as i64,
        p.wr as i64,
        p.wg as i64,
        p.wb as i64,
    );
    for i in 0..n {
        let base = i * stride;
        let r0 = z[base] as i64;
        let g0 = z[base + 1] as i64;
        let b0 = z[base + 2] as i64;
        let m = (r0 - tr > 0) && (g0 > tg) && (b0 > tb);
        let (r2, g2, b2) = if m {
            let kg = sdiv((g0 - tg) << 7, t - tg).min(128);
            let r_le_b = r0 <= b0;
            let c = if r_le_b { r0 } else { b0 };
            let tc = if r_le_b { tr } else { tb };
            let krb = if c > t {
                128
            } else {
                sdiv((c - tc) << 7, t - tc)
            };
            let f = 128 - ((krb * kg) >> 7);
            let l = wr * r0 + wg * g0 + wb * b0;
            let dr = f * (r0 - g0);
            let db = f * (b0 - g0);
            let g2 = ((l << 7) - dr * wr - db * wb) >> 14;
            let r2 = ((g2 << 7) + dr) >> 7;
            let b2 = ((g2 << 7) + db) >> 7;
            (r2 & 0xffff, g2 & 0xffff, b2 & 0xffff)
        } else {
            (r0, g0, b0)
        };
        let o = i * out_stride;
        out[o] = r2 as u16;
        out[o + 1] = g2 as u16;
        out[o + 2] = b2 as u16;
    }
    0
}
