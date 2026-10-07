"""Synthetic UI-state schema, CAS, durability, and recovery checks."""

from __future__ import annotations

import json
import copy
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

import hopper_files.state as state
from hopper_files.state import (
    StateError,
    UiStateConflict,
    UiStatePublicationIndeterminate,
    UiStateValidationError,
    default_ui_state,
    ensure_initialized,
    initialize_state,
    load_ui_state,
    mutate_ui_state,
    recover_ui_state,
    replace_ui_state,
    ui_state_path,
)

from conftest import CLOCK_START, PASSWORD, ManualClock, boot, D, ROOT, doc


def _proposal(document: dict[str, object]) -> dict[str, object]:
    return copy.deepcopy({key: value for key, value in document.items() if key != "stateRevision"})


def _concurrent_replace(
    state_directory: str,
    started: object,
    release: object,
    results: object,
    theme: str,
) -> None:
    started.put(os.getpid())
    release.wait(5)
    document = _proposal(default_ui_state())
    document["preferences"]["theme"] = theme
    try:
        committed = replace_ui_state(
            Path(state_directory),
            base_revision=0,
            document=document,
        )
    except UiStateConflict as exc:
        results.put(("conflict", exc.state_revision))
    else:
        results.put(("committed", committed["stateRevision"]))


def _exit_before_journal_intent_is_published(state_directory: str) -> None:
    """Simulate a real process death while its journal temporary is written."""
    real_write = state.os.write

    def terminate(file_descriptor: int, data: object) -> int:
        del file_descriptor, data
        os._exit(77)

    state.os.write = terminate
    document = _proposal(load_ui_state(Path(state_directory)))
    document["preferences"]["theme"] = "dark"
    replace_ui_state(Path(state_directory), base_revision=0, document=document)
    state.os.write = real_write


def _exit_before_initialization_intent_is_published(state_directory: str) -> None:
    """Leave only the private initialization-intent temporary from a real death."""
    real_atomic_write = state.atomic_write

    def terminate_intent_write(target: Path, data: bytes) -> None:
        if target.name == state.UI_STATE_INITIALIZATION_INTENT_NAME:
            def terminate(file_descriptor: int, chunk: object) -> int:
                del file_descriptor, chunk
                os._exit(79)

            state.os.write = terminate
        real_atomic_write(target, data)

    state.atomic_write = terminate_intent_write
    initialize_state(Path(state_directory), "alpha")


def _item(*, path: str = "/note.md", label_ids: list[str] | None = None, emoji: str | None = None) -> dict[str, object]:
    return {
        "path": path,
        "labelIds": ["azul"] if label_ids is None else label_ids,
        "favorite": True,
        "emoji": emoji,
        "inode": 12,
        "device": 34,
    }


def test_initial_state_has_exact_schema_labels_and_private_modes(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")

    document = load_ui_state(directory)
    path = ui_state_path(directory)

    assert document == default_ui_state()
    assert path.name == "ui-state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "journals" / "ui-state").stat().st_mode) == 0o700
    assert set(document["labels"]) == {
        "vermelho",
        "laranja",
        "amarelo",
        "verde",
        "azul",
        "roxo",
        "cinza",
    }
    assert document["version"] == 2
    assert document["orphans"] == []
    assert all(set(tab) == {"path", "mode"} for tab in document["tabs"])


def test_missing_committed_document_fails_closed_without_revision_zero_reset(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    document = _proposal(load_ui_state(directory))
    document["preferences"]["theme"] = "dark"
    assert replace_ui_state(directory, base_revision=0, document=document)["stateRevision"] == 1
    path = ui_state_path(directory)
    initialization_marker = directory / state.UI_STATE_INITIALIZATION_NAME
    assert initialization_marker.is_file()
    path.unlink()

    with pytest.raises(StateError):
        ensure_initialized(directory, "alpha")
    with pytest.raises(StateError):
        load_ui_state(directory)

    assert initialization_marker.is_file()
    assert not path.exists()


def test_failed_first_document_write_recovers_only_an_unpublished_initialization(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "state"
    path = ui_state_path(directory)
    real_atomic_write = state.atomic_write

    def fail_initial_document(target: Path, data: bytes) -> None:
        if target == path:
            raise OSError("synthetic initial document failure")
        real_atomic_write(target, data)

    monkeypatch.setattr(state, "atomic_write", fail_initial_document)
    with pytest.raises(OSError):
        initialize_state(directory, "alpha")
    monkeypatch.undo()

    intent = directory / state.UI_STATE_INITIALIZATION_INTENT_NAME
    marker = directory / state.UI_STATE_INITIALIZATION_NAME
    assert intent.is_file()
    assert not marker.exists()
    assert not path.exists()

    initialize_state(directory, "alpha")

    assert load_ui_state(directory) == default_ui_state()
    assert marker.is_file()
    assert not intent.exists()


def test_repeated_real_deaths_before_initialization_intent_publication_leave_no_temporary(tmp_path) -> None:
    directory = tmp_path / "state"
    context = multiprocessing.get_context("fork")
    for _attempt in range(2):
        process = context.Process(target=_exit_before_initialization_intent_is_published, args=(str(directory),))
        process.start()
        process.join(5)
        assert process.exitcode == 79

    temporaries = [
        entry
        for entry in directory.iterdir()
        if state._UI_STATE_INITIALIZATION_TEMPORARY_NAMES[1].fullmatch(entry.name)
    ]
    # The second retry first removes the first process's temporary, then dies
    # while creating its own. A successful third attempt must remove that one.
    assert len(temporaries) == 1

    initialize_state(directory, "alpha")

    assert not [
        entry
        for entry in directory.iterdir()
        if state._UI_STATE_INITIALIZATION_TEMPORARY_NAMES[1].fullmatch(entry.name)
    ]
    assert load_ui_state(directory) == default_ui_state()


def test_unknown_or_linked_initialization_temporary_fails_closed(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    before = ui_state_path(directory).read_bytes()
    unknown = directory / ".ui-state-initializing.json.not-a-generated-temporary.tmp"
    unknown.write_bytes(b"synthetic")

    with pytest.raises(StateError):
        ensure_initialized(directory, "alpha")

    assert unknown.read_bytes() == b"synthetic"
    assert ui_state_path(directory).read_bytes() == before
    unknown.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"synthetic outside")
    linked = directory / (".ui-state-initializing.json." + "a" * 16 + ".tmp")
    linked.symlink_to(outside)

    with pytest.raises(StateError):
        ensure_initialized(directory, "alpha")

    assert linked.is_symlink()
    assert outside.read_bytes() == b"synthetic outside"
    assert ui_state_path(directory).read_bytes() == before


def test_failed_initialization_marker_finishes_only_the_exact_durable_default(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "state"
    path = ui_state_path(directory)
    marker = directory / state.UI_STATE_INITIALIZATION_NAME
    real_atomic_write = state.atomic_write

    def fail_initialization_marker(target: Path, data: bytes) -> None:
        if target == marker:
            raise OSError("synthetic initialization marker failure")
        real_atomic_write(target, data)

    monkeypatch.setattr(state, "atomic_write", fail_initialization_marker)
    with pytest.raises(OSError):
        initialize_state(directory, "alpha")
    monkeypatch.undo()

    before = path.read_bytes()
    assert (directory / state.UI_STATE_INITIALIZATION_INTENT_NAME).is_file()
    assert not marker.exists()

    ensure_initialized(directory, "alpha")

    assert path.read_bytes() == before
    assert load_ui_state(directory) == default_ui_state()
    assert marker.is_file()
    assert not (directory / state.UI_STATE_INITIALIZATION_INTENT_NAME).exists()


def test_initialization_intent_refuses_a_different_document_without_resetting(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "state"
    marker = directory / state.UI_STATE_INITIALIZATION_NAME
    path = ui_state_path(directory)
    real_atomic_write = state.atomic_write

    def fail_initialization_marker(target: Path, data: bytes) -> None:
        if target == marker:
            raise OSError("synthetic initialization marker failure")
        real_atomic_write(target, data)

    monkeypatch.setattr(state, "atomic_write", fail_initialization_marker)
    with pytest.raises(OSError):
        initialize_state(directory, "alpha")
    monkeypatch.undo()

    different = default_ui_state()
    different["preferences"]["theme"] = "dark"
    path.write_bytes(state._encode_ui_state(different))
    before = path.read_bytes()

    with pytest.raises(StateError):
        ensure_initialized(directory, "alpha")

    assert path.read_bytes() == before
    assert not marker.exists()
    assert (directory / state.UI_STATE_INITIALIZATION_INTENT_NAME).is_file()


def test_state_validation_rejects_bad_addresses_ids_hints_and_tabs_without_replacing(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    before = ui_state_path(directory).read_bytes()
    document = _proposal(load_ui_state(directory))
    document["items"] = [
        {
            "path": "/" + doc("notes/../secret"),
            "labelIds": ["vermelho", "vermelho"],
            "favorite": True,
            "emoji": "📌",
            "inode": 1,
            "device": 1,
        }
    ]
    document["tabs"] = [{"path": "/" + doc("note.md"), "mode": "edit", "content": "secret"}]

    with pytest.raises(UiStateValidationError):
        replace_ui_state(directory, base_revision=0, document=document)

    assert ui_state_path(directory).read_bytes() == before
    assert load_ui_state(directory)["stateRevision"] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document["labels"]["verde"].__setitem__("name", "\ud800"),
        lambda document: document.__setitem__("items", [_item(path="/\ud800")]),
        lambda document: document.__setitem__("items", [_item(path="\ud800")]),
        lambda document: document.__setitem__("items", [_item(label_ids=["\ud800"])]),
        lambda document: document.__setitem__("items", [_item(emoji="\ud800")]),
        lambda document: document.__setitem__("tabs", [{"path": "/notas/\ud800", "mode": "browse"}]),
        lambda document: document.__setitem__("tabs", [{"path": "/" + doc("\ud800"), "mode": "browse"}]),
        lambda document: document.__setitem__("tabs", [{"path": "/" + D, "mode": "\ud800"}]),
        lambda document: document["preferences"].__setitem__("theme", "\ud800"),
        lambda document: document["preferences"].__setitem__("ordering", "\ud800"),
        lambda document: document["preferences"].__setitem__("density", "\ud800"),
    ],
)
def test_surrogates_in_text_fields_are_validation_errors_before_publication(tmp_path, mutate) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    before = ui_state_path(directory).read_bytes()
    document = _proposal(load_ui_state(directory))
    mutate(document)

    with pytest.raises(UiStateValidationError):
        replace_ui_state(directory, base_revision=0, document=document)

    assert ui_state_path(directory).read_bytes() == before
    assert load_ui_state(directory)["stateRevision"] == 0


def test_valid_unicode_text_is_preserved(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    document = _proposal(load_ui_state(directory))
    document["labels"]["verde"]["name"] = "Verde café"
    document["items"] = [_item(path="/notas/árvore.md", emoji="🪴")]
    document["tabs"] = [{"path": "/" + doc("notas/árvore.md"), "mode": "prévia"}]
    document["preferences"]["theme"] = "noite 🌙"

    committed = replace_ui_state(directory, base_revision=0, document=document)

    assert committed["stateRevision"] == 1
    assert committed["items"][0]["emoji"] == "🪴"


def test_ignored_tags_are_an_optional_normalized_preference(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    document = _proposal(load_ui_state(directory))
    assert "ignoredTags" not in document["preferences"]
    document["preferences"]["ignoredTags"] = ["tag", "ação", "rascunho-2"]

    committed = replace_ui_state(directory, base_revision=0, document=document)
    assert committed["preferences"]["ignoredTags"] == ["tag", "ação", "rascunho-2"]

    restored = _proposal(committed)
    del restored["preferences"]["ignoredTags"]
    assert "ignoredTags" not in replace_ui_state(directory, base_revision=1, document=restored)["preferences"]


def test_sidebar_width_is_an_optional_bounded_preference(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    document = _proposal(load_ui_state(directory))
    document["preferences"]["sidebarWidth"] = 320
    assert replace_ui_state(directory, base_revision=0, document=document)["preferences"]["sidebarWidth"] == 320

    for invalid in (199, 421, 320.5, True, "320"):
        rejected = _proposal(load_ui_state(directory))
        rejected["preferences"]["sidebarWidth"] = invalid
        with pytest.raises(UiStateValidationError):
            replace_ui_state(directory, base_revision=1, document=rejected)


@pytest.mark.parametrize(
    "value",
    [[], ["Tag"], ["#tag"], ["tag", "tag"], ["-tag"], ["tag-"], ["1tag"], ["com espaço"], "tag", [None]],
)
def test_ignored_tags_reject_non_normalized_or_empty_values(tmp_path, value) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    before = ui_state_path(directory).read_bytes()
    document = _proposal(load_ui_state(directory))
    document["preferences"]["ignoredTags"] = value

    with pytest.raises(UiStateValidationError):
        replace_ui_state(directory, base_revision=0, document=document)

    assert ui_state_path(directory).read_bytes() == before


def test_cas_uses_server_revision_and_preserves_both_versions_on_conflict(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    first = _proposal(load_ui_state(directory))
    first["preferences"]["theme"] = "dark"
    committed = replace_ui_state(directory, base_revision=0, document=first)
    stale = _proposal(default_ui_state())
    stale["preferences"]["density"] = "compact"

    with pytest.raises(UiStateConflict) as conflict:
        replace_ui_state(directory, base_revision=0, document=stale)

    assert committed["stateRevision"] == 1
    assert conflict.value.state_revision == 1
    current = load_ui_state(directory)
    assert current["preferences"] == {"theme": "dark", "ordering": "name", "density": "comfortable"}
    assert stale["preferences"]["density"] == "compact"
    assert list((directory / "journals" / "ui-state").iterdir()) == []


def test_real_processes_recheck_base_revision_under_one_lock(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    context = multiprocessing.get_context("fork")
    started = context.Queue()
    results = context.Queue()
    release = context.Event()
    processes = [
        context.Process(
            target=_concurrent_replace,
            args=(str(directory), started, release, results, theme),
        )
        for theme in ("dark", "light")
    ]
    for process in processes:
        process.start()
    assert {started.get(timeout=5), started.get(timeout=5)}
    release.set()
    outcomes = sorted((results.get(timeout=5), results.get(timeout=5)))
    for process in processes:
        process.join(5)
        assert process.exitcode == 0

    assert outcomes == [("committed", 1), ("conflict", 1)]
    document = load_ui_state(directory)
    assert document["stateRevision"] == 1
    assert document["preferences"]["theme"] in {"dark", "light"}


def test_failed_prepublication_leaves_old_document_and_recovery_is_idempotent(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    before = ui_state_path(directory).read_bytes()
    document = _proposal(load_ui_state(directory))
    document["preferences"]["ordering"] = "modified"
    real_rename = state.os.rename

    def fail_target(source: object, target: object, *args: object, **kwargs: object) -> None:
        if target == "ui-state.json":
            raise OSError("synthetic target publication failure")
        real_rename(source, target, *args, **kwargs)

    monkeypatch.setattr(state.os, "rename", fail_target)
    with pytest.raises(OSError):
        replace_ui_state(directory, base_revision=0, document=document)
    monkeypatch.undo()

    assert ui_state_path(directory).read_bytes() == before
    assert len(list((directory / "journals" / "ui-state").iterdir())) == 1
    recover_ui_state(directory)
    recover_ui_state(directory)
    assert ui_state_path(directory).read_bytes() == before
    assert list((directory / "journals" / "ui-state").iterdir()) == []


def test_failure_after_atomic_replace_recovers_the_complete_new_document(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    document = _proposal(load_ui_state(directory))
    document["preferences"]["density"] = "compact"
    target_parent = ui_state_path(directory).parent.stat()
    renamed_target = False
    real_rename = state.os.rename
    real_fsync = state.os.fsync

    def record_rename(source: object, target: object, *args: object, **kwargs: object) -> None:
        nonlocal renamed_target
        real_rename(source, target, *args, **kwargs)
        if target == "ui-state.json":
            renamed_target = True

    def fail_directory_sync(file_descriptor: int) -> None:
        descriptor = os.fstat(file_descriptor)
        if renamed_target and stat.S_ISDIR(descriptor.st_mode) and (descriptor.st_dev, descriptor.st_ino) == (target_parent.st_dev, target_parent.st_ino):
            raise OSError("synthetic directory sync failure")
        real_fsync(file_descriptor)

    monkeypatch.setattr(state.os, "rename", record_rename)
    monkeypatch.setattr(state.os, "fsync", fail_directory_sync)
    with pytest.raises(UiStatePublicationIndeterminate) as outcome:
        replace_ui_state(directory, base_revision=0, document=document)
    monkeypatch.undo()

    assert outcome.value.state_revision == 1
    assert len(list((directory / "journals" / "ui-state").iterdir())) == 1
    recover_ui_state(directory)
    recovered = load_ui_state(directory)
    assert recovered["stateRevision"] == 1
    assert recovered["preferences"]["density"] == "compact"
    assert list((directory / "journals" / "ui-state").iterdir()) == []


def test_http_state_reports_indeterminate_when_rename_precedes_directory_sync_failure(
    tmp_path, monkeypatch
) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = world.post_login(PASSWORD).json()["csrfToken"]
        initial = world.client.get("/api/state").json()
        proposal = _proposal(initial)
        proposal["preferences"]["density"] = "compact"
        target_parent = ui_state_path(world.config.state_directory).parent.stat()
        renamed_target = False
        real_rename = state.os.rename
        real_fsync = state.os.fsync

        def record_rename(source: object, target: object, *args: object, **kwargs: object) -> None:
            nonlocal renamed_target
            real_rename(source, target, *args, **kwargs)
            if target == "ui-state.json":
                renamed_target = True

        def fail_directory_sync(file_descriptor: int) -> None:
            descriptor = os.fstat(file_descriptor)
            if (
                renamed_target
                and stat.S_ISDIR(descriptor.st_mode)
                and (descriptor.st_dev, descriptor.st_ino) == (target_parent.st_dev, target_parent.st_ino)
            ):
                raise OSError("synthetic directory sync failure after state publication")
            real_fsync(file_descriptor)

        monkeypatch.setattr(state.os, "rename", record_rename)
        monkeypatch.setattr(state.os, "fsync", fail_directory_sync)
        response = None
        try:
            response = world.client.put(
                "/api/state",
                content=json.dumps({"baseRevision": 0, **proposal}, separators=(",", ":")).encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "origin": world.config.origin,
                    "x-csrf-token": csrf,
                },
            )
        except OSError:
            # If the failure escaped the route, the assertions below report it.
            pass
        monkeypatch.undo()
        after = world.client.get("/api/state")

    assert renamed_target
    assert response is not None
    assert response.status_code == 503
    assert response.json() == {"error": "indeterminate", "stateRevision": 1}
    assert after.status_code == 200
    assert after.json()["stateRevision"] == 1
    assert after.json()["preferences"]["density"] == "compact"


def test_http_state_reports_storage_unavailable_before_state_document_replace(
    tmp_path, monkeypatch
) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = world.post_login(PASSWORD).json()["csrfToken"]
        before = world.client.get("/api/state").json()
        before_bytes = ui_state_path(world.config.state_directory).read_bytes()
        proposal = _proposal(before)
        proposal["preferences"]["density"] = "compact"
        real_atomic_write = state.atomic_write

        def fail_before_state_replace(path, content):
            if path.name == "ui-state.json":
                raise OSError("synthetic storage failure before state replace")
            return real_atomic_write(path, content)

        monkeypatch.setattr(state, "atomic_write", fail_before_state_replace)
        response = world.client.put(
            "/api/state",
            content=json.dumps({"baseRevision": 0, **proposal}, separators=(",", ":")).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "origin": world.config.origin,
                "x-csrf-token": csrf,
            },
        )
        monkeypatch.setattr(state, "atomic_write", real_atomic_write)
        after = world.client.get("/api/state").json()
        after_bytes = ui_state_path(world.config.state_directory).read_bytes()

    assert response.status_code == 503
    assert response.json() == {"error": "storage_unavailable"}
    assert after == before
    assert after_bytes == before_bytes


def test_invalid_or_indeterminate_recovery_never_replaces_current_state(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    before = ui_state_path(directory).read_bytes()
    journal = directory / "journals" / "ui-state" / "invalid.json"
    journal.write_text("{}", encoding="utf-8")

    with pytest.raises(StateError):
        recover_ui_state(directory)
    with pytest.raises(StateError):
        recover_ui_state(directory)

    assert ui_state_path(directory).read_bytes() == before
    assert journal.exists()


def test_real_process_death_before_journal_publication_cleans_only_its_temporary(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    before = ui_state_path(directory).read_bytes()
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_exit_before_journal_intent_is_published, args=(str(directory),))

    process.start()
    process.join(5)

    journal_directory = directory / "journals" / "ui-state"
    temporaries = list(journal_directory.iterdir())
    assert process.exitcode == 77
    assert len(temporaries) == 1
    assert state._UI_STATE_JOURNAL_TEMPORARY_NAME.fullmatch(temporaries[0].name)

    recover_ui_state(directory)
    recover_ui_state(directory)

    assert ui_state_path(directory).read_bytes() == before
    assert list(journal_directory.iterdir()) == []


def test_unknown_journal_temporary_remains_an_error_for_diagnosis(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    before = ui_state_path(directory).read_bytes()
    temporary = directory / "journals" / "ui-state" / "unknown.tmp"
    temporary.write_bytes(b"not a hopper files temporary")

    with pytest.raises(StateError):
        recover_ui_state(directory)
    with pytest.raises(StateError):
        recover_ui_state(directory)

    assert ui_state_path(directory).read_bytes() == before
    assert temporary.exists()


def test_internal_mutations_share_the_revision_protocol(tmp_path) -> None:
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")

    def add_item(document: dict[str, object]) -> None:
        document["items"].append(
            {
                "path": "/" + doc("note.md"),
                "labelIds": ["azul"],
                "favorite": True,
                "emoji": None,
                "inode": 12,
                "device": 34,
            }
        )

    committed = mutate_ui_state(directory, add_item)
    assert committed["stateRevision"] == 1
    assert committed["items"][0]["path"] == "/" + doc("note.md")
    assert mutate_ui_state(directory, lambda document: None)["stateRevision"] == 1


def test_http_state_requires_session_csrf_precondition_and_absolute_paths(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        denied = world.client.get("/api/state")
        signed = world.post_login(PASSWORD)
        csrf = signed.json()["csrfToken"]
        initial = world.client.get("/api/state")
        missing = world.client.put(
            "/api/state",
            json={},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        without_csrf = world.client.put(
            "/api/state",
            json={"baseRevision": 0, **_proposal(initial.json())},
            headers={"origin": world.config.origin},
        )
        update = _proposal(initial.json())
        update["items"] = [
            {
                "path": "/" + doc("notes/today.md"),
                "labelIds": ["verde"],
                "favorite": True,
                "emoji": "📌",
                "inode": 123,
                "device": 456,
            }
        ]
        committed = world.client.put(
            "/api/state",
            json={"baseRevision": 0, **update},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        stale = world.client.put(
            "/api/state",
            json={"baseRevision": 0, **update},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        unknown = _proposal(committed.json())
        unknown["items"][0]["path"] = doc("notes/today.md")
        invalid = world.client.put(
            "/api/state",
            json={"baseRevision": 1, **unknown},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )

    assert denied.status_code == 401
    assert initial.status_code == 200
    assert initial.json()["stateRevision"] == 0
    assert missing.status_code == 428
    assert without_csrf.status_code == 403
    assert committed.status_code == 200
    assert committed.json()["stateRevision"] == 1
    assert stale.status_code == 409
    assert stale.json() == {"error": "conflict", "stateRevision": 1}
    assert invalid.status_code == 422


def test_http_state_rejects_isolated_surrogates_with_422_without_writing(tmp_path) -> None:
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        csrf = world.post_login(PASSWORD).json()["csrfToken"]
        initial = world.client.get("/api/state")
        invalid = _proposal(initial.json())
        invalid["labels"]["azul"]["name"] = "\ud800"
        response = world.client.put(
            "/api/state",
            content=json.dumps({"baseRevision": 0, **invalid}, ensure_ascii=True).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "origin": world.config.origin,
                "x-csrf-token": csrf,
            },
        )
        after = world.client.get("/api/state")

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_state"}
    assert after.json() == initial.json()


def test_http_state_isolated_between_instances(tmp_path) -> None:
    with boot(tmp_path / "alpha", ManualClock(CLOCK_START), password=PASSWORD, instance_id="alpha", port=8831) as alpha:
        with boot(tmp_path / "beta", ManualClock(CLOCK_START), password=PASSWORD, instance_id="beta", port=8832) as beta:
            csrf = alpha.post_login(PASSWORD).json()["csrfToken"]
            update = _proposal(alpha.client.get("/api/state").json())
            update["preferences"]["theme"] = "dark"
            changed = alpha.client.put(
                "/api/state",
                json={"baseRevision": 0, **update},
                headers={"origin": alpha.config.origin, "x-csrf-token": csrf},
            )
            untouched = beta.client.get("/api/state")

    assert changed.status_code == 200
    assert changed.json()["stateRevision"] == 1
    assert untouched.status_code == 401
    assert load_ui_state(beta.config.state_directory)["stateRevision"] == 0


def test_orphans_are_server_owned_and_kept_by_client_writes(tmp_path) -> None:
    """HF-META-004: orphaned version 1 records are kept only for reporting and rollback."""
    with boot(tmp_path, ManualClock(CLOCK_START), password=PASSWORD) as world:
        orphan = {
            "kind": "item",
            "record": {"rootId": "retired", "path": "old.md", "labelIds": ["cinza"], "favorite": False, "emoji": None, "inode": None, "device": None},
        }
        mutate_ui_state(world.config.state_directory, lambda document: document["orphans"].append(orphan))
        csrf = world.post_login(PASSWORD).json()["csrfToken"]
        current = world.client.get("/api/state").json()
        without_orphans = {key: value for key, value in _proposal(current).items() if key != "orphans"}
        without_orphans["preferences"]["theme"] = "dark"
        kept = world.client.put(
            "/api/state",
            json={"baseRevision": current["stateRevision"], **without_orphans},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        changed = _proposal(kept.json())
        changed["orphans"] = []
        refused = world.client.put(
            "/api/state",
            json={"baseRevision": kept.json()["stateRevision"], **changed},
            headers={"origin": world.config.origin, "x-csrf-token": csrf},
        )
        after = world.client.get("/api/state").json()

    assert kept.status_code == 200
    assert kept.json()["orphans"] == [orphan]
    assert refused.status_code == 422
    assert after["orphans"] == [orphan]
    assert after["preferences"]["theme"] == "dark"


def test_version_1_ui_state_fails_closed_without_initializing_over_it(tmp_path) -> None:
    """HF-META-004/HF-NAV-012: this release never serves or resets version 1 state."""
    directory = tmp_path / "state"
    initialize_state(directory, "alpha")
    path = ui_state_path(directory)
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy.pop("orphans")
    legacy["version"] = 1
    legacy["items"] = [{"rootId": "documents", "path": "a.md", "labelIds": [], "favorite": True, "emoji": None, "inode": None, "device": None}]
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(state.LegacyStateError):
        load_ui_state(directory)
    with pytest.raises(state.LegacyStateError):
        ensure_initialized(directory, "alpha")

    assert path.read_bytes() == before
