#!/usr/bin/env python3
"""Camera Raw profiles for the Sony A7M5 looks.

Base: 'SonyA7M5 Linear Base.dcp' - Adobe Standard's ColorMatrix1/2 (white balance stays Adobe's),
ForwardMatrix = linear ProPhoto -> XYZ D50 (so ACR's working RGB is the white-balanced camera RGB itself),
identity ProfileToneCurve, DefaultBlackRender = none, no HueSatMap / LookTable.
Looks: XMP 'Look' presets on that base with an RGBTable (ProPhoto primaries, gamma 1.8).

RGBTable binary (crs_table.py for the string form):
  u32 1, u32 1, u32 dims=3, u32 divisions n, n^3 x (u16 R,G,B) as (value - identity) mod 2^16 with
  R slowest / B fastest and identity round(i*65535/(n-1)), then u32 primaries (0 sRGB, 1 Adobe RGB,
  2 ProPhoto), u32 gamma (1 sRGB, 2 1.8, 3 2.2), u32 gamut, f64 min amount, f64 max amount.
  (Enum meanings inferred from the combinations used by Adobe's own profiles: (0,1) (1,3) (2,2).)
usage: acr_profiles.py probes | looks
"""
import hashlib
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import crs_table as T  # noqa: E402
import dcp  # noqa: E402

OUT = HERE.parent / "profiles" / "camera_raw"
ADOBE_STD = Path("/Library/Application Support/Adobe/CameraRaw/CameraProfiles/Adobe Standard/Sony ILCE-7M5 Adobe Standard.dcp")
MODEL = "Sony ILCE-7M5"
BASE_NAME = "SonyA7M5 Linear Base"
GROUP = "Sony A7M5"
PROPHOTO_TO_XYZ = np.array([[0.7976749, 0.1351917, 0.0313534],
                            [0.2880402, 0.7118741, 0.0000857],
                            [0.0, 0.0, 0.8252100]])
PRIM_PROPHOTO, GAMMA_18 = 2, 2


def write_base():
    a = dcp.read(ADOBE_STD)
    ent = [("UniqueCameraModel", 2, MODEL), ("ProfileName", 2, BASE_NAME),
           ("ColorMatrix1", 10, a["ColorMatrix1"]), ("ColorMatrix2", 10, a["ColorMatrix2"]),
           ("CalibrationIlluminant1", 3, a["CalibrationIlluminant1"]),
           ("CalibrationIlluminant2", 3, a["CalibrationIlluminant2"]),
           ("ForwardMatrix1", 10, PROPHOTO_TO_XYZ), ("ForwardMatrix2", 10, PROPHOTO_TO_XYZ),
           ("ProfileToneCurve", 11, [0.0, 0.0, 1.0, 1.0]), ("ProfileEmbedPolicy", 4, [0]),
           ("DefaultBlackRender", 4, [1]), ("ProfileCopyright", 2, "sony-a7m5-looks (not an Adobe profile)")]
    path = OUT / f"{BASE_NAME}.dcp"
    dcp.write(path, ent)
    return path


def table_bytes(values, prim=PRIM_PROPHOTO, gamma=GAMMA_18):
    """values: (n, n, n, 3) floats in [0,1] indexed [r, g, b] (table input, encoded)."""
    n = values.shape[0]
    ident = np.round(np.arange(n) * 65535 / (n - 1)).astype(np.int64)
    I = np.stack(np.meshgrid(ident, ident, ident, indexing="ij"), -1)
    q = np.round(np.clip(values, 0, 1) * 65535).astype(np.int64)
    body = ((q - I) % 65536).astype("<u2").tobytes()
    return (struct.pack("<4I", 1, 1, 3, n) + body + struct.pack("<3I", prim, gamma, 0)
            + struct.pack("<2d", 0.0, 1.0))


def grid(n):
    x = np.arange(n) / (n - 1)
    return np.stack(np.meshgrid(x, x, x, indexing="ij"), -1)


XMP = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0-c000 1.000000, 0000/00/00-00:00:00        ">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:PresetType="Look"
   crs:Cluster=""
   crs:UUID="{uuid}"
   crs:SupportsAmount="False"
   crs:SupportsColor="True"
   crs:SupportsMonochrome="False"
   crs:SupportsHighDynamicRange="True"
   crs:SupportsNormalDynamicRange="True"
   crs:SupportsSceneReferred="True"
   crs:SupportsOutputReferred="False"
   crs:CameraModelRestriction="{model}"
   crs:Copyright=""
   crs:ContactInfo=""
   crs:Version="13.0"
   crs:ProcessVersion="11.0"
   crs:ConvertToGrayscale="False"
   crs:CameraProfile="{base}"
   crs:RGBTable="{md5}"
   crs:Table_{md5}="{table}"
   crs:HasSettings="True">
   <crs:Name>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{name}</rdf:li>
    </rdf:Alt>
   </crs:Name>
   <crs:ShortName>
    <rdf:Alt>
     <rdf:li xml:lang="x-default"/>
    </rdf:Alt>
   </crs:ShortName>
   <crs:SortName>
    <rdf:Alt>
     <rdf:li xml:lang="x-default"/>
    </rdf:Alt>
   </crs:SortName>
   <crs:Group>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{group}</rdf:li>
    </rdf:Alt>
   </crs:Group>
   <crs:Description>
    <rdf:Alt>
     <rdf:li xml:lang="x-default"/>
    </rdf:Alt>
   </crs:Description>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


def write_look(name, values, **kw):
    data = table_bytes(values, **kw)
    md5 = T.fingerprint(data)
    uuid = hashlib.md5(("SonyA7M5/" + name).encode()).hexdigest().upper()
    path = OUT / f"SonyA7M5 {name}.xmp"
    path.write_text(XMP.format(uuid=uuid, model=MODEL, base=BASE_NAME, md5=md5, table=T.encode(data),
                               name=f"SonyA7M5 {name}", group=GROUP))
    return path


def probes(n=32):
    g = grid(n)
    luma = g @ np.array([0.2880402, 0.7118741, 0.0000857])      # ProPhoto Y weights on encoded values
    return {"Probe": g, "Probe-Sq": g ** 2, "Probe-Mono": np.repeat(luma[..., None], 3, -1)}


def main():
    OUT.mkdir(exist_ok=True)
    what = sys.argv[1] if len(sys.argv) > 1 else "probes"
    print(write_base())
    if what == "probes":
        for k, v in probes().items():
            print(write_look(k, v))


if __name__ == "__main__":
    main()
