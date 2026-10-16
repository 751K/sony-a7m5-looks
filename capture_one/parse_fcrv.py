#!/usr/bin/env python3
"""Parse Capture One .fcrv film-curve files (read only).

Layout (little endian): 'II' u16 version, then curve A (u32 n, n x (u32 x, u32 y) as unsigned 0.32
fixed point, 0xffffffff = 1), u32, name (u32 len + bytes) twice, u32, f32 1.0, u32, u32, u32 tag,
curve B, curve C.
usage: parse_fcrv.py FILE.fcrv ...  -> prints the control points
"""
import struct
import sys

S = 2.0 ** 32 - 1


def curve(b, o):
    n = struct.unpack_from("<I", b, o)[0]; o += 4
    pts = [(x / S, y / S) for x, y in struct.iter_unpack("<II", b[o:o + 8 * n])]
    return pts, o + 8 * n


def parse(b):
    assert b[:2] == b"II"
    ver = struct.unpack_from("<H", b, 2)[0]
    A, o = curve(b, 4 + 2)
    k = struct.unpack_from("<I", b, o)[0]; o += 4
    names = []
    for _ in range(2):
        L = struct.unpack_from("<I", b, o)[0]; names.append(b[o + 4:o + 4 + L].decode()); o += 4 + L
    meta = struct.unpack_from("<IfII", b, o); o += 16
    tag = struct.unpack_from("<I", b, o)[0]; o += 4
    B, o = curve(b, o)
    C, o = curve(b, o)
    return dict(version=ver, k=k, names=names, meta=meta, tag=tag, A=A, B=B, C=C, rest=len(b) - o)


if __name__ == "__main__":
    for f in sys.argv[1:]:
        r = parse(open(f, "rb").read())
        print("==", f.split("/")[-1], "ver", r["version"], r["names"][0], "meta", r["meta"], "tag", r["tag"], "rest", r["rest"])
        for key in "ABC":
            print(f"  {key}:", " ".join(f"({x:.4f},{y:.4f})" for x, y in r[key]))
