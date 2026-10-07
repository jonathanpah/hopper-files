"""Change notices for the directories that open browser views show.

The service watches only the directories that clients name: the open folder,
expanded tree branches, and the folders of open documents and viewers. It
never walks the base. A directory stops being watched when no request has
named it for ``RELEASE_SECONDS``. Notices come from Linux inotify through the
standard library, one kernel instance per service process.

Every notice gets a sequence number. A client sends back the last number it
received and learns which watched entries changed after it. When the service
restarts (new epoch), the kernel queue overflows, or the retained notices no
longer reach back to the client's number, the client is told to reread
everything it shows.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import select
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from hopper_files.roots import AddressRejected, RootCatalog, readable_directory

WATCH_LIMIT = 4096
NOTICE_LIMIT = 8192
RELEASE_SECONDS = 60.0
# Beyond this many changed names in one directory, a response names the
# directory only; the client rereads it once either way.
NAMES_PER_DIRECTORY = 64
_SWEEP_SECONDS = 5.0

_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_UNMOUNT = 0x00002000
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ONLYDIR = 0x01000000
_IN_EXCL_UNLINK = 0x04000000
_WATCH_MASK = (
    _IN_MODIFY | _IN_ATTRIB | _IN_CLOSE_WRITE | _IN_MOVED_FROM | _IN_MOVED_TO | _IN_CREATE
    | _IN_DELETE | _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_ONLYDIR | _IN_EXCL_UNLINK
)
_SELF_GONE = _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT
_EVENT_HEADER = struct.Struct("iIII")


@dataclass
class _Watch:
    descriptor: int
    identity: tuple[int, int]
    named_at: float


@dataclass(frozen=True)
class Collected:
    seq: int
    resync: bool
    changes: tuple[tuple[str, str | None], ...]


class ChangeWatcher:
    """Kernel watches for named directories and a bounded, numbered notice log."""

    def __init__(self) -> None:
        self.epoch = secrets.token_urlsafe(16)
        self.watch_limit = WATCH_LIMIT
        self.notice_limit = NOTICE_LIMIT
        self.release_seconds = RELEASE_SECONDS
        self._lock = threading.Lock()
        self._inotify = -1
        self._closed = False
        self._libc: ctypes.CDLL | None = None
        self._thread: threading.Thread | None = None
        self._wake_read = -1
        self._wake_write = -1
        self._seq = 0
        # A client whose number is below the floor may have missed a notice.
        self._floor = 0
        self._notices: OrderedDict[tuple[str, str | None], int] = OrderedDict()
        self._watches: dict[str, _Watch] = {}
        self._paths_by_descriptor: dict[int, set[str]] = {}
        self._listeners: set[Callable[[], None]] = set()

    # Lifecycle

    def available(self) -> bool:
        """Start the kernel instance on first use. False when Linux refuses it."""
        with self._lock:
            if self._inotify >= 0:
                return True
            if self._closed:
                return False
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                libc.inotify_init1.argtypes = [ctypes.c_int]
                libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
                libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
                descriptor = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            except (AttributeError, OSError):
                return False
            if descriptor < 0:
                return False
            self._libc = libc
            self._inotify = descriptor
            self._wake_read, self._wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
            self._thread = threading.Thread(target=self._read_loop, name="hopper-changes", daemon=True)
            self._thread.start()
            return True

    def active(self) -> bool:
        return self._inotify >= 0

    def close(self) -> None:
        with self._lock:
            self._closed = True
            thread = self._thread
            if self._wake_write >= 0:
                os.write(self._wake_write, b"x")
        if thread is not None:
            thread.join(timeout=5)
        with self._lock:
            for descriptor in (self._inotify, self._wake_read, self._wake_write):
                if descriptor >= 0:
                    os.close(descriptor)
            self._inotify = self._wake_read = self._wake_write = -1
            self._watches.clear()
            self._paths_by_descriptor.clear()
            listeners = list(self._listeners)
        _notify(listeners)

    # Watches

    def watch(self, catalog: RootCatalog, root_id: str, paths: Iterable[str]) -> list[str]:
        """Watch every named directory. Return the ones that cannot be watched."""
        rejected: list[str] = []
        for path in paths:
            if not self._watch_one(catalog, root_id, path):
                rejected.append(path)
        self.release_unnamed()
        return rejected

    def mention(self, paths: Iterable[str]) -> None:
        """Record that a request still names these directories."""
        now = time.monotonic()
        with self._lock:
            for path in paths:
                watch = self._watches.get(path)
                if watch is not None:
                    watch.named_at = now

    def watched(self) -> list[str]:
        with self._lock:
            return sorted(self._watches)

    def release_unnamed(self) -> None:
        limit = time.monotonic() - self.release_seconds
        with self._lock:
            for path in [path for path, watch in self._watches.items() if watch.named_at < limit]:
                self._forget(path)

    def _watch_one(self, catalog: RootCatalog, root_id: str, path: str) -> bool:
        try:
            with readable_directory(catalog, root_id, path) as (kernel_path, identity):
                with self._lock:
                    if self._inotify < 0:
                        return False
                    known = self._watches.get(path)
                    if known is not None and known.identity == identity:
                        known.named_at = time.monotonic()
                        return True
                    if known is None and len(self._watches) >= self.watch_limit:
                        self._release_locked()
                        if len(self._watches) >= self.watch_limit:
                            return False
                    descriptor = self._libc.inotify_add_watch(self._inotify, os.fsencode(kernel_path), _WATCH_MASK)
                    if descriptor < 0:
                        if known is not None:
                            self._forget(path)
                        return False
                    if known is not None:
                        # Another directory now answers to this address.
                        self._forget(path)
                        self._record(path, None)
                    self._watches[path] = _Watch(descriptor, identity, time.monotonic())
                    self._paths_by_descriptor.setdefault(descriptor, set()).add(path)
                    return True
        except (AddressRejected, OSError):
            with self._lock:
                if path in self._watches:
                    self._forget(path)
            return False

    def _release_locked(self) -> None:
        limit = time.monotonic() - self.release_seconds
        for path in [path for path, watch in self._watches.items() if watch.named_at < limit]:
            self._forget(path)

    def _forget(self, path: str) -> None:
        watch = self._watches.pop(path)
        paths = self._paths_by_descriptor.get(watch.descriptor)
        if paths is None:
            return
        paths.discard(path)
        if not paths:
            del self._paths_by_descriptor[watch.descriptor]
            if self._inotify >= 0:
                self._libc.inotify_rm_watch(self._inotify, watch.descriptor)

    # Notices

    def subscribe(self, listener: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.add(listener)

    def unsubscribe(self, listener: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    def collect(self, epoch: str | None, seq: int | None, paths: set[str]) -> Collected:
        """Return the notices after ``seq`` for ``paths``, or a request to reread."""
        with self._lock:
            if epoch != self.epoch or seq is None or seq > self._seq or seq < self._floor:
                return Collected(self._seq, True, ())
            found: list[tuple[str, str | None]] = []
            for key, number in reversed(self._notices.items()):
                if number <= seq:
                    break
                if key[0] in paths:
                    found.append(key)
            return Collected(self._seq, False, _summarize(found))

    def _record(self, path: str, name: str | None) -> None:
        self._seq += 1
        key = (path, name)
        self._notices[key] = self._seq
        self._notices.move_to_end(key)
        while len(self._notices) > self.notice_limit:
            _key, number = self._notices.popitem(last=False)
            self._floor = max(self._floor, number)

    def _overflowed(self) -> None:
        self._seq += 1
        self._floor = self._seq
        self._notices.clear()

    def _read_loop(self) -> None:
        swept = time.monotonic()
        while True:
            with self._lock:
                if self._closed:
                    return
                inotify, wake = self._inotify, self._wake_read
            try:
                ready, _, _ = select.select([inotify, wake], [], [], _SWEEP_SECONDS)
            except (OSError, ValueError):
                return
            if wake in ready:
                return
            if inotify in ready:
                self._drain(inotify)
            if time.monotonic() - swept >= _SWEEP_SECONDS:
                swept = time.monotonic()
                self.release_unnamed()

    def _drain(self, inotify: int) -> None:
        while True:
            try:
                block = os.read(inotify, 64 * 1024)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                break
            if not block:
                break
            with self._lock:
                if self._closed:
                    return
                self._apply(block)
        with self._lock:
            listeners = list(self._listeners)
        _notify(listeners)

    def _apply(self, block: bytes) -> None:
        offset = 0
        while offset + _EVENT_HEADER.size <= len(block):
            descriptor, mask, _cookie, length = _EVENT_HEADER.unpack_from(block, offset)
            raw = block[offset + _EVENT_HEADER.size : offset + _EVENT_HEADER.size + length]
            offset += _EVENT_HEADER.size + length
            if mask & _IN_Q_OVERFLOW:
                self._overflowed()
                continue
            paths = sorted(self._paths_by_descriptor.get(descriptor, ()))
            if not paths:
                continue
            if mask & (_SELF_GONE | _IN_IGNORED):
                for path in paths:
                    self._record(path, None)
                    if path in self._watches:
                        self._forget(path)
                continue
            name = _entry_name(raw)
            for path in paths:
                self._record(path, name)


def _entry_name(raw: bytes) -> str | None:
    raw = raw.split(b"\0", 1)[0]
    if not raw:
        return None
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _summarize(found: list[tuple[str, str | None]]) -> tuple[tuple[str, str | None], ...]:
    names: dict[str, set[str | None]] = {}
    for path, name in found:
        names.setdefault(path, set()).add(name)
    result: list[tuple[str, str | None]] = []
    for path in sorted(names):
        entries = names[path]
        if len(entries) > NAMES_PER_DIRECTORY:
            result.append((path, None))
            continue
        if None in entries:
            result.append((path, None))
        result.extend((path, name) for name in sorted(item for item in entries if item is not None))
    return tuple(result)


def _notify(listeners: list[Callable[[], None]]) -> None:
    for listener in listeners:
        try:
            listener()
        except RuntimeError:
            # The listener's event loop has already stopped.
            pass
