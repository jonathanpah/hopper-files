"""Conservative streaming inventory of relative Markdown image destinations.

This module deliberately recognizes only Markdown image references. Ordinary
links, HTML, wiki links, and external URLs are outside the managed attachment
identity model. Any ambiguous local reference makes a collection scan fail
closed.
"""

from __future__ import annotations

import html
import os
import re
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote_to_bytes

from hopper_files.roots import (
    BASE_ID,
    AddressRejected,
    RootCatalog,
    address_has_missing_component,
    inspect_address,
    open_regular,
    open_scanned_regular,
)


class ReferenceScanError(Exception):
    """The scanner cannot prove that a candidate has no saved references."""


MAX_MARKDOWN_SCAN_BYTES = 1024 * 1024 * 1024
MAX_MARKDOWN_REFERENCES = 1_000_000
MAX_MARKDOWN_LINE_BYTES = 8 * 1024 * 1024
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_EXTERNAL_SCHEMES = {"http", "https", "data", "mailto", "cid", "ftp", "tel"}
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


@dataclass(frozen=True, order=True)
class ImageIdentity:
    root_id: str
    path: str


@dataclass(frozen=True)
class MarkdownSnapshot:
    identity: ImageIdentity
    references: frozenset[ImageIdentity]
    version: tuple[int, int, int, int, int, int]
    digest: str
    content: bytes | None = None


@dataclass(frozen=True)
class DestinationSpan:
    start: int
    end: int
    destination: str
    identity: ImageIdentity | None


@dataclass(frozen=True)
class CorpusReferences:
    """Saved image identities per note, and notes whose references are unknown."""

    references: dict[ImageIdentity, frozenset[ImageIdentity]]
    undetermined: dict[str, str]

    def blocking(self, catalog: RootCatalog, names: set[str], paths: set[str] = frozenset()) -> list[str]:
        """Undetermined notes that might reference a file named in ``names``.

        A note inside ``paths`` (moved objects) blocks unconditionally because
        its references cannot be rewritten.
        """
        from hopper_files.note_index import mentions_any

        blocked = []
        for note, reason in sorted(self.undetermined.items()):
            # A note stopped by a scanner limit was not read completely; its
            # references stay unknown for every candidate (HF-IMG-005).
            if "limit" in reason:
                blocked.append(note)
            elif any(note == path or note.startswith(path + "/") for path in paths) or mentions_any(catalog, note, names):
                blocked.append(note)
        return blocked


def scan_corpus(catalog: RootCatalog) -> CorpusReferences:
    """Resolve saved image references of every note in the corpus (HF-NAV-004).

    The traversal follows HF-NAV-011. A budget stop, a race, or a read error
    makes the whole result inconclusive. A note whose references cannot be
    determined is reported separately; a caller treats it as a possible
    reference to any file whose name the note contains.
    """
    from hopper_files.note_index import observe_corpus

    generation = catalog.generation
    try:
        view = observe_corpus(catalog)
    except (AddressRejected, OSError) as exc:
        raise ReferenceScanError("the note corpus cannot be scanned completely") from exc
    if view.scan is None or not view.scan.complete:
        raise ReferenceScanError("the note corpus scan is incomplete")
    references: dict[ImageIdentity, frozenset[ImageIdentity]] = {}
    undetermined: dict[str, str] = {}
    for path, record in view.notes.items():
        if record.destinations is None:
            undetermined[path] = record.reference_error or "unparsed"
            continue
        resolved: set[ImageIdentity] = set()
        try:
            for destination in record.destinations:
                target = resolve_destination(catalog, BASE_ID, path, destination)
                if target is not None:
                    resolved.add(target)
        except ReferenceScanError as exc:
            undetermined[path] = str(exc)
            continue
        references[ImageIdentity(BASE_ID, path)] = frozenset(resolved)
    if catalog.generation != generation:
        raise ReferenceScanError("document root generation changed")
    return CorpusReferences(references, undetermined)


def scan_catalog(catalog: RootCatalog) -> dict[ImageIdentity, frozenset[ImageIdentity]]:
    """Return every saved image reference, failing when any note is undetermined."""
    corpus = scan_corpus(catalog)
    if corpus.undetermined:
        raise ReferenceScanError("a note's image references cannot be determined")
    return corpus.references


def scan_document(
    catalog: RootCatalog,
    root_id: str,
    path: str,
    *,
    keep_content: bool = False,
) -> MarkdownSnapshot:
    """Read one Markdown source completely and parse its image destinations."""
    if not path.lower().endswith((".md", ".markdown")):
        raise ReferenceScanError("not a Markdown document")
    before_catalog = catalog.generation
    try:
        handle = open_regular(catalog, root_id, path)
        with handle as (fd, initial):
            if not stat.S_ISREG(initial.st_mode):
                raise ReferenceScanError("Markdown object is not regular")
            version = _stat_version(initial)
            parser = _MarkdownParser(catalog, root_id, path)
            data, digest = _read_stable(fd, initial, parser.feed_line, keep_content=keep_content)
            final = os.fstat(fd)
            if _stat_version(final) != version:
                raise ReferenceScanError("Markdown content changed while scanning")
    except (AddressRejected, OSError) as exc:
        raise ReferenceScanError("Markdown document is unavailable") from exc
    if catalog.generation != before_catalog:
        raise ReferenceScanError("document root generation changed")
    references = parser.finish()
    canonical = "/".join(part for part in path.split("/") if part not in {"", "."})
    return MarkdownSnapshot(
        ImageIdentity(root_id, canonical), references, version, digest,
        data if keep_content else None,
    )


def resolve_destination(catalog: RootCatalog, note_root: str, note_path: str, destination: str) -> ImageIdentity | None:
    """Resolve one Markdown destination; return None for non-local URLs.

    A relative destination, which may contain ``..``, resolves against the
    note's directory to an absolute canonical path (HF-IMG-002). It must not
    traverse a symbolic link on the way to an existing file.
    """
    raw = html.unescape(destination.strip())
    if not raw or raw.startswith("<") or raw.startswith("#"):
        return None
    raw = _unescape_markdown(raw)
    if raw.startswith(("//", "/", "\\")) or "\\" in raw or "\x00" in raw:
        raise ReferenceScanError("absolute or malformed local image reference")
    if _PERCENT_ESCAPE.search(unquote_to_text_once(raw)):
        raise ReferenceScanError("double-encoded image reference")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw):
        scheme = raw.split(":", 1)[0].lower()
        if scheme in _EXTERNAL_SCHEMES:
            return None
        raise ReferenceScanError("unsupported image reference scheme")
    decoded = unquote_to_text_once(raw)
    if decoded.startswith(("/", "\\")) or "\\" in decoded or "\x00" in decoded:
        raise ReferenceScanError("absolute or malformed image reference")
    if "?" in decoded or "#" in decoded:
        raise ReferenceScanError("query or fragment on a local image reference")

    if note_root != BASE_ID:
        raise ReferenceScanError("source note address is unavailable")
    absolute_parts = note_path.split("/")[:-1]
    for component in decoded.split("/"):
        if component in {"", "."}:
            continue
        if component == "..":
            if not absolute_parts:
                raise ReferenceScanError("image reference escapes filesystem")
            absolute_parts.pop()
        else:
            absolute_parts.append(component)
    if not absolute_parts:
        raise ReferenceScanError("image reference names the filesystem base")
    path = "/".join(absolute_parts)
    try:
        inspection = inspect_address(catalog, BASE_ID, path, operation="read")
    except AddressRejected as exc:
        if exc.code == "not_found":
            return ImageIdentity(BASE_ID, path)
        raise ReferenceScanError("image reference crosses an unsafe path") from exc
    if not inspection.allowed:
        # A missing target remains a valid textual identity; a forbidden, symbolic-link, or
        # unavailable target does not.
        if inspection.code == "not_found":
            return ImageIdentity(BASE_ID, path)
        # The ordinary walker treats an absent component and a symlink alike.
        # A second descriptor walk proves absence while still rejecting links.
        if address_has_missing_component(catalog, BASE_ID, path):
            return ImageIdentity(BASE_ID, path)
        raise ReferenceScanError("image reference crosses an unsafe path")
    if not inspection.is_regular:
        raise ReferenceScanError("image reference target is not a regular file")
    return ImageIdentity(BASE_ID, path)


def relative_markdown_destination(
    catalog: RootCatalog,
    source_root: str,
    source_path: str,
    target_root: str,
    target_path: str,
) -> str:
    """Make the minimal percent-escaped relative destination for two addresses."""
    if source_root != BASE_ID or target_root != BASE_ID:
        raise ReferenceScanError("document address is unavailable")
    source_dir = PurePosixPath("/", *source_path.split("/")[:-1])
    target_file = PurePosixPath("/", *target_path.split("/"))
    try:
        relative = os.path.relpath(str(target_file), str(source_dir))
    except ValueError as exc:
        raise ReferenceScanError("cannot form a relative image reference") from exc
    return "/".join(_quote_component(component) for component in relative.split("/"))


def image_destination_spans(
    catalog: RootCatalog,
    root_id: str,
    path: str,
    content: str,
) -> tuple[DestinationSpan, ...]:
    """Locate parsed image destinations; refuse definitions shared with links."""
    spans: list[tuple[int, int, str, str | None]] = []
    definitions: dict[str, tuple[int, int, str]] = {}
    ordinary_labels: set[str] = set()
    in_fence: tuple[str, int] | None = None
    in_comment = False
    code_delimiter = 0
    offset = 0
    for line in content.splitlines(keepends=True):
        logical = line.rstrip("\r\n")
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", logical)
        if in_fence:
            if fence and fence.group(1)[0] == in_fence[0] and len(fence.group(1)) >= in_fence[1] and not fence.group(2).strip(" `~\t"):
                in_fence = None
            offset += len(line)
            continue
        if fence and not (fence.group(1).startswith("`") and "`" in fence.group(2)):
            in_fence = (fence.group(1)[0], len(fence.group(1)))
            offset += len(line)
            continue
        clean, in_comment, code_delimiter = _mask_inline_code(logical, in_comment, code_delimiter)
        definition = re.match(r"^ {0,3}\[([^\]]+)\]:[ \t]*(<[^>]*>|\S+)", clean)
        if definition:
            label = _normalize_label(definition.group(1))
            raw = definition.group(2)
            begin = definition.start(2) + (1 if raw.startswith("<") else 0)
            end = definition.end(2) - (1 if raw.startswith("<") else 0)
            value = raw[1:-1] if raw.startswith("<") else raw
            if label in definitions:
                raise ReferenceScanError("ambiguous Markdown reference definition")
            definitions[label] = (offset + begin, offset + end, value)
        else:
            ordinary_labels.update(_ordinary_reference_labels(clean))
        for destination, label, begin, end in _image_spans_line(clean):
            spans.append((offset + begin, offset + end, destination, label))
        offset += len(line)
    if in_fence is not None or in_comment or code_delimiter:
        raise ReferenceScanError("unterminated Markdown construct")
    if ({label for _start, _end, _destination, label in spans if label is not None} & ordinary_labels):
        raise ReferenceScanError("image reference definition is also used by an ordinary link")
    result: list[DestinationSpan] = []
    seen: dict[tuple[int, int], DestinationSpan] = {}
    for begin, end, destination, label in spans:
        if label is not None:
            definition = definitions.get(label)
            if definition is None:
                raise ReferenceScanError("unresolved Markdown image reference")
            begin, end, destination = definition
        identity = resolve_destination(catalog, root_id, path, destination)
        current = DestinationSpan(begin, end, destination, identity)
        prior = seen.get((begin, end))
        if prior is not None:
            if prior.identity != current.identity:
                raise ReferenceScanError("ambiguous shared image destination")
            continue
        seen[(begin, end)] = current
        result.append(current)
    return tuple(sorted(result, key=lambda item: item.start))


def _read_stable(
    fd: int,
    initial: os.stat_result,
    feed_text: object | None = None,
    *,
    keep_content: bool = False,
) -> tuple[bytes, str]:
    import hashlib

    digest = hashlib.sha256()
    blocks: list[bytes] = []
    total = 0
    byte_buffer = bytearray()
    while True:
        block = os.read(fd, 128 * 1024)
        if not block:
            break
        total += len(block)
        if total > MAX_MARKDOWN_SCAN_BYTES:
            raise ReferenceScanError("Markdown scanner limit reached")
        digest.update(block)
        if keep_content:
            blocks.append(block)
        if feed_text is not None:
            byte_buffer.extend(block)
            start = 0
            for match in _LINE_BREAK.finditer(byte_buffer):
                if match.start() == len(byte_buffer) - 1 and byte_buffer[match.start()] == 13:
                    break
                line_bytes = bytes(byte_buffer[start : match.start()])
                if len(line_bytes) > MAX_MARKDOWN_LINE_BYTES:
                    raise ReferenceScanError("Markdown line scanner limit reached")
                try:
                    line = line_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise ReferenceScanError("Markdown is not valid UTF-8") from exc
                feed_text(line)
                start = match.end()
            if start:
                del byte_buffer[:start]
            if len(byte_buffer) > MAX_MARKDOWN_LINE_BYTES:
                raise ReferenceScanError("Markdown line scanner limit reached")
    if feed_text is not None:
        if byte_buffer.endswith(b"\r"):
            byte_buffer.pop()
        if len(byte_buffer) > MAX_MARKDOWN_LINE_BYTES:
            raise ReferenceScanError("Markdown line scanner limit reached")
        if byte_buffer:
            try:
                final_line = bytes(byte_buffer).decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ReferenceScanError("Markdown is not valid UTF-8") from exc
            feed_text(final_line)
        elif total == 0:
            feed_text("")
    final = os.fstat(fd)
    if _stat_version(initial) != _stat_version(final) or total != initial.st_size:
        raise ReferenceScanError("Markdown content changed while scanning")
    return b"".join(blocks) if keep_content else b"", digest.hexdigest()


def _parse_markdown(catalog: RootCatalog, root_id: str, path: str, data: bytes) -> frozenset[ImageIdentity]:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReferenceScanError("Markdown is not valid UTF-8") from exc
    parser = _MarkdownParser(catalog, root_id, path)
    for line in text.splitlines():
        parser.feed_line(line)
    return parser.finish()


def parse_markdown_stream(catalog: RootCatalog, root_id: str, path: str, fd: int) -> frozenset[ImageIdentity]:
    """Parse an already-open Markdown file completely without buffering it."""
    generation = catalog.generation
    try:
        initial = os.fstat(fd)
        if not stat.S_ISREG(initial.st_mode):
            raise ReferenceScanError("Markdown object is not regular")
        parser = _MarkdownParser(catalog, root_id, path)
        _read_stable(fd, initial, parser.feed_line)
        references = parser.finish()
    except OSError as exc:
        raise ReferenceScanError("Markdown document is unavailable") from exc
    if catalog.generation != generation:
        raise ReferenceScanError("document root generation changed")
    return references


class _MarkdownParser:
    def __init__(self, catalog: RootCatalog, root_id: str, path: str) -> None:
        self.catalog = catalog
        self.root_id = root_id
        self.path = path
        self.definitions: dict[str, str] = {}
        self.images: list[tuple[str, str | None]] = []
        self.in_fence: tuple[str, int] | None = None
        self.in_comment = False
        self.code_delimiter = 0
        self.references = 0
        self.text_buffer = ""

    def feed_text(self, text: str) -> None:
        self.text_buffer += text
        lines = self.text_buffer.split("\n")
        self.text_buffer = lines.pop()
        for line in lines:
            self.feed_line(line[:-1] if line.endswith("\r") else line)

    def feed_line(self, line: str) -> None:
        if len(line.encode("utf-8")) > MAX_MARKDOWN_LINE_BYTES:
            raise ReferenceScanError("Markdown line scanner limit reached")
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if self.in_fence:
            if fence and fence.group(1)[0] == self.in_fence[0] and len(fence.group(1)) >= self.in_fence[1] and not fence.group(2).strip(" `~\t"):
                self.in_fence = None
            return
        if fence and not (fence.group(1).startswith("`") and "`" in fence.group(2)):
            self.in_fence = (fence.group(1)[0], len(fence.group(1)))
            return
        clean, self.in_comment, self.code_delimiter = _mask_inline_code(
            line, self.in_comment, self.code_delimiter
        )
        definition = re.match(r"^ {0,3}\[([^\]]+)\]:\s*(<[^>]*>|\S+)", clean)
        if definition:
            key = _normalize_label(definition.group(1))
            destination = definition.group(2)
            if destination.startswith("<"):
                destination = destination[1:-1]
            if key in self.definitions:
                raise ReferenceScanError("ambiguous Markdown reference definition")
            self.definitions[key] = destination
        line_images = _image_destinations(clean)
        self.images.extend(line_images)
        self.references += len(line_images)
        if self.references > MAX_MARKDOWN_REFERENCES:
            raise ReferenceScanError("Markdown reference scanner limit reached")

    def finish(self) -> frozenset[ImageIdentity]:
        if self.text_buffer:
            self.feed_line(self.text_buffer[:-1] if self.text_buffer.endswith("\r") else self.text_buffer)
            self.text_buffer = ""
        result: set[ImageIdentity] = set()
        for destination in self.destinations():
            target = resolve_destination(self.catalog, self.root_id, self.path, destination)
            if target is not None:
                result.add(target)
        return frozenset(result)

    def destinations(self) -> list[str]:
        """Return the raw image destinations, before resolving them on disk."""
        if self.text_buffer:
            self.feed_line(self.text_buffer[:-1] if self.text_buffer.endswith("\r") else self.text_buffer)
            self.text_buffer = ""
        if self.in_fence is not None or self.in_comment or self.code_delimiter:
            raise ReferenceScanError("unterminated Markdown construct")
        result: list[str] = []
        for destination, reference_label in self.images:
            if reference_label is not None:
                destination = self.definitions.get(reference_label, "")
                if not destination:
                    raise ReferenceScanError("unresolved Markdown image reference")
            result.append(destination)
        return result


def _image_destinations(line: str) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    cursor = 0
    while cursor < len(line):
        start = line.find("![", cursor)
        if start < 0:
            break
        if start and _escaped(line, start):
            cursor = start + 2
            continue
        label_end = _matching_bracket(line, start + 1)
        if label_end < 0:
            raise ReferenceScanError("ambiguous Markdown image syntax")
        index = label_end + 1
        while index < len(line) and line[index] in " \t":
            index += 1
        if index < len(line) and line[index] == "(":
            destination, after = _inline_destination(line, index + 1)
            found.append((destination, None))
            cursor = after
            continue
        if index < len(line) and line[index] == "[":
            ref_end = _matching_bracket(line, index)
            if ref_end < 0:
                raise ReferenceScanError("ambiguous Markdown image reference")
            label = line[index + 1 : ref_end] or line[start + 2 : label_end]
            found.append(("", _normalize_label(label)))
            cursor = ref_end + 1
            continue
        found.append(("", _normalize_label(line[start + 2 : label_end])))
        cursor = label_end + 1
    return found


def _image_spans_line(line: str) -> list[tuple[str, str | None, int, int]]:
    found: list[tuple[str, str | None, int, int]] = []
    cursor = 0
    while cursor < len(line):
        start = line.find("![", cursor)
        if start < 0:
            break
        if start and _escaped(line, start):
            cursor = start + 2
            continue
        label_end = _matching_bracket(line, start + 1)
        if label_end < 0:
            raise ReferenceScanError("ambiguous Markdown image syntax")
        index = label_end + 1
        while index < len(line) and line[index] in " \t":
            index += 1
        if index < len(line) and line[index] == "(":
            position = index + 1
            while position < len(line) and line[position] in " \t":
                position += 1
            if position < len(line) and line[position] == "<":
                end = position + 1
                while end < len(line) and (line[end] != ">" or _escaped(line, end)):
                    end += 1
                if end >= len(line):
                    raise ReferenceScanError("unterminated angle destination")
                _destination, after = _inline_destination(line, index + 1)
                found.append((line[position + 1 : end], None, position + 1, end))
            else:
                destination, after = _inline_destination(line, index + 1)
                begin = position
                found.append((destination, None, begin, begin + len(destination)))
            cursor = after
            continue
        if index < len(line) and line[index] == "[":
            ref_end = _matching_bracket(line, index)
            if ref_end < 0:
                raise ReferenceScanError("ambiguous Markdown image reference")
            label = line[index + 1 : ref_end] or line[start + 2 : label_end]
            found.append(("", _normalize_label(label), -1, -1))
            cursor = ref_end + 1
            continue
        found.append(("", _normalize_label(line[start + 2 : label_end]), -1, -1))
        cursor = label_end + 1
    return found


def _ordinary_reference_labels(line: str) -> set[str]:
    labels: set[str] = set()
    cursor = 0
    while cursor < len(line):
        opening = line.find("[", cursor)
        if opening < 0:
            break
        if opening and line[opening - 1] == "!":
            first_end = _matching_bracket(line, opening)
            if first_end < 0:
                raise ReferenceScanError("ambiguous Markdown image reference")
            after = first_end + 1
            while after < len(line) and line[after] in " \t":
                after += 1
            if after < len(line) and line[after] == "(":
                after = _matching_parenthesis(line, after)
            elif after < len(line) and line[after] == "[":
                second_end = _matching_bracket(line, after)
                if second_end < 0:
                    raise ReferenceScanError("ambiguous Markdown image reference")
                after = second_end + 1
            cursor = max(after, first_end + 1)
            continue
        first_end = _matching_bracket(line, opening)
        if first_end < 0:
            raise ReferenceScanError("ambiguous Markdown link reference")
        after = first_end + 1
        while after < len(line) and line[after] in " \t":
            after += 1
        if after < len(line) and line[after] == "(":
            after = _matching_parenthesis(line, after)
            cursor = after
            continue
        first_label = line[opening + 1 : first_end]
        if after < len(line) and line[after] == "[":
            second_end = _matching_bracket(line, after)
            if second_end < 0:
                raise ReferenceScanError("ambiguous Markdown link reference")
            second_label = line[after + 1 : second_end]
            labels.add(_normalize_label(second_label or first_label))
            cursor = second_end + 1
        else:
            labels.add(_normalize_label(first_label))
            cursor = first_end + 1
    return labels


def _inline_destination(line: str, position: int) -> tuple[str, int]:
    while position < len(line) and line[position] in " \t":
        position += 1
    if position >= len(line):
        raise ReferenceScanError("multiline Markdown image destination")
    if line[position] == "<":
        end = position + 1
        while end < len(line):
            if line[end] == ">" and not _escaped(line, end):
                cursor = end + 1
                before_space = cursor
                while cursor < len(line) and line[cursor] in " \t":
                    cursor += 1
                if cursor < len(line) and line[cursor] == ")":
                    return line[position + 1 : end], cursor + 1
                if cursor == before_space:
                    raise ReferenceScanError("ambiguous Markdown image title")
                if cursor >= len(line) or line[cursor] not in "\"' (":
                    raise ReferenceScanError("ambiguous Markdown image title")
                title_open = line[cursor]
                if title_open == "(":
                    depth = 1
                    cursor += 1
                    while cursor < len(line) and depth:
                        if _escaped(line, cursor):
                            cursor += 2
                            continue
                        if line[cursor] == "(":
                            depth += 1
                        elif line[cursor] == ")":
                            depth -= 1
                        cursor += 1
                    if depth:
                        raise ReferenceScanError("unterminated Markdown image title")
                else:
                    cursor += 1
                    while cursor < len(line):
                        if line[cursor] == title_open and not _escaped(line, cursor):
                            break
                        cursor += 2 if _escaped(line, cursor) else 1
                    if cursor >= len(line):
                        raise ReferenceScanError("unterminated Markdown image title")
                    cursor += 1
                while cursor < len(line) and line[cursor] in " \t":
                    cursor += 1
                if cursor >= len(line) or line[cursor] != ")":
                    raise ReferenceScanError("ambiguous Markdown image title")
                return line[position + 1 : end], cursor + 1
            end += 1
        raise ReferenceScanError("unterminated angle destination")
    start = position
    depth = 0
    while position < len(line):
        character = line[position]
        if _escaped(line, position):
            position += 1
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return line[start:position], position + 1
            depth -= 1
        elif character in " \t" and depth == 0:
            destination = line[start:position]
            close = line.find(")", position)
            if close < 0:
                raise ReferenceScanError("unterminated Markdown image")
            return destination, close + 1
        position += 1
    raise ReferenceScanError("unterminated Markdown image")


def _matching_bracket(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if _escaped(text, index):
            continue
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _matching_parenthesis(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if _escaped(text, index):
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ReferenceScanError("ambiguous Markdown inline link")


def _mask_inline_code(line: str, in_comment: bool, delimiter: int) -> tuple[str, bool, int]:
    output = list(line)
    index = 0
    while index < len(line):
        if in_comment:
            end = line.find("-->", index)
            stop = len(line) if end < 0 else end + 3
            for offset in range(index, stop):
                output[offset] = " "
            index = stop
            if end >= 0:
                in_comment = False
            continue
        if delimiter:
            run = _backtick_run(line, index)
            if run == delimiter:
                for offset in range(index, index + run):
                    output[offset] = " "
                index += run
                delimiter = 0
            else:
                output[index] = " "
                index += 1
            continue
        if line.startswith("<!--", index):
            in_comment = True
            continue
        run = _backtick_run(line, index)
        if run:
            end = index + run
            close = _find_backtick_run(line, end, run)
            if close < 0:
                delimiter = run
                for offset in range(index, len(line)):
                    output[offset] = " "
                break
            for offset in range(index, close + run):
                output[offset] = " "
            index = close + run
            continue
        index += 1
    return "".join(output), in_comment, delimiter


def _find_backtick_run(text: str, start: int, wanted: int) -> int:
    index = start
    while index < len(text):
        run = _backtick_run(text, index)
        if run == wanted:
            return index
        index += max(run, 1)
    return -1


def _backtick_run(text: str, position: int) -> int:
    if position >= len(text) or text[position] != "`":
        return 0
    end = position
    while end < len(text) and text[end] == "`":
        end += 1
    return end - position


def _escaped(text: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _normalize_label(value: str) -> str:
    return " ".join(_unescape_markdown(value).split()).casefold()


def _unescape_markdown(value: str) -> str:
    return re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", value)


def unquote_to_text_once(value: str) -> str:
    try:
        return unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReferenceScanError("image reference is not valid UTF-8") from exc


def _quote_component(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="-._~")


def _stat_version(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)
