from __future__ import annotations

import json
import os
import threading
import time

from conftest import CLOCK_START, D, DOCUMENTS, PASSWORD, ROOT, ManualClock, address, boot, doc


def _signed(world) -> dict[str, str]:
    response = world.post_login(PASSWORD)
    assert response.status_code == 200, response.text
    return {"origin": world.config.origin, "x-csrf-token": response.json()["csrfToken"]}


def _search(world, query: str, scope: str | None = None, **params):
    return world.client.get(
        world.config.base_path + "api/search",
        params={"q": query, "scope": "/" + D if scope is None else scope, **params},
    )


def _names(response) -> list[str]:
    prefix = D + "/"
    return [item["path"][len(prefix):] for item in response.json()["items"]]


def _categories(response, key: str) -> dict[str, int]:
    return {item["category"]: item["count"] for item in response.json()[key]}


def _monitor(world, headers: dict[str, str], relative: str = "", monitored: bool = True):
    """Mark or unmark a folder of the test documents directory for the tag index."""
    response = world.client.post(
        world.config.base_path + "api/tag-folders",
        json={"path": "/" + doc(relative), "monitored": monitored},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response


def _tag_counts(response) -> dict[str, int]:
    return {item["tag"]: item["count"] for item in response.json()["tags"]}


def test_search_is_authenticated_literal_normalized_and_scoped(tmp_path) -> None:
    (DOCUMENTS / "ÁRVORE[1].md").write_text("conteúdo", encoding="utf-8")
    (DOCUMENTS / "arvore[1].md").write_text("sem acento", encoding="utf-8")
    (DOCUMENTS / "regex.*.md").write_text("literal", encoding="utf-8")
    (DOCUMENTS / "folder").mkdir()
    (DOCUMENTS / "folder" / "inside.md").write_text("ok", encoding="utf-8")
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        denied = _search(world, "árvore", mode="name")
        _signed(world)
        equivalent = _search(world, "Árvore", mode="name")
        literal = _search(world, ".*", mode="name")
        glob = _search(world, "arv?re", mode="name")
        narrowed = _search(world, "inside", mode="name", scope="/" + doc("folder"))
        missing_scope = _search(world, "x", mode="name", scope="/" + doc("missing"))
        file_scope = _search(world, "x", mode="name", scope="/" + doc("regex.*.md"))
        relative_scope = _search(world, "x", mode="name", scope=doc("folder"))
        no_scope = world.client.get(world.config.base_path + "api/search", params={"q": "x"})

    assert denied.status_code == 401
    assert _names(equivalent) == ["ÁRVORE[1].md"]
    assert _names(literal) == ["regex.*.md"]
    assert _names(glob) == []
    assert _names(narrowed) == ["folder/inside.md"]
    assert equivalent.json()["items"][0]["address"] == "/" + doc("ÁRVORE[1].md")
    assert missing_scope.status_code == file_scope.status_code == 404
    assert relative_scope.status_code == no_scope.status_code == 400


def test_name_search_recurses_keeps_dotfiles_and_returns_links_without_following(tmp_path) -> None:
    nested = DOCUMENTS / "Café-folder"
    nested.mkdir()
    (nested / "Café-note.md").write_text("nested", encoding="utf-8")
    (DOCUMENTS / ".Café-user-note.md").write_text("dotfile", encoding="utf-8")
    target = DOCUMENTS / "target-dir"
    target.mkdir()
    (target / "café-inside.md").write_text("inside", encoding="utf-8")
    (DOCUMENTS / "café-link").symlink_to(target, target_is_directory=True)
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        _signed(world)
        matched = _search(world, "Café", mode="name")
        accent_sensitive = _search(world, "cafe", mode="name")

    assert matched.status_code == 200
    assert [(name, item["type"]) for name, item in zip(_names(matched), matched.json()["items"])] == [
        (".Café-user-note.md", "file"),
        ("Café-folder", "directory"),
        ("Café-folder/Café-note.md", "file"),
        ("café-link", "link"),
        ("target-dir/café-inside.md", "file"),
    ]
    assert _names(matched) == sorted(_names(matched), key=lambda name: name.encode("utf-8"))
    assert _names(accent_sensitive) == []


def test_text_search_lowest_line_and_categorized_exclusions(tmp_path) -> None:
    (DOCUMENTS / "match.md").write_text("primeira linha\nárvore no começo\nárvore depois", encoding="utf-8")
    (DOCUMENTS / "split.md").write_text("arb\nore", encoding="utf-8")
    (DOCUMENTS / "nul.txt").write_bytes(b"alpha\x00needle")
    (DOCUMENTS / "invalid.txt").write_bytes(b"\xffneedle")
    (DOCUMENTS / "large.txt").write_bytes(b"needle" + b" " * (5 * 1024 * 1024))
    (DOCUMENTS / "exact.txt").write_bytes(b"needle" + b" " * (5 * 1024 * 1024 - len(b"needle")))
    (DOCUMENTS / "linked.txt").write_text("needle in a hard link", encoding="utf-8")
    os.link(DOCUMENTS / "linked.txt", DOCUMENTS / "second-name.txt")
    (DOCUMENTS / "private").mkdir()
    (DOCUMENTS / "private" / "needle.txt").write_text("needle", encoding="utf-8")
    (DOCUMENTS / "shortcut.txt").symlink_to(DOCUMENTS / "match.md")
    os.mkfifo(DOCUMENTS / "stream")
    os.chmod(DOCUMENTS / "private", 0)
    try:
        with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
            _signed(world)
            response = _search(world, "árvore", mode="text")
            newline = _search(world, "arb\nore", mode="text")
            needles = _search(world, "needle", mode="text")
            short = _search(world, "x", mode="text")
    finally:
        os.chmod(DOCUMENTS / "private", 0o700)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert _names(response) == ["match.md"]
    assert (item["line"], item["snippet"]) == (2, "árvore no começo")
    assert _names(newline) == []
    assert _names(needles) == ["exact.txt", "linked.txt", "second-name.txt"], needles.json()
    exclusions = _categories(needles, "exclusions")
    assert exclusions["nul"] == 1
    assert exclusions["invalid_utf8"] == 1
    assert exclusions["too_large"] == 1
    assert exclusions["symbolic_link"] == 1
    assert exclusions["special_file"] == 1
    assert exclusions["unreadable_directory"] == 1
    # An unlistable directory is counted but does not make the result incomplete.
    assert needles.json()["complete"] is True
    assert short.status_code == 400


def test_search_cursor_pages_deterministically_binds_inputs_and_detects_mutation(tmp_path) -> None:
    for index in range(405):
        (DOCUMENTS / f"entry-{index:03}.txt").write_text("first", encoding="utf-8")
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        _signed(world)
        first = _search(world, "entry-", mode="name", limit="100")
        cursor = first.json()["nextCursor"]
        pages = [first]
        while pages[-1].json()["nextCursor"]:
            pages.append(_search(world, "entry-", mode="name", limit="100", cursor=pages[-1].json()["nextCursor"]))
        wide = [_search(world, "entry-", mode="name", limit="200")]
        while wide[-1].json()["nextCursor"]:
            wide.append(_search(world, "entry-", mode="name", limit="200", cursor=wide[-1].json()["nextCursor"]))
        changed = _search(world, "entry-", mode="name", limit="99", cursor=cursor)
        other_scope = _search(world, "entry-", scope="/" + D.rsplit("/", 1)[0], mode="name", limit="100", cursor=cursor)
        tampered = _search(world, "entry-", mode="name", limit="100", cursor=cursor[:-1] + ("A" if cursor[-1] != "A" else "B"))
        duplicate_mode = world.client.get(
            world.config.base_path + "api/search",
            params=[("q", "entry"), ("mode", "name"), ("mode", "text"), ("scope", "/" + D)],
        )
        (DOCUMENTS / "entry-150.txt").write_text("changed", encoding="utf-8")
        conflict = _search(world, "entry-", mode="name", limit="100", cursor=cursor)

    assert [len(page.json()["items"]) for page in pages] == [100, 100, 100, 100, 5]
    names = [name for page in pages for name in _names(page)]
    assert len(names) == len(set(names)) == 405
    assert names == sorted(names, key=lambda name: name.encode("utf-8"))
    assert [len(page.json()["items"]) for page in wide] == [200, 200, 5]
    assert [name for page in wide for name in _names(page)] == names
    assert first.json()["complete"] is False
    assert pages[-1].json()["complete"] is True
    assert changed.status_code == other_scope.status_code == tampered.status_code == duplicate_mode.status_code == 400
    assert conflict.status_code == 409


def test_search_cursor_expires_and_removes_its_private_snapshot(tmp_path) -> None:
    for index in range(3):
        (DOCUMENTS / f"note-{index}.txt").write_text("body", encoding="utf-8")
    clock = ManualClock(CLOCK_START)
    with boot(tmp_path / "app", clock, password=PASSWORD) as world:
        _signed(world)
        first = _search(world, "note", mode="name", limit="1")
        cursor = first.json()["nextCursor"]
        cache = world.config.state_directory / "cache" / "search-cursors"
        assert len(list(cache.glob("[0-9a-f]*.json"))) == 1
        clock.advance(901)
        expired = _search(world, "note", mode="name", limit="1", cursor=cursor)
        remaining = list(cache.glob("[0-9a-f]*.json"))

    assert expired.status_code == 410
    assert remaining == []


def test_search_rejects_oversize_and_budget_stop_is_incomplete(tmp_path, monkeypatch) -> None:
    from hopper_files import search as search_module

    (DOCUMENTS / "entry.txt").write_text("entry", encoding="utf-8")
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        _signed(world)
        oversized = _search(world, "x" * 257, mode="name")
        monkeypatch.setattr(search_module, "SEARCH_TIME_LIMIT_SECONDS", -1.0)
        stopped = _search(world, "entry", mode="name")

    assert oversized.status_code == 413
    assert stopped.status_code == 200
    assert stopped.json()["complete"] is False
    assert "execution_budget" in _categories(stopped, "omissions")


def test_search_lists_the_internal_trash_and_never_enters_pseudo_filesystems(tmp_path) -> None:
    """HF-API-004/HF-NAV-011: state and trash are searchable; /proc, /sys, /dev are not entered."""
    from hopper_files.roots import BASE_ID, RootCatalog, DocumentRoot, scan_tree
    from pathlib import Path
    import time

    (DOCUMENTS / "trash-needle.md").write_text("to trash", encoding="utf-8")
    state = tmp_path / "app" / "state"
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD, state=state) as world:
        headers = _signed(world)
        deleted = world.client.post(
            world.config.base_path + "api/trash",
            json={"action": "delete", "source": {"rootId": ROOT, "path": doc("trash-needle.md")}},
            headers=headers,
        )
        assert deleted.status_code == 201, deleted.text
        found = _search(world, "payload", scope="/" + str(state / "trash")[1:], mode="name")

    assert found.status_code == 200
    assert len(found.json()["items"]) == 1
    # Metadata-only traversal from /: the pseudo-filesystem mount points are skipped.
    catalog = RootCatalog(1, (DocumentRoot(BASE_ID, "/", Path("/")),), state, state / "config.json")
    counts: dict[str, int] = {}
    tree = scan_tree(catalog, BASE_ID, "", deadline=time.monotonic() + 0.2, skip_internal_trash=True, counts=counts)
    first = next(tree)
    names = {entry.name for entry in first.entries}
    visited = [first.path] + [directory.path for directory, _ in zip(tree, range(200))]
    tree.close()
    assert {"proc", "sys", "dev"} <= names
    assert not any(path.split("/")[0] in {"proc", "sys", "dev"} for path in visited if path)


def test_markdown_tags_skip_code_urls_destinations_and_colors() -> None:
    from hopper_files.search import extract_markdown_tags

    tags = extract_markdown_tags(
        "#ação #Ação #valid-2 #not-valid- #ffffff #face\n"
        "`#inline` [texto #body](https://example.test/#destination)\n"
        "https://example.test/#url\n"
        "```md\n#fenced\n```\n"
    )

    assert tags == {"ação", "body", "valid-2"}


def test_tag_index_reads_monitored_folders_with_read_only_notes_and_skips_the_trash(tmp_path) -> None:
    (DOCUMENTS / "one.md").write_text("#ação #common `#hidden`", encoding="utf-8")
    (DOCUMENTS / "sub").mkdir()
    (DOCUMENTS / "sub" / "two.md").write_text("#common", encoding="utf-8")
    (DOCUMENTS / "gone.md").write_text("#deleted", encoding="utf-8")
    frozen = DOCUMENTS / "frozen"
    frozen.mkdir()
    (frozen / "frozen.md").write_text("#frozen", encoding="utf-8")
    frozen.chmod(0o555)
    try:
        with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
            headers = _signed(world)
            unmonitored = world.client.get(world.config.base_path + "api/tags")
            _monitor(world, headers)
            before = world.client.get(world.config.base_path + "api/tags")
            deleted = world.client.post(
                world.config.base_path + "api/trash",
                json={"action": "delete", "source": {"rootId": ROOT, "path": doc("gone.md")}},
                headers=headers,
            )
            tags = world.client.get(world.config.base_path + "api/tags")
            selected = world.client.get(world.config.base_path + "api/tags", params={"tag": "common"})
            state = world.client.get(world.config.base_path + "api/state")
            trash = os.path.realpath(world.config.state_directory / "trash")
            trash_marked = world.client.post(
                world.config.base_path + "api/tag-folders", json={"path": trash, "monitored": True}, headers=headers
            )
            with_trash = world.client.get(world.config.base_path + "api/tags")
    finally:
        frozen.chmod(0o755)

    # HF-META-002: no folder is monitored by default, so there are no tags.
    assert unmonitored.status_code == 200 and unmonitored.json()["tags"] == [] and unmonitored.json()["complete"] is True
    assert deleted.status_code == 201, deleted.text
    # A note in a read-only directory under a monitored folder counts too.
    assert _tag_counts(before) == {"ação": 1, "common": 2, "deleted": 1, "frozen": 1}
    assert _tag_counts(tags) == {"ação": 1, "common": 2, "frozen": 1}
    assert tags.json()["complete"] is True
    assert [item["path"] for item in selected.json()["items"]] == [doc("one.md"), doc("sub/two.md")]
    assert set(state.json()["labels"]) == {"vermelho", "laranja", "amarelo", "verde", "azul", "roxo", "cinza"}
    # Even marked, the internal trash is never read: deleted notes do not return.
    assert trash_marked.status_code == 200, trash_marked.text
    assert _tag_counts(with_trash) == _tag_counts(tags)
    assert _categories(with_trash, "exclusions").get("internal_trash") == 1


def test_tag_index_reuses_derived_records_and_rereads_changed_notes(tmp_path) -> None:
    note = DOCUMENTS / "note.md"
    note.write_text("#first", encoding="utf-8")
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        _monitor(world, _signed(world))
        first = world.client.get(world.config.base_path + "api/tags")
        index = world.config.state_directory / "indexes" / "notes.json"
        assert index.is_file() and oct(index.stat().st_mode & 0o777) == oct(0o600)
        note.write_text("#second", encoding="utf-8")
        second = world.client.get(world.config.base_path + "api/tags")

    assert [item["tag"] for item in first.json()["tags"]] == ["first"]
    assert [item["tag"] for item in second.json()["tags"]] == ["second"]


def test_ignored_tags_leave_index_counts_and_lookup_until_restored(tmp_path) -> None:
    (DOCUMENTS / "one.md").write_text("#Tag #common", encoding="utf-8")
    (DOCUMENTS / "two.md").write_text("#tag", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in DOCUMENTS.iterdir()}
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _signed(world)
        _monitor(world, headers)

        def save(ignored: list[str] | None) -> None:
            current = world.client.get(world.config.base_path + "api/state").json()
            replacement = {key: value for key, value in current.items() if key != "stateRevision"}
            replacement["preferences"] = {key: value for key, value in current["preferences"].items() if key != "ignoredTags"}
            if ignored:
                replacement["preferences"]["ignoredTags"] = ignored
            saved = world.client.put(
                world.config.base_path + "api/state",
                json={"baseRevision": current["stateRevision"], **replacement},
                headers=headers,
            )
            assert saved.status_code == 200, saved.text

        save(["tag"])
        hidden = world.client.get(world.config.base_path + "api/tags")
        hidden_lookup = world.client.get(world.config.base_path + "api/tags", params={"tag": "tag"})
        common = world.client.get(world.config.base_path + "api/tags", params={"tag": "common"})
        save(None)
        restored = world.client.get(world.config.base_path + "api/tags")

    assert {item["tag"]: item["count"] for item in hidden.json()["tags"]} == {"common": 1}
    assert hidden_lookup.json()["tags"] == [] and hidden_lookup.json()["items"] == []
    assert common.json()["items"] == [{"rootId": ROOT, "path": doc("one.md"), "tags": ["common"]}]
    assert {item["tag"]: item["count"] for item in restored.json()["tags"]} == {"common": 1, "tag": 2}
    assert {path.name: path.read_bytes() for path in DOCUMENTS.iterdir()} == before


def test_trash_invalidates_search_snapshot_and_restores_ui_metadata(tmp_path) -> None:
    (DOCUMENTS / "favorito.md").write_text("needle favorito", encoding="utf-8")
    (DOCUMENTS / "outro.md").write_text("needle outro", encoding="utf-8")
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _signed(world)
        state = world.client.get(world.config.base_path + "api/state").json()
        replacement = {key: value for key, value in state.items() if key != "stateRevision"}
        replacement["items"] = [{
            "path": "/" + doc("favorito.md"),
            "labelIds": ["azul"],
            "favorite": True,
            "emoji": None,
            "inode": None,
            "device": None,
        }]
        saved = world.client.put(
            world.config.base_path + "api/state",
            json={"baseRevision": state["stateRevision"], **replacement},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text

        first = _search(world, "needle", mode="text", limit="1")
        assert first.status_code == 200 and first.json()["nextCursor"]
        cursor = first.json()["nextCursor"]
        deleted = world.client.post(
            world.config.base_path + "api/trash",
            json={"action": "delete", "source": {"rootId": ROOT, "path": doc("favorito.md")}},
            headers=headers,
        )
        assert deleted.status_code == 201, deleted.text
        stale = _search(world, "needle", mode="text", limit="1", cursor=cursor)
        state_after_delete = world.client.get(world.config.base_path + "api/state").json()
        restore = world.client.post(
            world.config.base_path + "api/trash",
            json={"action": "restore", "id": deleted.json()["id"], "alternativePath": doc("restored.md")},
            headers=headers,
        )
        state_after_restore = world.client.get(world.config.base_path + "api/state").json()
        fresh = _search(world, "needle", mode="text")

    assert stale.status_code == 409
    assert state_after_delete["items"] == []
    assert deleted.json()["uiMetadata"][0]["favorite"] is True
    assert restore.status_code == 200, restore.text
    restored_metadata = state_after_restore["items"]
    assert len(restored_metadata) == 1
    assert {key: restored_metadata[0][key] for key in ("path", "labelIds", "favorite", "emoji")} == {
        "path": "/" + doc("restored.md"),
        "labelIds": ["azul"],
        "favorite": True,
        "emoji": None,
    }
    assert isinstance(restored_metadata[0]["inode"], int)
    assert _names(fresh) == ["outro.md", "restored.md"]


def test_tag_index_skips_temporary_directories_unless_marked_and_keeps_their_cached_records(tmp_path, monkeypatch) -> None:
    import hopper_files.corpus

    # The product skips /tmp and /var/tmp below a monitored folder; a synthetic
    # folder plays that part.
    (DOCUMENTS / "kept.md").write_text("#kept", encoding="utf-8")
    (DOCUMENTS / "temporaria").mkdir()
    (DOCUMENTS / "temporaria" / "skipped.md").write_text("#skipped", encoding="utf-8")
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _signed(world)
        _monitor(world, headers)
        monkeypatch.setattr(hopper_files.corpus, "TAG_EXCLUDED_PATHS", frozenset())
        full = world.client.get(world.config.base_path + "api/tags")
        monkeypatch.setattr(hopper_files.corpus, "TAG_EXCLUDED_PATHS", frozenset({doc("temporaria")}))
        skipping = world.client.get(world.config.base_path + "api/tags")
        index = json.loads((world.config.state_directory / "indexes" / "notes.json").read_text(encoding="utf-8"))
        _monitor(world, headers, "temporaria")
        marked = world.client.get(world.config.base_path + "api/tags")

    assert [item["tag"] for item in full.json()["tags"]] == ["kept", "skipped"]
    assert [item["tag"] for item in skipping.json()["tags"]] == ["kept"]
    assert skipping.json()["complete"] is True
    assert _categories(skipping, "exclusions").get("excluded_path") == 1
    # A skipped folder is outside this traversal, so its record is not stale.
    assert doc("temporaria/skipped.md") in index["notes"]
    # Marked directly, the temporary folder enters like a hidden one would.
    assert _tag_counts(marked) == {"kept": 1, "skipped": 1}


def test_tag_folders_start_empty_and_only_existing_directories_can_be_marked(tmp_path) -> None:
    (DOCUMENTS / "pasta").mkdir()
    (DOCUMENTS / "pasta" / "nota.md").write_text("#nota", encoding="utf-8")
    (DOCUMENTS / "arquivo.md").write_text("#arquivo", encoding="utf-8")
    (DOCUMENTS / "atalho").symlink_to(DOCUMENTS / "pasta", target_is_directory=True)
    route = "api/tag-folders"
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        anonymous = world.client.get(world.config.base_path + route)
        anonymous_post = world.client.post(
            world.config.base_path + route,
            json={"path": "/" + doc("pasta"), "monitored": True},
            headers={"origin": world.config.origin},
        )
        headers = _signed(world)
        state_before = world.client.get(world.config.base_path + "api/state").json()
        empty = world.client.get(world.config.base_path + route)
        forged = world.client.post(
            world.config.base_path + route,
            json={"path": "/" + doc("pasta"), "monitored": True},
            headers={"origin": world.config.origin, "x-csrf-token": "forged"},
        )

        def post(body: object):
            return world.client.post(world.config.base_path + route, json=body, headers=headers)

        refused = {
            "missing": post({"path": "/" + doc("nada"), "monitored": True}),
            "file": post({"path": "/" + doc("arquivo.md"), "monitored": True}),
            "link": post({"path": "/" + doc("atalho"), "monitored": True}),
            "relative": post({"path": doc("pasta"), "monitored": True}),
            "dotted": post({"path": "/" + doc("pasta/../pasta"), "monitored": True}),
            "flag": post({"path": "/" + doc("pasta"), "monitored": "yes"}),
            "extra": post({"path": "/" + doc("pasta"), "monitored": True, "more": 1}),
        }
        marked = post({"path": "/" + doc("pasta"), "monitored": True})
        repeated = post({"path": "/" + doc("pasta"), "monitored": True})
        stored = json.loads((world.config.state_directory / "tag-folders.json").read_text(encoding="utf-8"))
        mode = (world.config.state_directory / "tag-folders.json").stat().st_mode & 0o777
        tags = world.client.get(world.config.base_path + "api/tags")
        state_after = world.client.get(world.config.base_path + "api/state").json()
        (DOCUMENTS / "pasta").rename(DOCUMENTS / "renomeada")
        vanished = world.client.get(world.config.base_path + route)
        vanished_tags = world.client.get(world.config.base_path + "api/tags")
        unmarked = post({"path": "/" + doc("pasta"), "monitored": False})
        absent = post({"path": "/" + doc("pasta"), "monitored": False})

    assert anonymous.status_code == anonymous_post.status_code == 401
    assert empty.status_code == 200 and empty.json() == {"folders": []}
    assert forged.status_code == 403
    assert {name: response.status_code for name, response in refused.items()} == {
        "missing": 403, "file": 403, "link": 403, "relative": 422, "dotted": 422, "flag": 422, "extra": 422,
    }
    assert refused["missing"].json()["error"] == "not_directory"
    assert marked.json() == repeated.json() == {"folders": [{"path": "/" + doc("pasta"), "available": True}]}
    assert stored == {"version": 1, "folders": ["/" + doc("pasta")]} and mode == 0o600
    assert _tag_counts(tags) == {"nota": 1}
    # The list lives outside ui-state.json: marking a folder changes no UI state.
    assert state_after == state_before
    assert vanished.json() == {"folders": [{"path": "/" + doc("pasta"), "available": False}]}
    assert vanished_tags.json()["tags"] == [] and vanished_tags.json()["complete"] is True
    assert _categories(vanished_tags, "exclusions") == {"unavailable_folder": 1}
    assert unmarked.json() == absent.json() == {"folders": []}


def test_tag_folders_skip_hidden_subfolders_unless_marked_but_read_hidden_files(tmp_path) -> None:
    (DOCUMENTS / "visivel.md").write_text("#topo", encoding="utf-8")
    (DOCUMENTS / ".nota-oculta.md").write_text("#arquivo-oculto", encoding="utf-8")
    (DOCUMENTS / "sub").mkdir()
    (DOCUMENTS / "sub" / "funda.md").write_text("#funda", encoding="utf-8")
    (DOCUMENTS / "sub" / ".git").mkdir()
    (DOCUMENTS / "sub" / ".git" / "log.md").write_text("#git", encoding="utf-8")
    (DOCUMENTS / ".oculta" / "dentro").mkdir(parents=True)
    (DOCUMENTS / ".oculta" / "segredo.md").write_text("#segredo", encoding="utf-8")
    (DOCUMENTS / ".oculta" / "dentro" / "mais.md").write_text("#mais", encoding="utf-8")
    (DOCUMENTS / ".oculta" / ".interna").mkdir()
    (DOCUMENTS / ".oculta" / ".interna" / "x.md").write_text("#interna", encoding="utf-8")
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _signed(world)
        _monitor(world, headers)
        parent_only = world.client.get(world.config.base_path + "api/tags")
        _monitor(world, headers, ".oculta")
        with_hidden = world.client.get(world.config.base_path + "api/tags")
        # Marking a folder the parent already reaches reads nothing twice.
        _monitor(world, headers, "sub")
        redundant = world.client.get(world.config.base_path + "api/tags")
        _monitor(world, headers, "", monitored=False)
        hidden_only = world.client.get(world.config.base_path + "api/tags")

    assert _tag_counts(parent_only) == {"arquivo-oculto": 1, "funda": 1, "topo": 1}
    assert _categories(parent_only, "exclusions")["hidden_directory"] == 2
    assert _tag_counts(with_hidden) == {"arquivo-oculto": 1, "funda": 1, "mais": 1, "segredo": 1, "topo": 1}
    assert _categories(with_hidden, "exclusions")["hidden_directory"] == 3
    assert _tag_counts(redundant) == _tag_counts(with_hidden)
    assert _tag_counts(hidden_only) == {"funda": 1, "mais": 1, "segredo": 1}
    assert all(response.json()["complete"] for response in (parent_only, with_hidden, redundant, hidden_only))


def test_tag_folders_count_unreadable_notes_as_exclusions(tmp_path) -> None:
    if os.geteuid() == 0:
        import pytest

        pytest.skip("root reads every file")
    (DOCUMENTS / "aberta.md").write_text("#aberta", encoding="utf-8")
    fechada = DOCUMENTS / "fechada.md"
    fechada.write_text("#fechada", encoding="utf-8")
    fechada.chmod(0o000)
    try:
        with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
            _monitor(world, _signed(world))
            tags = world.client.get(world.config.base_path + "api/tags")
    finally:
        fechada.chmod(0o600)

    assert _tag_counts(tags) == {"aberta": 1}
    assert tags.json()["complete"] is True
    assert _categories(tags, "exclusions") == {"unreadable_file": 1}


def test_invalid_tag_folders_document_fails_closed(tmp_path) -> None:
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _signed(world)
        (world.config.state_directory / "tag-folders.json").write_text('{"version":1,"folders":["relativo"]}', encoding="utf-8")
        listed = world.client.get(world.config.base_path + "api/tag-folders")
        tags = world.client.get(world.config.base_path + "api/tags")
        changed = world.client.post(
            world.config.base_path + "api/tag-folders",
            json={"path": "/" + doc(), "monitored": True},
            headers=headers,
        )
        kept = (world.config.state_directory / "tag-folders.json").read_text(encoding="utf-8")

    assert listed.status_code == tags.status_code == changed.status_code == 409
    assert kept == '{"version":1,"folders":["relativo"]}'


def test_tag_starts_put_ancestors_first_and_skips_follow_the_start() -> None:
    from hopper_files.corpus import tag_skips, tag_starts

    assert tag_starts([]) == ()
    assert tag_starts(["home/alice/.config", "home", "", "home"]) == ("", "home", "home/alice/.config")
    assert tag_skips("") == {"tmp", "var/tmp"} and tag_skips("var") == {"var/tmp"}
    assert tag_skips("tmp") == tag_skips("var/tmp") == tag_skips("home") == frozenset()


def test_marked_folder_below_a_directory_that_cannot_be_listed_is_still_read(tmp_path) -> None:
    if os.geteuid() == 0:
        import pytest

        pytest.skip("root lists every directory")
    (DOCUMENTS / "pai" / "trancada" / "notas").mkdir(parents=True)
    (DOCUMENTS / "pai" / "fora.md").write_text("#fora", encoding="utf-8")
    (DOCUMENTS / "pai" / "trancada" / "notas" / "n.md").write_text("#interna", encoding="utf-8")
    trancada = DOCUMENTS / "pai" / "trancada"
    trancada.chmod(0o311)  # the account passes through it but cannot list it
    try:
        with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
            headers = _signed(world)
            _monitor(world, headers, "pai/trancada/notas")
            inner_only = world.client.get(world.config.base_path + "api/tags")
            _monitor(world, headers, "pai")
            both = world.client.get(world.config.base_path + "api/tags")
            unlistable = world.client.post(
                world.config.base_path + "api/tag-folders",
                json={"path": "/" + doc("pai/trancada"), "monitored": True},
                headers=headers,
            )
            listed = world.client.get(world.config.base_path + "api/tag-folders")
            trancada.chmod(0o755)
            _monitor(world, headers, "pai/trancada")
            trancada.chmod(0o311)
            unavailable = world.client.get(world.config.base_path + "api/tag-folders")
            with_unavailable = world.client.get(world.config.base_path + "api/tags")
    finally:
        trancada.chmod(0o755)

    assert _tag_counts(inner_only) == {"interna": 1}
    # The marked folder below keeps being read after its ancestor is marked too.
    assert _tag_counts(both) == {"fora": 1, "interna": 1}
    assert _categories(both, "exclusions") == {"unreadable_directory": 1}
    # A folder the account cannot list cannot be marked; one that stops being
    # listable reads as unavailable in the list and in the index alike.
    assert unlistable.status_code == 403 and unlistable.json()["error"] == "not_directory"
    assert [folder["available"] for folder in listed.json()["folders"]] == [True, True]
    assert {folder["path"]: folder["available"] for folder in unavailable.json()["folders"]}["/" + doc("pai/trancada")] is False
    assert _tag_counts(with_unavailable) == {"fora": 1, "interna": 1}
    assert _categories(with_unavailable, "exclusions").get("unavailable_folder") == 1


def test_corpus_scans_keep_records_that_only_the_tag_index_reads(tmp_path) -> None:
    from hopper_files.note_index import observe_corpus
    from hopper_files.roots import build_catalog

    (DOCUMENTS / "escrita.md").write_text("#escrita", encoding="utf-8")
    (DOCUMENTS / "apagada.md").write_text("#apagada", encoding="utf-8")
    fixa = DOCUMENTS / "fixa"
    fixa.mkdir()
    (fixa / "leitura.md").write_text("#leitura", encoding="utf-8")
    fixa.chmod(0o555)
    try:
        with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
            _monitor(world, _signed(world))
            tags = world.client.get(world.config.base_path + "api/tags")
            (DOCUMENTS / "apagada.md").unlink()
            observe_corpus(build_catalog(world.config))
            index = json.loads((world.config.state_directory / "indexes" / "notes.json").read_text(encoding="utf-8"))
    finally:
        fixa.chmod(0o755)

    assert _tag_counts(tags) == {"apagada": 1, "escrita": 1, "leitura": 1}
    # The corpus traversal drops the deleted note but keeps the read-only one.
    assert doc("fixa/leitura.md") in index["notes"] and doc("escrita.md") in index["notes"]
    assert doc("apagada.md") not in index["notes"]


def test_tag_folders_stop_at_the_limit_with_413(tmp_path) -> None:
    for index in range(65):
        (DOCUMENTS / f"p{index:02d}").mkdir()
    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        headers = _signed(world)
        for index in range(64):
            _monitor(world, headers, f"p{index:02d}")
        over = world.client.post(
            world.config.base_path + "api/tag-folders",
            json={"path": "/" + doc("p64"), "monitored": True},
            headers=headers,
        )
        listed = world.client.get(world.config.base_path + "api/tag-folders")

    assert over.status_code == 413 and over.json()["error"] == "limit_exceeded"
    assert len(listed.json()["folders"]) == 64


def _slow(started: threading.Event, value: dict[str, object]):
    def run(*_args, **_kwargs):
        started.set()
        time.sleep(1.0)
        return value

    return run


def test_tag_index_and_search_do_not_queue_listings(tmp_path, monkeypatch) -> None:
    import hopper_files.api.search as search_api

    with boot(tmp_path / "app", ManualClock(CLOCK_START), password=PASSWORD) as world:
        _signed(world)
        for name, route, value in (
            ("derive_tag_index", "api/tags", {"complete": True, "omissions": [], "exclusions": [], "tags": [], "notes": []}),
            ("search", "api/search?q=x&mode=name&scope=/" + D, {"items": [], "complete": True}),
        ):
            started = threading.Event()
            monkeypatch.setattr(search_api, name, _slow(started, value))
            answers: dict[str, object] = {}
            worker = threading.Thread(target=lambda: answers.update(slow=world.client.get(world.config.base_path + route)))
            worker.start()
            assert started.wait(5)
            begin = time.monotonic()
            listing = world.client.get(world.config.base_path + "api/list", params=address())
            elapsed = time.monotonic() - begin
            still_running = worker.is_alive()
            worker.join(10)

            assert listing.status_code == 200, listing.text
            assert still_running and elapsed < 0.5, (route, elapsed)
            assert answers["slow"].status_code == 200
