#!/usr/bin/env python3
"""Read constant tables from the user's own Imaging Edge Edit installation (nothing from it is stored here).

The table addresses in this project belong to Edit 4.0.00.10312, arm64 slice SHA-256 EXPECTED_SHA256.
Location: $SONY_EDIT_BINARY, else the default macOS install path. Universal (fat) binaries are handled;
the arm64 slice is used as is (same bytes as `lipo -thin arm64`).
Set SONY_EDIT_SKIP_CHECK=1 to use another build anyway (addresses will most likely be wrong).
"""
import hashlib
import os
import struct
from functools import lru_cache
from pathlib import Path

DEFAULT = "/Applications/Imaging Edge/Edit.app/Contents/MacOS/Edit"
RESOURCES = "/Applications/Imaging Edge/Edit.app/Contents/Resources"
EXPECTED_SHA256 = "a03b2449625f01003ee41d1a3f42630a20119059f65a74944c35eca81d5da442"
CPU_ARM64 = 0x0100000C


def binary_path():
    return Path(os.environ.get("SONY_EDIT_BINARY", DEFAULT))


def resources_path():
    return Path(os.environ.get("SONY_EDIT_RESOURCES", RESOURCES))


@lru_cache(maxsize=1)
def arm64_slice():
    p = binary_path()
    if not p.exists():
        raise FileNotFoundError(f"Imaging Edge Edit not found at {p}; install Edit 4.0.00.10312 or set SONY_EDIT_BINARY")
    data = p.read_bytes()
    if struct.unpack_from(">I", data)[0] in (0xCAFEBABE, 0xCAFEBABF):        # fat header (big endian)
        magic, n = struct.unpack_from(">II", data)
        wide = magic == 0xCAFEBABF
        for i in range(n):
            if wide:
                cpu, _, off, size = struct.unpack_from(">iiQQ", data, 8 + 32 * i)
            else:
                cpu, _, off, size = struct.unpack_from(">iiII", data, 8 + 20 * i)
            if cpu == CPU_ARM64:
                data = data[off:off + size]
                break
        else:
            raise ValueError(f"{p}: no arm64 slice")
    sha = hashlib.sha256(data).hexdigest()
    if sha != EXPECTED_SHA256 and os.environ.get("SONY_EDIT_SKIP_CHECK") != "1":
        raise RuntimeError(f"{p}: arm64 slice SHA-256 {sha} is not Edit 4.0.00.10312 ({EXPECTED_SHA256}); "
                           "table addresses are version specific (SONY_EDIT_SKIP_CHECK=1 to override)")
    return data


@lru_cache(maxsize=1)
def segments():
    data = arm64_slice()
    magic, cpu, _, _, ncmds, _, _, _ = struct.unpack_from("<8I", data)
    assert magic == 0xFEEDFACF and cpu == CPU_ARM64
    out, off = [], 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd == 0x19:                                                     # LC_SEGMENT_64
            name, vmaddr, vmsize, fileoff, filesize = struct.unpack_from("<16sQQQQ", data, off + 8)
            out.append((name.rstrip(b"\0").decode(), vmaddr, vmsize, fileoff, filesize))
        off += size
    return out


def read_va(va, n):
    for name, vmaddr, vmsize, fileoff, filesize in segments():
        if vmaddr <= va and va + n <= vmaddr + filesize:
            o = fileoff + va - vmaddr
            return arm64_slice()[o:o + n]
    raise ValueError(f"0x{va:x}+{n} is not file-backed")
