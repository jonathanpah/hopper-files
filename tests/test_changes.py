"""Change notices for the directories a view shows (api/changes)."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hopper_files.api.changes
import hopper_files.changes
from hopper_files.changes import ChangeWatcher

from conftest import CLOCK_START, DOCUMENTS, PASSWORD, ROOT, ManualClock, boot, doc

pytestmark = pytest.mark.skipif(not Path("/proc/sys/fs/inotify").is_dir(), reason="inotify is not available")


@pytest.fixture
def world(tmp_path):
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as running:
        login = running.post_login(PASSWORD)
        assert login.status_code == 200, login.text
        running.headers = {"origin": running.config.origin, "x-csrf-token": login.json()["csrfToken"]}
        yield running


@pytest.fixture
def pool():
    with ThreadPoolExecutor(max_workers=4) as executor:
        yield executor


def _ask(world, paths, epoch=None, seq=None, **extra):
    body = {"rootId": ROOT, "paths": paths, "epoch": epoch, "seq": seq, **extra}
    return world.client.post("/api/changes", headers=world.headers, json=body)


def _start(world, paths):
    """First request: always a reread, and the named directories are watched."""
    response = _ask(world, paths)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["supported"] is True
    assert payload["resync"] is True
    assert payload["changes"] == []
    return payload


def _next(world, pool, paths, state):
    return pool.submit(_timed_ask, world, paths, state["epoch"], state["seq"])


def _timed_ask(world, paths, epoch, seq):
    response = _ask(world, paths, epoch, seq)
    return response, time.monotonic()


def _settle():
    """Give the pending request time to reach its wait."""
    time.sleep(0.3)


def _answer(future, changed_at):
    response, answered_at = future.result(timeout=10)
    assert response.status_code == 200, response.text
    assert answered_at - changed_at < 1.0
    return response.json()


def _kernel_watches(watcher: ChangeWatcher) -> int:
    lines = Path(f"/proc/self/fdinfo/{watcher._inotify}").read_text().splitlines()
    return sum(1 for line in lines if line.startswith("inotify wd:"))


def _names(payload, path):
    return {item["name"] for item in payload["changes"] if item["path"] == path}


# Access and request form


def test_changes_require_a_session_and_the_csrf_token(tmp_path) -> None:
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as anonymous:
        body = {"rootId": ROOT, "paths": [doc()], "epoch": None, "seq": None}
        response = anonymous.client.post("/api/changes", headers={"origin": anonymous.config.origin}, json=body)
        assert response.status_code == 401
        assert response.json() == {"error": "authentication_required"}
        login = anonymous.post_login(PASSWORD)
        assert login.status_code == 200
        for token in ("", "wrong-token"):
            refused = anonymous.client.post(
                "/api/changes",
                headers={"origin": anonymous.config.origin, "x-csrf-token": token},
                json=body,
            )
            assert refused.status_code == 403
            assert refused.json() == {"error": "forbidden"}
        assert anonymous.client.app.state.runtime.changes.watched() == []


def test_changes_reject_bodies_outside_the_contract(world) -> None:
    valid = {"rootId": ROOT, "paths": [doc()], "epoch": None, "seq": None}
    malformed = [
        [],
        {"rootId": ROOT, "paths": [doc()], "epoch": None},
        {**valid, "extra": 1},
        {**valid, "rootId": ""},
        {**valid, "rootId": 1},
        {**valid, "paths": doc()},
        {**valid, "paths": [doc(), 3]},
        {**valid, "paths": [doc()] * 257},
        {**valid, "epoch": 5},
        {**valid, "epoch": "x" * 129},
        {**valid, "seq": -1},
        {**valid, "seq": True},
        {**valid, "seq": "1"},
        {**valid, "seq": 1.5},
    ]
    for body in malformed:
        response = world.client.post("/api/changes", headers=world.headers, json=body)
        assert response.status_code == 422, body
        assert response.json() == {"error": "invalid_request"}
    broken = world.client.post("/api/changes", headers={**world.headers, "content-type": "application/json"}, content=b"{no")
    assert broken.status_code == 422
    large = world.client.post(
        "/api/changes",
        headers={**world.headers, "content-type": "application/json"},
        content=b'{"rootId":"fs","paths":["' + b"a" * (64 * 1024) + b'"],"epoch":null,"seq":null}',
    )
    assert large.status_code == 413
    assert large.json() == {"error": "limit_exceeded"}
    unknown = world.client.post("/api/changes", headers=world.headers, json={**valid, "rootId": "other"})
    assert unknown.status_code == 404
    repeated = world.client.post("/api/changes", headers=world.headers, json={**valid, "paths": [doc()] * 256})
    assert repeated.status_code == 200
    assert world.client.app.state.runtime.changes.watched() == [doc()]


def test_addresses_are_checked_like_a_listing(world) -> None:
    (DOCUMENTS / "real").mkdir()
    (DOCUMENTS / "real" / "inner").mkdir()
    (DOCUMENTS / "link").symlink_to("real", target_is_directory=True)
    (DOCUMENTS / "note.txt").write_bytes(b"text")
    (DOCUMENTS / "closed").mkdir(mode=0o300)
    try:
        requested = [
            doc(),
            doc("real/inner"),
            doc("link"),
            doc("link/inner"),
            doc("note.txt"),
            doc("missing"),
            doc("closed"),
            doc("real/../real"),
            doc("real//inner"),
            "/" + doc(),
        ]
        payload = _start(world, requested)
        assert payload["rejected"] == requested[2:]
        assert world.client.app.state.runtime.changes.watched() == sorted([doc(), doc("real/inner")])
        # Each listed directory gets its own kernel watch; nothing else is watched.
        assert _kernel_watches(world.client.app.state.runtime.changes) == 2
    finally:
        os.chmod(DOCUMENTS / "closed", 0o700)


# Notices


def test_create_delete_rename_modify_and_replace_are_reported_within_a_second(world, pool) -> None:
    (DOCUMENTS / "old.md").write_bytes(b"old")
    (DOCUMENTS / "gone.md").write_bytes(b"gone")
    (DOCUMENTS / "edited.md").write_bytes(b"one")
    (DOCUMENTS / "replaced.md").write_bytes(b"one")
    state = _start(world, [doc()])

    def replace_by_rename():
        (DOCUMENTS / ".replaced.md.tmp").write_bytes(b"two")
        os.replace(DOCUMENTS / ".replaced.md.tmp", DOCUMENTS / "replaced.md")

    steps = [
        (lambda: (DOCUMENTS / "new.md").write_bytes(b"new"), {"new.md"}),
        (lambda: (DOCUMENTS / "gone.md").unlink(), {"gone.md"}),
        (lambda: (DOCUMENTS / "old.md").rename(DOCUMENTS / "renamed.md"), {"old.md", "renamed.md"}),
        (lambda: (DOCUMENTS / "edited.md").write_bytes(b"two"), {"edited.md"}),
        (replace_by_rename, {"replaced.md"}),
        (lambda: (DOCUMENTS / "folder").mkdir(), {"folder"}),
    ]
    for change, expected in steps:
        future = _next(world, pool, [doc()], state)
        _settle()
        change()
        changed_at = time.monotonic()
        payload = _answer(future, changed_at)
        assert payload["resync"] is False
        assert payload["epoch"] == state["epoch"]
        assert payload["seq"] > state["seq"]
        assert expected <= _names(payload, doc()), (expected, payload)
        state = payload


def test_notices_already_waiting_are_answered_at_once_and_not_twice(world) -> None:
    state = _start(world, [doc()])
    (DOCUMENTS / "a.md").write_bytes(b"a")
    time.sleep(0.2)
    started = time.monotonic()
    first = _ask(world, [doc()], state["epoch"], state["seq"]).json()
    assert time.monotonic() - started < 1.0
    assert "a.md" in _names(first, doc())
    hopper_files.api.changes.HOLD_SECONDS = 0.5
    try:
        again = _ask(world, [doc()], first["epoch"], first["seq"]).json()
    finally:
        hopper_files.api.changes.HOLD_SECONDS = 20.0
    assert again["changes"] == []
    assert again["seq"] == first["seq"]


def test_only_named_directories_answer(world, pool, monkeypatch) -> None:
    monkeypatch.setattr(hopper_files.api.changes, "HOLD_SECONDS", 1.5)
    (DOCUMENTS / "shown").mkdir()
    (DOCUMENTS / "shown" / "deeper").mkdir()
    (DOCUMENTS / "other").mkdir()
    state = _start(world, [doc("shown")])
    future = _next(world, pool, [doc("shown")], state)
    _settle()
    (DOCUMENTS / "other" / "x.md").write_bytes(b"x")
    (DOCUMENTS / "shown" / "deeper" / "y.md").write_bytes(b"y")
    (DOCUMENTS / "top.md").write_bytes(b"z")
    response, _ = future.result(timeout=10)
    assert response.json()["changes"] == []


def test_a_request_without_news_is_held_then_answered_empty(world, pool, monkeypatch) -> None:
    assert hopper_files.api.changes.HOLD_SECONDS == 20.0
    assert hopper_files.api.changes.HOLD_SECONDS + hopper_files.api.changes.GATHER_SECONDS <= 25.0
    monkeypatch.setattr(hopper_files.api.changes, "HOLD_SECONDS", 1.0)
    state = _start(world, [doc()])
    started = time.monotonic()
    response = _ask(world, [doc()], state["epoch"], state["seq"])
    held = time.monotonic() - started
    assert 0.9 <= held < 2.0
    assert response.json() == {**state, "resync": False, "changes": [], "rejected": []}


def test_a_burst_is_answered_in_few_responses(world) -> None:
    state = _start(world, [doc()])
    responses = 0
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=1) as writer:
        def burst():
            for index in range(100):
                (DOCUMENTS / f"burst-{index:03}.md").write_bytes(b"x")
                time.sleep(0.005)
        done = writer.submit(burst)
        while not done.done() or responses == 0:
            payload = _ask(world, [doc()], state["epoch"], state["seq"]).json()
            responses += 1
            seen |= _names(payload, doc())
            if None in _names(payload, doc()):
                seen |= {f"burst-{index:03}.md" for index in range(100)}
            state = payload
        done.result()
    hopper_files.api.changes.HOLD_SECONDS = 0.5
    try:
        while len(seen) < 100:
            payload = _ask(world, [doc()], state["epoch"], state["seq"]).json()
            if not payload["changes"]:
                break
            seen |= _names(payload, doc())
            state = payload
    finally:
        hopper_files.api.changes.HOLD_SECONDS = 20.0
    assert {f"burst-{index:03}.md" for index in range(100)} <= seen
    assert responses <= 6


def test_many_names_in_one_directory_are_summarized(world) -> None:
    state = _start(world, [doc()])
    for index in range(80):
        (DOCUMENTS / f"many-{index:03}.md").write_bytes(b"x")
    time.sleep(0.2)
    payload = _ask(world, [doc()], state["epoch"], state["seq"]).json()
    assert payload["changes"] == [{"path": doc(), "name": None}]


def test_names_that_are_not_utf8_are_reported_without_a_name(world, pool) -> None:
    state = _start(world, [doc()])
    future = _next(world, pool, [doc()], state)
    _settle()
    with open(os.path.join(os.fsencode(DOCUMENTS), b"bad-\xff.md"), "wb") as handle:
        handle.write(b"x")
    payload = _answer(future, time.monotonic())
    assert payload["changes"] == [{"path": doc(), "name": None}]


def test_two_waiting_views_are_both_answered(world, pool) -> None:
    (DOCUMENTS / "left").mkdir()
    state = _start(world, [doc(), doc("left")])
    first = _next(world, pool, [doc()], state)
    second = _next(world, pool, [doc(), doc("left")], state)
    _settle()
    (DOCUMENTS / "left" / "note.md").write_bytes(b"x")
    (DOCUMENTS / "top.md").write_bytes(b"x")
    changed_at = time.monotonic()
    assert "top.md" in _names(_answer(first, changed_at), doc())
    both = _answer(second, changed_at)
    assert "note.md" in _names(both, doc("left"))
    assert "top.md" in _names(both, doc())


# Rereads


def test_restart_unknown_epoch_and_future_seq_ask_for_a_reread(world, tmp_path) -> None:
    state = _start(world, [doc()])
    for epoch, seq in ((state["epoch"], state["seq"] + 5), ("other-epoch", state["seq"]), (state["epoch"], None)):
        payload = _ask(world, [doc()], epoch, seq).json()
        assert payload["resync"] is True
        assert payload["epoch"] == state["epoch"]
        assert payload["seq"] == state["seq"]
    # Another service process starts with a new epoch.
    with boot(tmp_path / "restarted", ManualClock(CLOCK_START), password=PASSWORD) as restarted:
        login = restarted.post_login(PASSWORD)
        headers = {"origin": restarted.config.origin, "x-csrf-token": login.json()["csrfToken"]}
        body = {"rootId": ROOT, "paths": [doc()], "epoch": state["epoch"], "seq": state["seq"]}
        payload = restarted.client.post("/api/changes", headers=headers, json=body).json()
    assert payload["resync"] is True
    assert payload["epoch"] != state["epoch"]


def test_kernel_queue_overflow_asks_for_a_reread(world, monkeypatch) -> None:
    monkeypatch.setattr(hopper_files.api.changes, "HOLD_SECONDS", 0.5)
    state = _start(world, [doc()])
    watcher = world.client.app.state.runtime.changes
    # Only the kernel overflow, not the retained-notice limit, may cause the reread.
    watcher.notice_limit = 10**9
    limit = int(Path("/proc/sys/fs/inotify/max_queued_events").read_text())
    if limit > 100_000:
        pytest.skip("kernel queue too large to overflow quickly")
    # Holding the lock stops the reader, so the kernel queue fills and overflows.
    with watcher._lock:
        for index in range(limit + 10):
            os.mkdir(DOCUMENTS / f"d{index}")
    started = time.monotonic()
    payload = _ask(world, [doc()], state["epoch"], state["seq"]).json()
    assert time.monotonic() - started < 2.0
    assert payload["resync"] is True
    assert payload["changes"] == []
    assert payload["seq"] > state["seq"]
    followed = _ask(world, [doc()], payload["epoch"], payload["seq"])
    assert followed.status_code == 200


def test_retained_notices_overflow_asks_for_a_reread(world, monkeypatch) -> None:
    watcher = world.client.app.state.runtime.changes
    watcher.notice_limit = 5
    state = _start(world, [doc()])
    for index in range(10):
        (DOCUMENTS / f"n{index}").mkdir()
    time.sleep(0.2)
    payload = _ask(world, [doc()], state["epoch"], state["seq"]).json()
    assert payload["resync"] is True
    assert payload["changes"] == []


def test_a_waiting_request_is_told_to_reread_when_the_queue_overflows(world, pool) -> None:
    watcher = world.client.app.state.runtime.changes
    state = _start(world, [doc()])
    future = _next(world, pool, [doc()], state)
    _settle()
    with watcher._lock:
        watcher._overflowed()
    hopper_files.changes._notify(list(watcher._listeners))
    payload = _answer(future, time.monotonic())
    assert payload["resync"] is True


# Watch lifetime and limits


def test_directories_no_longer_named_are_released(world, monkeypatch) -> None:
    monkeypatch.setattr(hopper_files.api.changes, "HOLD_SECONDS", 0.2)
    watcher = world.client.app.state.runtime.changes
    watcher.release_seconds = 1.0
    (DOCUMENTS / "kept").mkdir()
    (DOCUMENTS / "left").mkdir()
    state = _start(world, [doc("kept"), doc("left")])
    assert _kernel_watches(watcher) == 2
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        state = _ask(world, [doc("kept")], state["epoch"], state["seq"]).json()
    assert watcher.watched() == [doc("kept")]
    assert _kernel_watches(watcher) == 1


def test_the_reader_releases_directories_without_requests(world, monkeypatch) -> None:
    monkeypatch.setattr(hopper_files.changes, "_SWEEP_SECONDS", 0.2)
    # A fresh watcher, so its reader starts with the short sweep interval.
    world.client.app.state.runtime.changes.close()
    replacement = ChangeWatcher()
    replacement.release_seconds = 0.5
    world.client.app.state.runtime.changes = replacement
    _start(world, [doc()])
    assert _kernel_watches(replacement) == 1
    time.sleep(1.5)
    assert replacement.watched() == []
    assert _kernel_watches(replacement) == 0


def test_the_watch_limit_rejects_further_directories(world) -> None:
    watcher = world.client.app.state.runtime.changes
    watcher.watch_limit = 2
    for name in ("a", "b", "c"):
        (DOCUMENTS / name).mkdir()
    payload = _start(world, [doc("a"), doc("b"), doc("c")])
    assert payload["rejected"] == [doc("c")]
    assert watcher.watched() == [doc("a"), doc("b")]
    assert _kernel_watches(watcher) == 2


def test_a_replaced_directory_is_watched_again(world, pool, monkeypatch) -> None:
    (DOCUMENTS / "box").mkdir()
    state = _start(world, [doc("box")])
    future = _next(world, pool, [doc("box")], state)
    _settle()
    (DOCUMENTS / "box").rename(DOCUMENTS / "box-old")
    (DOCUMENTS / "box").mkdir()
    payload = _answer(future, time.monotonic())
    assert _names(payload, doc("box")) == {None}
    future = _next(world, pool, [doc("box")], payload)
    _settle()
    (DOCUMENTS / "box" / "inside.md").write_bytes(b"x")
    later = _answer(future, time.monotonic())
    assert "inside.md" in _names(later, doc("box"))
    (DOCUMENTS / "box-old" / "stale.md").write_bytes(b"x")
    time.sleep(0.2)
    monkeypatch.setattr(hopper_files.api.changes, "HOLD_SECONDS", 0.5)
    stale = _ask(world, [doc("box")], later["epoch"], later["seq"])
    assert "stale.md" not in _names(stale.json(), doc("box"))


def test_a_listing_starts_the_watch_so_no_change_is_lost(world) -> None:
    state = _start(world, [doc()])
    (DOCUMENTS / "opened").mkdir()
    time.sleep(0.2)
    state = _ask(world, [doc()], state["epoch"], state["seq"]).json()
    listed = world.client.get("/api/list", params={"rootId": ROOT, "path": doc("opened")})
    assert listed.status_code == 200
    # The change happens after the listing and before the view names the folder.
    (DOCUMENTS / "opened" / "late.md").write_bytes(b"x")
    time.sleep(0.2)
    started = time.monotonic()
    payload = _ask(world, [doc(), doc("opened")], state["epoch"], state["seq"]).json()
    assert time.monotonic() - started < 1.0
    assert "late.md" in _names(payload, doc("opened"))


def test_opening_a_document_starts_the_watch_of_its_folder(world) -> None:
    (DOCUMENTS / "notes").mkdir()
    (DOCUMENTS / "notes" / "a.md").write_bytes(b"one")
    state = _start(world, [doc()])
    opened = world.client.get("/api/file", params={"rootId": ROOT, "path": doc("notes/a.md")})
    assert opened.status_code == 200, opened.text
    assert doc("notes") in world.client.app.state.runtime.changes.watched()
    (DOCUMENTS / "notes" / "a.md").write_bytes(b"two")
    time.sleep(0.2)
    payload = _ask(world, [doc(), doc("notes")], state["epoch"], state["seq"]).json()
    assert "a.md" in _names(payload, doc("notes"))


def test_listing_does_not_watch_until_changes_are_in_use(world) -> None:
    listed = world.client.get("/api/list", params={"rootId": ROOT, "path": doc()})
    assert listed.status_code == 200
    watcher = world.client.app.state.runtime.changes
    assert not watcher.active()
    assert watcher.watched() == []


def test_without_inotify_the_route_reports_no_support(world, monkeypatch) -> None:
    watcher = world.client.app.state.runtime.changes
    monkeypatch.setattr(watcher, "available", lambda: False)
    payload = _ask(world, [doc()]).json()
    assert payload["supported"] is False
    assert payload["changes"] == []
