"""Creation (birth) time of a directory entry through Linux ``statx(2)``.

``os.stat`` does not expose the birth time on Linux, so the listing asks the
kernel directly. A filesystem or kernel without a recorded birth time yields
``None``; the caller shows the value as unavailable instead of guessing.
"""

from __future__ import annotations

import ctypes
import os

_AT_SYMLINK_NOFOLLOW = 0x100
_STATX_BTIME = 0x800


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_nsec", ctypes.c_uint32), ("reserved", ctypes.c_int32)]


class _Statx(ctypes.Structure):
    # struct statx is 256 bytes; the trailing spare area covers newer fields.
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("spare2", ctypes.c_uint64 * 14),
    ]


def _load_statx():
    try:
        function = ctypes.CDLL(None, use_errno=True).statx
    except (AttributeError, OSError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_Statx)]
    function.restype = ctypes.c_int
    return function


_STATX = _load_statx()


def birth_time(dir_fd: int, name: str | bytes) -> float | None:
    """Return the birth time of ``name`` inside ``dir_fd`` without following links."""
    if _STATX is None:
        return None
    buffer = _Statx()
    raw = name if isinstance(name, bytes) else os.fsencode(name)
    if _STATX(dir_fd, raw, _AT_SYMLINK_NOFOLLOW, _STATX_BTIME, ctypes.byref(buffer)) != 0:
        return None
    if not buffer.stx_mask & _STATX_BTIME or buffer.stx_btime.tv_sec <= 0:
        return None
    return buffer.stx_btime.tv_sec + buffer.stx_btime.tv_nsec / 1_000_000_000
