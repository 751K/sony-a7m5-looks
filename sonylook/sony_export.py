#!/usr/bin/env python3
"""Write a decoded image the way Edit exports it: 8/16-bit TIFF or JPEG, with an ICC profile.

The decoder image is 14-bit encoded (0..16383).  Edit's 16-bit TIFF stores exactly
`value << 2` (every sample of the reference export is a multiple of 4; the 16-bit
maximum is 65532), so the 8-bit value is `value >> 6`.  No rounding is involved.

The ICC profiles are the ones bundled with Edit, read from the user's own
installation (SONY_EDIT_RESOURCES, default /Applications/Imaging Edge/Edit.app/
Contents/Resources):

    sRGB      Sony_sRGB.icc            sRGB primaries, 1024-point sRGB TRC
    AdobeRGB  Sony_AdobeRGB_1998.icc   Adobe RGB primaries, gamma 2.19921875

TIFF gets tag 34675, JPEG an APP2 marker, PNG an iCCP chunk.  Edit itself embeds
the profile only for AdobeRGB (byte-identical to Sony_AdobeRGB_1998.icc, EXIF
ColorSpace 0xFFFF); its sRGB TIFFs carry none (ColorSpace 1).  Both are embedded
here, so the file is self-describing.  Checked on DSC03591 (AdobeRGB 16-bit and
8-bit exports): 8-bit = 16-bit >> 8 for all but a few pixels.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

SPACES = {"srgb": "Sony_sRGB.icc", "adobe": "Sony_AdobeRGB_1998.icc"}
ALIASES = {"srgb": "srgb", "adobergb": "adobe", "adobergb1998": "adobe", "adobe": "adobe"}


def normalize_space(space="srgb"):
    """'sRGB', 'AdobeRGB', 'Adobe RGB 1998', ... -> 'srgb' / 'adobe'."""
    key = str(space).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if key in ALIASES:
        return ALIASES[key]
    raise ValueError(f"unknown color space {space!r}; use 'srgb' or 'adobe'")


@lru_cache(maxsize=2)
def icc_profile(space="srgb"):
    """Bytes of Edit's bundled profile for this output space."""
    import edit_binary
    name = SPACES[normalize_space(space)]
    p = edit_binary.resources_path() / name
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found; install Imaging Edge Edit or set SONY_EDIT_RESOURCES")
    return p.read_bytes()


def to_16bit(img):
    """14-bit encoded 0..16383 -> the uint16 values Edit writes (exactly value << 2)."""
    return (np.asarray(img, np.uint16) << 2)


def to_8bit(img):
    """14-bit encoded 0..16383 -> uint8 (Edit's 16-bit value >> 8)."""
    return (np.asarray(img, np.uint16) >> 6).astype(np.uint8)


def write_image(img, path, *, color_space="srgb", bits=None, quality=95):
    """Write (H, W, 3) 14-bit encoded image with the matching ICC profile.

    path: .tif/.tiff (default 16-bit), .jpg/.jpeg (8-bit), .png (default 8-bit).
    bits: 8 or 16; None picks the natural depth for the format.  16-bit is not a
          JPEG option.
    quality: JPEG quality 1..100.
    """
    img = np.asarray(img)
    if img.ndim != 3 or img.shape[2] < 3:
        raise ValueError(f"expected an (H, W, 3) image, got {img.shape}")
    if img.dtype.kind not in "ui":
        raise TypeError(f"expected an integer 14-bit image, got {img.dtype}")
    img = img[..., :3]
    path = Path(path)
    space = normalize_space(color_space)
    icc = icc_profile(space)
    ext = path.suffix.lower()
    is_tiff = ext in (".tif", ".tiff")
    is_jpeg = ext in (".jpg", ".jpeg")
    if not (is_tiff or is_jpeg or ext == ".png"):
        raise ValueError(f"unsupported extension {path.suffix!r}; use .tif, .jpg or .png")
    bits = (16 if is_tiff else 8) if bits is None else int(bits)
    if bits not in (8, 16):
        raise ValueError("bits must be 8 or 16")
    if is_jpeg and bits == 16:
        raise ValueError("JPEG is 8-bit only")
    if ext == ".png" and bits == 16:
        raise ValueError("PNG output is 8-bit; use a .tif for 16-bit")

    arr = to_16bit(img) if bits == 16 else to_8bit(img)
    if is_tiff:
        import tifffile
        # TIFF type 7 (UNDEFINED), as Capture One and the TIFF ICC convention use
        tifffile.imwrite(path, arr, photometric="rgb",
                         extratags=[(34675, 7, len(icc), icc, False)])
    else:
        from PIL import Image
        im = Image.fromarray(arr, "RGB")
        if is_jpeg:
            im.save(path, quality=int(quality), icc_profile=icc)
        else:
            im.save(path, icc_profile=icc)
    return path
