#!/usr/bin/env python3
"""Encode/decode Camera Raw XMP 'crs:Table_<md5>' strings (dng_big_table string form).

String = base-85 (Adobe alphabet, little-endian digits, 4 bytes -> 5 chars, short tail) of
[u32 LE uncompressed length][zlib stream].
"""
import hashlib
import struct
import zlib

ALPHA = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-:+=^!/*?`'|()[]{}@%$#"
DEC = {c: i for i, c in enumerate(ALPHA)}


def b85_decode(s):
    out = bytearray()
    for i in range(0, len(s), 5):
        chunk = s[i:i + 5]
        v = 0
        for k, c in enumerate(chunk):
            v += DEC[c] * 85 ** k
        n = {5: 4, 4: 3, 3: 2, 2: 1}[len(chunk)]
        out += struct.pack("<I", v)[:n]
    return bytes(out)


def b85_encode(b):
    out = []
    for i in range(0, len(b), 4):
        chunk = b[i:i + 4]
        v = struct.unpack("<I", chunk + bytes(4 - len(chunk)))[0]
        nch = len(chunk) + 1
        for _ in range(nch):
            out.append(ALPHA[v % 85]); v //= 85
    return "".join(out)


def decode(s):
    raw = b85_decode(s)
    n = struct.unpack_from("<I", raw)[0]
    data = zlib.decompress(raw[4:])
    assert len(data) == n, (len(data), n)
    return data


def encode(data, level=9):
    return b85_encode(struct.pack("<I", len(data)) + zlib.compress(data, level))


def fingerprint(data):
    return hashlib.md5(data).hexdigest().upper()
