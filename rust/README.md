# Optional Rust render core

`sonylook_core` fuses the per-pixel half of the full-resolution band loop:

```
blur_rows -> hue index -> LinearMatrix16 -> MainGamma -> RGB2YCC
          -> AreaComp -> ChromaSuppress
Sharpness -> YGamma -> Spica -> YCC2RGB
GeoCorr (distortion + lateral CA, u16 bilinear path)
ITP demosaic (whole image: edge extension, 1024x1024 tile grid and parallel tiles all in Rust)
Marble (one slab per call; the luma path; the NR/chroma path falls back to NumPy)
SSCS (integer path; the float preview path stays on NumPy)
MainGamma and Effect table construction
```

The Python/NumPy implementation stays the reference and the default backend.
The Rust path is bit-identical to it (verified stage by stage and by comparing
the SHA-256 of a whole 7008x4672 decode), and is opt-in.

## Build

```bash
cargo build --release --manifest-path rust/sonylook_core/Cargo.toml
```

The shared library is written to `rust/sonylook_core/target/release/`
(`libsonylook_core.dylib` on macOS, `libsonylook_core.so` elsewhere).
`sonylook/fused_rust.py` finds it automatically; set `SONYLOOK_RUST_LIB` to
use a different path.

## Use

```bash
python3 sonylook/sony_decode.py /path/to/A7M5.ARW --rust --out out.tif
```

or from Python:

```python
from sony_decode import decode
img = decode("A7M5.ARW", rust=True)
```

`SONYLOOK_RUST=1` enables it for every `Look` as well.

## Notes

- All float32 operations keep the original evaluation order. Fused
  multiply-adds use `f32::mul_add`, which lowers to the hardware FMA
  (`fmadd`) on aarch64, so the rounding matches the original ARM64 code.
- The kernel consumes the uint16 band produced by SSCS (or the front end) and
  returns the planes that `Look.finish` feeds to Marble / Effect / ToneCurve.
- ITP's 1024x1024 tiles, Spica's row loops, Marble's row slabs and the render
  bands run on multiple threads (the work items are independent, and the output
  is unchanged).  `SONYLOOK_THREADS` is the total budget (default 6); the band
  pool uses `SONYLOOK_BAND_THREADS` (default min(6, bands)) and gives each band
  `budget // workers` Spica threads, so the total in flight stays bounded.
  `1` forces the sequential path.  Marble has its own
  `SONYLOOK_MARBLE_THREADS` override because each concurrent slab holds
  ~200 MB of int64 planes: reducing it trades speed for peak memory.
- In band-parallel mode the `render.look` / `render.spatial` entries in the
  timing dict are sums across workers (CPU time), so they can exceed the wall
  time of `render`; the parent entry is the wall time.
- MainGamma and the four Effect tables are built in Rust (`tables.rs`).
  LinearMatrix16 and YGamma stay in Python but are cached in-process and on
  disk (`~/.cache/sonylook`), keyed by the look blob, slider values, the Edit
  binary SHA and a cache version.  `SONYLOOK_CACHE=0` disables the disk cache;
  `SONYLOOK_CACHE_DIR` moves it.  Measured `Look` construction: 0.23 s with the
  Python builders, 0.06 s with the Rust ones, 0.015 s from a warm cache.
- Pointwise/LUT-grid renders (`Look.render(pointwise=True)`), float input and
  non-zero Hue/Saturation fall back to NumPy.
- The NumPy path remains the reference used by the differential verifiers; do
  not change the Rust kernel without re-running the whole-image SHA-256 check.

## Measured on an M4 (A7M5, 7008x4672, NR/DRO off)

| Stage | NumPy | Rust |
| --- | ---: | ---: |
| look + rgb2ycc + chroma + sharpness + ygamma + spica + ycc2rgb | 21.0 s | 0.5 s |
| GeoCorr | 6.9 s | 0.5 s |
| ITP | 5.8 s | 0.6 s |
| Marble | 5.0 s | 0.5 s |
| SSCS | 1.4 s | 0.1 s |
| whole render chain incl. Marble | 28.8 s | 1.7 s |
| full decode | 42.1 s | 3.2 s |

The remaining time is ITP (0.6 s), Marble (0.5 s), GeoCorr (0.5 s), WB (0.4 s)
and the one-time tables (~0.06 s cold, ~0.015 s with the disk cache).  When
enabled, ToneCurve / ShadeCorrection add about 3.5 s together, and the
NR/chroma branch of Marble still runs in NumPy.

The Rust render, GeoCorr, ITP, Marble, SSCS and table-builder paths are
verified against the NumPy version (interior and border tiles / slabs, the
full-image SHA-256, and direct table comparisons), all bit-identical.  `itp_debug_u16` and
the `debug_stage` argument of `marble_slab_u16` expose intermediate planes for
that comparison; they are not used by the decode path.  Files with NR enabled
take Marble's chroma path, which is not ported: the FFI returns 0 and the
caller uses NumPy for that slab.
