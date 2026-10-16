#!/usr/bin/env python3
"""Minimal lossless JPEG (ITU T.81 process 14, SOF3) codec for Sony ARW tiles.

A7M5 lossless-compressed ARW: 512x512 raw tiles, each an LJ92 stream of 256 lines x 256 samples x
4 components (precision 14, predictor 1, one Huffman table). Component k of sample (r, c) is raw
pixel (2r + k // 2, 2c + k % 2) of the tile (checked against LibRaw in make_chart.py).
"""
import struct

import numpy as np


def parse(b):
    i, info = 2, {}
    while True:
        assert b[i] == 0xFF
        mk = b[i + 1]
        L = struct.unpack(">H", b[i + 2:i + 4])[0]
        seg = b[i + 4:i + 2 + L]
        if mk == 0xC3:
            info["P"], info["Y"], info["X"], info["Nf"] = seg[0], *struct.unpack(">HH", seg[1:5]), seg[5]
        elif mk == 0xC4:
            info["bits"] = list(seg[1:17]); n = sum(info["bits"]); info["vals"] = list(seg[17:17 + n])
            info["dht"] = b[i:i + 2 + L]
        elif mk == 0xDA:
            info["pred"] = seg[1 + 2 * seg[0]]
            info["scan"] = i + 2 + L
            info["header"] = b[:i + 2 + L]
            return info
        i += 2 + L


def _codes(bits, vals):
    code, k, table = 0, 0, {}
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            table[vals[k]] = (code, length); k += 1; code += 1
        code <<= 1
    return table


def decode(b):
    info = parse(b)
    codes = _codes(info["bits"], info["vals"])
    lookup = {(l, c): v for v, (c, l) in codes.items()}
    data = b[info["scan"]:]
    # unstuff
    raw = bytearray(); j = 0
    while j < len(data):
        x = data[j]
        if x == 0xFF:
            if data[j + 1] == 0x00:
                raw.append(0xFF); j += 2; continue
            break
        raw.append(x); j += 1
    bits = np.unpackbits(np.frombuffer(bytes(raw), np.uint8))
    pos = 0
    Y, X, N, P = info["Y"], info["X"], info["Nf"], info["P"]
    out = np.zeros((Y, X, N), np.int64)
    for r in range(Y):
        for c in range(X):
            for k in range(N):
                code, l = 0, 0
                while True:
                    code = (code << 1) | int(bits[pos]); pos += 1; l += 1
                    if (l, code) in lookup:
                        s = lookup[(l, code)]; break
                if s == 0:
                    diff = 0
                elif s == 16:
                    diff = 32768
                else:
                    v = 0
                    for _ in range(s):
                        v = (v << 1) | int(bits[pos]); pos += 1
                    diff = v if v >= (1 << (s - 1)) else v - (1 << s) + 1
                if r == 0 and c == 0:
                    pred = 1 << (P - 1)
                elif r == 0:
                    pred = out[r, c - 1, k]
                elif c == 0:
                    pred = out[r - 1, c, k]
                else:
                    pred = out[r, c - 1, k]
                out[r, c, k] = (pred + diff) & 0xFFFF
    return out


def encode(samples, header):
    """samples: (Y, X, N) ints; header: bytes up to and including the SOS segment of a template tile
    (same Y, X, N, precision, predictor 1, Huffman table). Returns SOI..EOI bytes."""
    info = parse(header + b"\xff\xd9")
    codes = _codes(info["bits"], info["vals"])
    s = np.asarray(samples, np.int64)
    Y, X, N = s.shape
    pred = np.empty_like(s)
    pred[0, 0, :] = 1 << (info["P"] - 1)
    pred[0, 1:, :] = s[0, :-1, :]
    pred[1:, 0, :] = s[:-1, 0, :]
    pred[1:, 1:, :] = s[1:, :-1, :]
    diff = (s - pred).reshape(-1)
    diff = ((diff + 32768) % 65536) - 32768
    out_bits = []
    for d in diff.tolist():
        a = abs(d)
        cat = 0 if a == 0 else a.bit_length()
        code, l = codes[cat]
        out_bits.append(format(code, f"0{l}b"))
        if cat and cat < 16:
            v = d if d > 0 else d + (1 << cat) - 1
            out_bits.append(format(v, f"0{cat}b"))
    bitstr = "".join(out_bits)
    bitstr += "1" * (-len(bitstr) % 8)
    by = int(bitstr, 2).to_bytes(len(bitstr) // 8, "big") if bitstr else b""
    by = by.replace(b"\xff", b"\xff\x00")
    return header + by + b"\xff\xd9"
