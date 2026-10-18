"""Small in-process + on-disk cache for the one-time look tables.

The tables are deterministic functions of the ARW look tags, the slider values
and (for the LinearMatrix16 table) the Edit binary constants.  Keys hash all of
those inputs plus CACHE_VERSION, so a stale file is never used.  Corrupt or
unreadable files simply fall back to the builder.

Disable with SONYLOOK_CACHE=0; override the directory with
SONYLOOK_CACHE_DIR (default ~/.cache/sonylook).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import numpy as np

CACHE_VERSION = 1
_MEM = {}


def _enabled():
    return os.environ.get("SONYLOOK_CACHE", "1") != "0"


def _dir():
    d = os.environ.get("SONYLOOK_CACHE_DIR")
    return Path(d) if d else Path.home() / ".cache" / "sonylook"


def _key(name, parts):
    h = hashlib.sha256()
    h.update(f"v{CACHE_VERSION}:{name}:".encode())
    for p in parts:
        if isinstance(p, np.ndarray):
            h.update(p.tobytes())
        elif isinstance(p, bytes):
            h.update(p)
        else:
            h.update(repr(p).encode())
        h.update(b"|")
    return h.hexdigest()[:32]


def _ro(a):
    a.setflags(write=False)
    return a


def load_or_build(name, parts, builder, shape, dtype):
    """Return a cached array, or call builder() and store the result."""
    dtype = np.dtype(dtype)
    key = (name, _key(name, parts))
    hit = _MEM.get(key)
    if hit is not None:
        return hit
    if not _enabled():
        a = _ro(np.ascontiguousarray(builder(), dtype))
        _MEM[key] = a
        return a

    path = _dir() / f"{name}_{key[1]}.npy"
    try:
        a = np.load(path, allow_pickle=False)
        if a.shape == shape and a.dtype == dtype:
            a = _ro(np.ascontiguousarray(a))
            _MEM[key] = a
            return a
    except Exception:
        pass

    a = _ro(np.ascontiguousarray(builder(), dtype))
    if a.shape != shape:
        raise ValueError(f"{name}: builder returned shape {a.shape}, expected {shape}")
    _MEM[key] = a
    tmp = None
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_dir()), suffix=".npy")
        with os.fdopen(fd, "wb") as f:
            np.save(f, a)          # a file object: no ".npy" is appended
        os.replace(tmp, path)
        tmp = None
    except Exception:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return a
