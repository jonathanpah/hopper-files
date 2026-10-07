# Instance runtime

This note describes what a running instance does: access control, the
filesystem base, navigation, search, metadata, file operations, the trash,
the editor and viewers, managed images and moves, the systemd unit, and the
state directory. It is not a second specification; the
[specification](specification.md) is the contract. Installation and
administration are in [operations](operations.md), the security boundary in
[security](security.md), and what was checked in [validation](validation.md).

Dirty editor buffers stay in browser memory and are never written to the
interface state.

## Access and configuration

One process serves one instance. The configuration fields and their rules are
in [operations](operations.md#instance-configuration); the configuration has
no document roots, System view, protected paths, or creation policies.

Local mode uses `http`, and its base URL names the loopback listener. Every
request must carry a `Host` equal to the host and port of `baseUrl`, so
`localhost` or a different port is refused. HTTP port 80 and HTTPS port 443 may
be omitted from `baseUrl`; an explicit default port is accepted and stored
without that port in `Origin`, `Host`, and redirects. Every other port remains
part of the address.

Remote mode uses `https`, and its loopback connection is proxy transport only.
Remote mode needs an HTTPS reverse proxy on the same host. Each request must
come from `trustedProxyPeer` and carry `X-Forwarded-Proto: https`, an
`X-Forwarded-Host` equal to the host of `baseUrl`, and the same value in
`Host`; any other request gets `403`, including a direct loopback request from
a browser on the server. `X-Forwarded-For` is read only from that peer, and its
last address is the client address. See
[remote access](operations.md#remote-access-behind-an-https-proxy).

In both modes, a request that repeats `Host`, `Origin`, `X-Forwarded-Proto`,
`X-Forwarded-Host`, or `X-Forwarded-For` is refused. Every `POST`, `PUT`,
`PATCH`, and `DELETE` needs the exact origin of `baseUrl`, and any `Origin`
header that is present must match it. Uvicorn does not interpret forwarded
headers; the instance decides.

Cookies are `HttpOnly`, `SameSite=Strict`, scoped to the base path, and have
no `Domain`. `Secure` is always set in remote mode and is omitted in local mode.
A forwarded protocol cannot change that.

Every route lives under the base path. With a base path other than `/`, the
address without its final `/` answers `401` with
`{"error":"authentication_required"}`, or `404` with
`{"error":"not_available"}` when a session is open; it does not redirect.

A version 1 configuration (the root-based model) is refused with a message
that points to the state migration below. A version 2 file that still contains
`roots`, `system`, `protectedPaths`, or `creationPolicies` is refused.

## Releases and the service

The application is shared at `/opt/hopper-files`. Immutable releases live in
`/opt/hopper-files/releases/<release-id>`, and the `current` link selects the
active release atomically. Each release records its full source tree ID, the
SHA-256 of `requirements.lock`, hashes of all source files, an installation
record, and a hash-locked pip report. Installed source and dependencies are
owned by root and read-only; the instance process cannot modify the release or
the selector.

A release archive is built from a full Git tree object, not from the working
directory; see [development](development.md#release-archive). The archive
manifest identifies the source tree and the lock. The installer requires the
administrator to pass the archive's SHA-256. Dependency installation uses the
official `https://pypi.org/simple` index, `--require-hashes`, and
`--only-binary=:all:`; every reported wheel URL must be on
`files.pythonhosted.org`, and its digest must appear in the lock. On Debian,
`pip` comes from the signed distribution package `python3-pip`; the product
never downloads a pip bootstrap wheel.

The administrator creates the service account and writes the instance
configuration. Registration checks the configuration, the state path, the
state format, the password runtime, and that the account can read and traverse
`/`, as the configured account with its existing group memberships. It may
initialize state for a first registration. A failure reports the configured
path and the required access; no owner, mode, or group is changed to make
validation pass. Registration writes one systemd unit and a root-owned
registry record, then enables and starts the unit. The unit restricts no path:
the Linux permissions of the account decide every access (see
[systemd unit](#systemd-unit)).

Applying an edited configuration rewrites the unit and restarts the instance;
it does not edit the configuration or migrate state. An update installs and
validates a new immutable release before switching `current`. It restarts each
registered instance, so the process imports Python code and dependencies from
the selected release. Startup logs include the release ID, the source tree,
and the resolved runtime module path. A restart that `systemctl` reports as
failed restores the prior selector and restarts the instances with it. The
commands report success once systemd starts a service; a process that exits a
few seconds later is not detected (see
[operations](operations.md#recovery)). The failed candidate stays
installed only if its release and file hashes passed; the same archive can be
retried without replacing its bytes. A rollback rechecks all installed file
hashes and switches only to an installed release whose dependency report still
matches its lock.

Removing an instance disables its service and removes only its unit and
registry record. It preserves the configuration, the state, and every
document. Uninstalling removes all registered units, both commands, the release
code, and the lifecycle records; configuration, state, and documents stay. No
command deletes state. Update and rollback never convert state; only the
explicit procedure below does.

### Migration from the root-based model

This applies only to instances created by pre-release builds with configured
roots; [operations](operations.md#appendix-instances-created-by-pre-release-builds)
has the commands. Such an instance keeps version 1 interface state, version 1
trash metadata, and a version 1 configuration. The current release refuses to
serve it and never initializes empty state over it. The administrator moves
every instance registered under the shared selector with one command, run from
the new release, after reviewing the summary it prints.

Both `migrate-release` and `return-release` show the instances, accounts, and
configuration and state paths, and ask for confirmation (`--yes` records a
confirmation already given). Before stopping anything, each instance is
checked as its service account: the procedure refuses and names the pending
journals when any store holds an unfinished operation journal, trash journal,
interface-state journal, attachment-move journal, or buffer-move command.
Recover those under the release that created them and repeat.

`migrate-release` installs the identified release, stops every registered
instance, and converts each state as its service account. `(rootId, path)`
records become absolute paths under the saved version 1 configuration, with the
System view mapped from its base. Records whose root has no mapping, such as a
retired one, stay as `orphans` in the interface state; trash entries that
cannot be mapped keep version 1 metadata and stay quarantined. Records that map
to the same path merge (favorite if either is, label union in the fixed label
order, the document-root record's emoji and identity hints). Terminal operation
results are converted or kept terminal; search cursors, the derived note index,
and buffer registrations are discarded. The installer then saves the version 1
configuration beside the original as `NAME.v1-before-single-base.json` with the
same owner, group, and mode, writes the version 2 configuration, keeps the
previous unit under `/var/lib/hopper-files/migration/`, writes the new unit,
switches the selector, and starts the instances. If any instance cannot be
converted, the instances already converted return and the previous release
stays selected.

`return-release` stops the instances, converts each version 2 state back under
the saved configuration, restores the version 1 configuration and unit, and
switches the selector back before starting them. A path inside a former
document root maps to that root; a path outside every root maps to the former
System view only where that release exposed it, that is, outside its protected
names and paths; anything else is kept in the return report and restored by
the next migration. Trash the previous release cannot represent stays in
trash, quarantined by that release. Each run writes a report and the replaced
documents under `STATE/migration/`; both directions can be repeated safely.

## Password verifier

Version 1 is a fixed 112-byte record:

| Offset | Bytes | Value |
| --- | ---: | --- |
| 0 | 4 | `HFCR` |
| 4 | 1 | version `1` |
| 5 | 1 | algorithm length `6` |
| 6 | 6 | `scrypt` |
| 12 | 16 | `n`, `r`, `p`, `dklen` as big-endian uint32: `131072`, `8`, `1`, `64` |
| 28 | 4 | credential epoch, big-endian uint32, starting at 1 |
| 32 | 16 | random salt |
| 48 | 64 | scrypt digest |

`hashlib.scrypt` uses `maxmem=268435456`. Passwords are exact UTF-8, are not
normalized, and are rejected before the KDF above 1024 bytes. A missing or
malformed record uses one dummy verification with the same parameters. Wrong,
missing, and malformed credentials return the same external authentication
failure. At most one password KDF runs per instance; a busy verifier returns
`429` with `Retry-After: 1`.

## Login limits

Every parsed login attempt consumes one token from two persistent buckets
before any KDF:

| Bucket | Capacity | Refill |
| --- | ---: | --- |
| Socket or trusted-proxy network origin | 5 | 1 token each 60 seconds |
| Whole instance | 20 | 1 token each 15 seconds |

An empty bucket returns `429` and `{"error":"too_many_requests"}`.
`Retry-After` is the whole seconds until both buckets can admit another
attempt. Success does not refill either bucket. The files keep absolute time,
so a restart continues the same window and does not create a permanent lockout.
`X-Forwarded-For` is ignored unless the peer is the configured proxy; in remote
mode without it, every client shares the proxy's bucket. Login and logout
control bodies are limited to 64 KiB and are counted as the server reads the
stream; an absent or inaccurate `Content-Length` does not extend the limit.

## Filesystem base and addresses

Every instance navigates the filesystem from `/` with the Linux permissions of
its service account. There are no configured roots, no System view, and no
protected namespaces of its own: what the account can read is listed and
opens, and what it can write can be changed. An authenticated session therefore
has the file access of a terminal of that account; the login is the only
application barrier in front of it.

An address is the absolute canonical path of an object. The API transports it
as the fixed base identifier `fs` plus the path relative to `/` (`rootId=fs`,
`path=home/alice/note.md`); every API response, metadata record, cursor, and
version identifies the object by that absolute path. Paths use `/` as the only
separator; `..`, `.`, empty components, a trailing separator, NUL, backslash,
any percent-encoding, and invalid UTF-8 are rejected. Resolution walks
directory descriptors from `/` and rejects an address whose resolution meets a
symbolic link. A client reaches a link's destination through the real path
that the listing reports. A name that is replaced by a symlink during the walk
is denied; the open descriptor, not a second path lookup, is what gets read.
Another process can still change the bytes of a file that was already opened.

`GET api/list` returns every entry the account obtains when it lists the
directory, including dotfiles, `/proc`, `/sys`, `/dev`, and leftover
`.hopper-stage-*` names, and reports whether the open directory is writable.
Each entry has `name`, `type` (`directory`, `file`, `link`, or `other`),
`size`, `openable`, `addressable`, `writable`, `links`, and, when it does not
open, a `reason` (`permission`, `broken`, `special`, `invalid_name`, or
`unavailable`). A directory opens only with read and search permission. A
symbolic link reports its literal `target`, its real path as `resolved`, and
`resolvedType`; it is never expanded, and a broken link or a link to a special
object does not open. Devices, sockets, and FIFOs are listed and never opened.
A regular file with several links is listed and readable, but not editable.
Regular files in `/proc` and `/sys` can be read within the text limit. A name
that is not valid UTF-8 is listed with a lossless display name, where each
invalid byte appears as `\xHH` and each literal backslash as `\\`; it has no
address and accepts no operation.

Create, rename, move, copy, upload, extract, save, and move to trash succeed
wherever the account has the Linux permission the action needs, checked when
the operation runs; a refusal changes nothing on disk and is reported as a
permission error. New files and directories are created with the modes a
terminal would request (`0666` and `0777`) and receive the result of the
process umask (`UMask=0002` in the unit), a set-group-ID parent, and a default
POSIX ACL, exactly as a terminal of the same account would. Upload, ordinary
copy, and extraction import no owner, group, set-ID bits, ACL, or mode from
their input. A symbolic link itself is never renamed, moved, or moved to the
trash.

`GET login` issues one pre-authentication nonce. `POST login` requires that
nonce, the exact `Origin`, and the expected `Host`. Every attempt consumes its
nonce, so the login page asks `GET login` for a fresh one after a failed
attempt. Authenticated mutations also require the session and its CSRF token.
The login page links to `GET password`, a page that changes the password
without a session: `POST password` takes a fresh nonce, the current password,
and the new password twice, spends the same login limits, and refuses a
mismatched confirmation before any KDF. Logout, expiry, and a password change
from that page or from `set-password` invalidate sessions and CSRF tokens for
that instance only. `GET app` without a valid session answers `401` with the
login page. When any API call answers `401`, the interface stops its periodic
buffer synchronization and offers to sign in again instead of showing an
empty folder or a generic error.

## Navigation, search, and metadata

The authenticated `/app` shell shows one tree whose first node is `/`,
collapsed on first access. The chevron expands or collapses only its node;
activating a directory name navigates to it and expands it without collapsing
it. Files appear in the tree. A link shows as `name → target`; activating it
opens its real path. Entries that do not open are dimmed with the reason in
their tooltip. Favorites sit above that tree, and each favorite directory opens
as its own tree with the same rules. Which favorite branches were open and
whether `Arquivos & Pastas` was collapsed stay in the browser, per origin; the
favorites themselves remain in the server's interface state. The breadcrumb
shows the absolute path. The local `aqui` filter examines only the displayed
immediate files and directories, compares an NFC-normalized case-insensitive
name substring, reads no file content, and clears when the directory changes or
the filter is cleared. It does not call the search route.

`GET api/list` reports, for each entry, `modifiedAt` and `createdAt` as UTC
`YYYY-MM-DDTHH:MM:SSZ`. The creation time is the birth time that `statx(2)`
returns; it is `null` when the filesystem does not record one. The listing
shows both dates in the instance time zone and measures each visible
directory with `GET api/dir-size`: at most two measurements run at once,
outside the request loop, each within a 3-second budget. A measurement never
follows a link or enters a pseudo filesystem; a budget stop, an unreadable
subdirectory, or a race returns the partial total with `complete: false`, and
the interface marks it with a trailing `+`.

`GET api/search` accepts `mode=name|text`, a `scope` that is the absolute
address of a directory the account can open (the interface uses the open
directory and shows it), a query `q`, and optional `limit` and `cursor`. It
descends from the scope, never enters `/proc`, `/sys`, `/dev`, or another
pseudo filesystem, and never follows a symbolic link. Name mode compares
basenames of files, directories, and links; a matching link is returned as the
link itself. Text mode reads only regular files up to 5 MiB that have valid
UTF-8 and no NUL, including files with several links. Both modes compare
literal substrings using `NFC(casefold(NFC(value)))`; accents remain
significant. Results sort by the UTF-8 bytes of their absolute paths and carry
`address`; text mode returns the first matching line and a literal snippet. The
instance's own state and trash are searchable like any other readable
directory. A directory the account cannot list is counted as
`unreadable_directory` and does not make the result incomplete.

The initial traversal is limited to 10 seconds, 10,000 results, and 64 MiB of
serialized cursor state. A cursor is HMAC-authenticated for one instance and
binds the normalized query, mode, scope, page size, snapshot, and next offset.
It expires after 15 minutes. Changed parameters return `400`, expiry returns
`410`, and a change observed while materializing, or in the result objects and
their parent directories when a later page is served, returns `409`; the
client must start a fresh search. Responses report categorized omissions and
aggregate exclusions. These bounds do not claim a universal snapshot against
outside writers.

`GET api/tags` derives Markdown tags from the monitored folders: every
regular `.md` file under a folder the account marked, under the same traversal
limits, excluding the instance's internal trash. With no monitored folder the
list is empty. Below a monitored folder, a subdirectory whose name starts with
`.` and the directories `/tmp` and `/var/tmp` are left out and reported as the
`hidden_directory` and `excluded_path` exclusions; such a directory is read only
from a monitored folder at or below it. A note file whose own name starts with
`.` is read, and so is a note the account can read but not write. A marked
folder that is gone or cannot be listed is reported as `unavailable_folder`
and an unreadable note as `unreadable_file`. The index ignores fenced or inline
code, link destinations, URLs, and hexadecimal colors and does not rewrite
Markdown. A budget stop, read error, or race makes the result incomplete;
invalid or oversized notes are reported as omissions. What was derived from
each note is kept in the private derived index described in
[State layout](#state-layout) and reused only while the note's identity and
version are unchanged. Tags listed in the instance's `ignoredTags` preference
are left out of the list, counts, and per-tag lookup until they are restored in
Settings; the notes are unchanged. Tag and search requests run outside the
loop that serves requests, so a listing does not wait for them.

`GET api/tag-folders` returns `{"folders": [{"path", "available"}]}`, the
monitored folders sorted by address, each with whether it is currently a
directory the account can list. `POST api/tag-folders` takes
`{"path": ABSOLUTE_ADDRESS, "monitored": true|false}` with the CSRF token and
returns the same shape. Marking needs a directory the account can list,
reached without a symbolic link; otherwise it returns `403 not_directory` and
writes nothing. A 65th folder returns `413 limit_exceeded`. A repeated mark or
unmark changes nothing. The interface marks a folder from its context menu or
its information panel and lists the folders in Settings. A mark does not
follow a rename or a move of its folder.

Favorites, label IDs and names, directory emoji, tabs, theme, ordering, and
density are stored in the per-instance interface state by absolute path. The
browser submits a complete document with `baseRevision` to `PUT api/state`; it
omits `stateRevision` from the proposal and may omit `orphans`, which only the
server writes. A stale revision returns `409`. The page preserves local changes
and offers an explicit reload or merge. Tabs store only the path and mode,
never document contents. The interface includes a trash view backed by the
trash routes below.

### Changes made outside the application

While its browser tab is visible, the interface keeps one `POST api/changes`
request open with the CSRF token and the body
`{"rootId": "fs", "paths": [...], "epoch": ..., "seq": ...}`. `paths` names the
directories on screen: the open folder, the open branches of the `/` tree and
of favorite trees, and the folders of open documents and previews, at most 256.
The server watches only those directories through Linux inotify, validating
each one as `GET api/list` does, and answers about 200 ms after the first
notice in one of them, or with an empty `changes` list after 20 seconds. The
response is `{"supported", "epoch", "seq", "resync", "changes", "rejected"}`;
each change is `{"path", "name"}`, with `name` set to `null` when the directory
itself changed, the name is not UTF-8, or more than 64 names changed there. The
interface asks again at once with the returned `epoch` and `seq`, and cancels
and repeats the request when the set of directories changes.

The first request, a new server process (another `epoch`), a `seq` the server
no longer keeps, or an inotify queue overflow answers at once with
`"resync": true`; the interface then reads again everything it shows. A
directory is released after 60 seconds without being named. One instance
watches at most 4,096 directories and keeps at most 8,192 notices; a directory
it cannot watch is listed in `rejected` and is read again when the tab returns
to view. When inotify is not available, the answer is `"supported": false` and
the interface stops asking. Once notices are in use, `GET api/list` and
`GET api/file` start watching the directory they read before reading it.

On a notice, the listing and the tree read the folder again and redraw only
when entries differ, keeping scroll, focus, and marked items; a redraw waits
for an open context menu, dialog, drag, or pointer press. A folder that starts
being watched is read once more after its first answer. When the open folder
is deleted or can no longer be opened, the listing is replaced by a notice and
the interface also watches the folder's ancestors; when the folder exists
again, the listing shows its content. An open text document
without local edits takes the new content and shows `Arquivo atualizado fora
do app`; with unsaved edits it keeps the buffer and shows `Mudou fora do app`
with the conflict choices; the user's own save produces no notice. A removed or
renamed document shows `Removido fora do app` and stays open. Image and PDF
previews reload. While the tab is hidden, the interface requests nothing; on
return it reads again the open folder, the open branches, and the open
documents. After a network or server error it retries after 1, 2, 4, and up to
30 seconds; after `401` it stops. Changes that another machine makes on a
network filesystem produce no inotify notice and appear only when the view is
read again.

## Base file operations

`GET api/list` returns the entries described above and an opaque listing
version. Reads, downloads, and copies never follow a link; a directory copy,
ZIP, or move that contains a link or a special object is refused. `GET
api/raw` streams a regular file as an attachment with a UTF-8-safe filename.
It does not buffer the complete file in application memory. A directory cannot
be downloaded as one file.

`POST api/files/token` issues an instance-bound operation token. The first
mutation binds that token to its action payload and retains per-item results in
the private `operations/` state directory. The authenticated status route
`GET api/files/{token}` remains available for seven days after token expiry.
Expired tokens cannot start or resume a mutation. The store allows at most 64
active operations per instance; journal and per-token upload lock files are
`0600` in a `0700` directory. Terminal results are retained through seven days
after token expiry.

`POST api/files` supports exclusive creation of empty files, directories, and
Markdown notes, ordinary file and recursive directory copies, ZIP creation,
and ZIP extraction. It requires an operation token and the session CSRF token.
The JSON request body is streamed and limited to 1 MiB.
Destinations are never overwritten. Operation intents are synced before
publication; a replay skips committed items, cleans only matching private
staging, and reports committed, uncommitted, or indeterminate results. Known
conflicts with no commit return `409`; known limits with no commit return
`413`. HTTP `207` is reserved for partial, indeterminate, or heterogeneous
per-item results; a partial commit is not reported as a total failure.

`POST api/files/upload` accepts a bounded JSON manifest in a header and a
concatenated request body. The manifest binds exact names, byte counts, and
SHA-256 digests before body bytes are read. Upload is streamed through
exclusive temporary files and publishes only after every declared file has
the expected size and digest. A failed later item leaves earlier commits
visible in the result. Requests allow at most 20 files, 200 MiB per file, and
400 MiB total.

The upload holds a private per-token lock across the request stream and
publication. A concurrent replay receives a conflict, and background recovery
skips a token while its request owns that lock.

## Trash

Trash is private to the instance under `stateDirectory/trash/<uuid>/`. Each
entry has a `payload`, a version 2 `meta.json`, and `meta.seal`. The metadata
fields are `version`, `id`, `sourcePath` (the absolute address of the deleted
item), `deletedAt`, `kind`, `size`, `reason`, and `uiMetadata`. `deletedAt` is
a server UTC timestamp. The seal binds that document and the payload digest to
the instance signing secret. Invalid, incomplete, or unsealed metadata is
quarantined: it is listed as quarantined, and it is not restored or purged.
Version 1 metadata that the migration could not map stays in trash and is
listed as quarantined with `legacy: true`.

`GET api/trash` requires a session. `POST api/trash` also requires the session
CSRF token. `{"action":"delete","source":{"rootId","path"}}` moves one file or
directory into trash. `{"action":"restore","id"}` restores it to the original
path. An occupied path or active UI metadata at the destination returns `409`
and leaves the trash payload in place. `alternativePath` is an explicit
address (transport form) chosen by the user. There is no request for early
permanent deletion.

A same-filesystem delete or restore renames the inode and keeps its metadata,
including set-ID bits. A cross-filesystem operation copies to an exclusive
stage, checks bytes and the supported access policy, syncs, publishes, and
only then removes the previous copy. The preserved policy is UID, GID,
permission bits without set-ID, the POSIX access ACL, and a directory's POSIX
default ACL. Set-ID bits, capabilities, and any other extended or security
attribute are refused before publication. The destination's new-object mode is
not applied to a restored item. A regular file with several links that is
moved to trash across filesystems is copied with the same preserved metadata;
only the addressed name is removed, the other names keep the original inode,
and the response reports `separatedLinks`. An item on another filesystem whose
owner, group, set-ID bits, capabilities, or extended metadata the account
cannot reproduce cannot move to trash, so it cannot be deleted through Hopper
Files. Failure before publication removes only the stage and leaves the
source. Failure after publication can leave two copies. Before a restore
rename, the journal records the kernel file handle of the payload or
destination stage. Recovery adopts a publication only when that same handle
and the expected content digest still match. Device and inode values are also
checked as hints, not identity on their own; inode reuse cannot transfer marks
to a replacement. If the filesystem cannot provide the required file handle,
restore returns `422 unsupported_metadata` before publication and the trash
item remains available. Another process can still change a file that ignores
the instance lock; that residual race is reported instead of being treated as
impossible.

Favorites, labels, and directory emoji for the item and its marked descendants
move with the same interface-state lock. The active document loses those
records, the trash metadata keeps them relative to the payload, and
`stateRevision` increases by one. A later `PUT api/state` with the old revision
returns `409` and cannot put the marks back. A new file created at the old path
does not inherit them, including when the inode number is reused. Restore
writes the marks at the original or explicit alternative address before
removing their trash copy. If that published object is replaced before
recovery finishes, the marks stay with the trash item and are not attached to
the replacement.

Moving an item to trash needs the permissions that removing it from its
directory needs. A symbolic link or a special object is not moved to trash,
and neither is a directory that contains a special object or another mount.

Valid entries are kept for at least 30 days, measured from the protected
`deletedAt`. The only permanent deletion is the `purge-trash` command, run as
the instance's account (see [operations](operations.md#trash)). It compares
the server clock with that timestamp and skips quarantined entries and entries
whose journal is still open. Nothing runs it automatically.

Operations stage new objects under unpredictable names of the form
`.hopper-stage-<32 lowercase hex>.tmp` or `.dir`, created exclusively and
removed by recovery only when the journaled name, type, and inode all match.
These names are listed like any other entry and are not reserved.

Generated names use an NFD-derived ASCII slug and the server's configured time
zone at commit time. Generated collisions try the base name and suffixes `-2`
through `-30`; exact names and original upload names never gain a suffix.
Exact names are preserved subject to the address rules. New objects receive
the terminal-equivalent result described above. A note is atomically
initialized to `# <title>\n`.

ZIP creation and extraction stream file contents; creation writes ZIP64 member
headers when a source file reaches the classic ZIP size threshold. Source sets
remain limited to 3 GiB, 10,000 entries, and depth 32. Extraction is limited to
3 GiB uncompressed, 200 MiB per file, 10,000 entries, depth 32, and an
expansion ratio of 120:1. It rejects traversal, duplicate destinations, links,
special files, and changes observed while planning or streaming. Recursive
ordinary directory copy uses the same 3 GiB, 10,000-entry, and depth-32 safety
bounds. Destination capacity is checked before publication; quota or space
exhaustion is reported per item when detected during writing.

## Managed images, references, and moves

Markdown editors accept PNG, JPEG, GIF, and WebP through the image button,
paste, or drop. SVG is not accepted as a managed inline image. The server
checks the file signature and enforces the 20 MiB image limit, and places the
image in the visible `attachments/` directory beside the note, in the note's
own directory. The editor inserts an ordinary percent-escaped relative
Markdown image destination. `GET api/images?rootId=fs&path=NOTE` lists that
note's `attachments/` directory and each image's status;
`POST api/images/upload?rootId=…&path=…` requires the session, CSRF token, and
a fresh operation token. The small pending-resolution JSON request is limited
to 64 KiB and is read incrementally. The gallery can insert an existing image
reference.

The authenticated `GET api/images/resolve` route accepts `rootId=fs`, `path`,
and one Markdown `reference`. It verifies the readable Markdown source, then
uses the image-reference resolver for percent decoding, normalization of `..`
against the note's directory, and symbolic-link rejection. A local result
contains only `{resolved: true, rootId, path}`; an external destination
returns `{resolved: false}` and is never fetched or returned as a URL. The
interface loads a resolved image through the same-origin authenticated
`GET api/preview`, which enforces the preview media and size limits.

An upload remains pending until the note save confirms its reference, or the
user explicitly resolves the pending state from the gallery. Cancellation,
failed save, or a lost connection does not make it an automatic deletion
candidate. A confirmed save checks only managed images referenced by that note
before saving and removed by this save. The service checks the note corpus
(the `.md` files in directories the account can write, discovered from `/`,
excluding the internal trash) to prove whether another reference remains. A
reference from a note outside the corpus is not counted. It does not sweep
unreferenced files generally. A complete scan with no remaining reference may
move the candidate to trash; a traversal stop, read or permission error, race,
or scanner limit preserves it. A note whose references cannot be parsed keeps
every candidate whose file name appears in its text. The scanner supports
Markdown larger than the editor's 5 MiB limit and enforces explicit
per-document byte, line, and reference-count bounds. Parsed destinations are
kept in the derived note index and reused only while the note is unchanged.

`POST api/files` accepts journaled `move` and `rename` actions with the normal
operation-token and exclusive-destination rules. The source must be a regular
file or a directory. A file over 3 GiB is refused. A directory is refused when
it has more than 3 GiB, 10,000 entries, or depth 32, or when it contains a
symbolic link, a special file, or an entry the account cannot read. These
limits hold even for a rename within one filesystem. The navigation interface offers these actions where the
open directory is writable and calls this API. Visibility in the interface is
not an authorization decision: the server still validates the selected source
and destination, and the kernel checks Linux access at the mutation. Before
moving a note, image, or directory, the server builds one plan. It maps moved
note and image identities, finds affected Markdown in the note corpus, and
stages only the URL-destination changes needed to preserve each image's
absolute path. Relative references may contain `..`; absolute, double-encoded,
and symbolic-link destinations are rejected. A note whose references cannot be
parsed and that names a moved file blocks the move with `409`, as does an
incomplete corpus scan. Ordinary links, wiki links, and backlinks are not part
of this scan. The interface checks of copy, move, and rename are recorded in
[validation](validation.md); physical devices remain open.

The move journal binds the plan to the source identity and digest and to the
strong versions and digests of rewritten notes. Affected open buffers in
connected sessions must acknowledge the same saved version and be clean; the
service holds them during publication and reloads them afterward. A dirty,
changed, or unreachable buffer blocks the move before publication. The route
runs the move outside the ASGI event loop so the buffer-acknowledgement route
remains available while the operation waits. Each rewrite is written, synced,
and checked on its destination filesystem before the move is published. If a
directory move will create the rewritten note's final parent, its stage is
placed in the nearest safe existing ancestor on that filesystem. At
publication, the service compares the current target with its recorded kernel
file handle and digest, then atomically replaces only that object. For a
cross-filesystem directory restore, the move journal also records handles for
rewritten Markdown files in the exclusive restored tree before publication so
internal note rewrites remain bound to the files copied by that restore.
When source and destination are on one filesystem, whichever filesystem holds
the state directory, the move is a single `renameat2(RENAME_NOREPLACE)`
recorded in a trash journal: the inode, its other links, and all its metadata,
including set-ID bits and extended attributes, stay unchanged, and nothing is
copied. If the kernel refuses that rename across separate mounts of one
filesystem, the move falls back to the path below before anything is
published. Only a move between filesystems goes through the trash and copies.
It preserves UID, GID, permission bits without set-ID, access ACL, and
directory default ACL; unsupported metadata is refused before publication. The
other names of a multiply linked file keep the original inode, and the
committed item reports `separatedLinks`. The interface shows that count after a
move and after moving to trash. Recovery adopts a rewritten note only when its
recorded handle and content match. An ambiguous occupant keeps both copies and
the journal for diagnosis, without a blind rollback. Related favorites, labels,
and directory emoji move with the same identity-scoped state revision.

In the trash view, a Markdown note can list removed images whose source paths
match its missing references. `GET api/trash/related-images?id=…` returns only
verified candidates. Selecting images submits them with the note through
`POST api/trash` using `relatedImageIds`; a conflict leaves every
not-yet-restored payload available and never overwrites an existing
destination. Otherwise the trash view restores one item at a time.

## Editor and preview

`GET api/file?rootId=fs&path=…` reads a regular file no larger than 5 MiB and
returns strict UTF-8 content, an HMAC-authenticated version bound to the
instance, absolute path, SHA-256, and observed device, inode, content-time,
change-time, and size, plus `editable` and `readOnlyReason`. A file is editable
only when the account can write it and its directory, owns it and belongs to
its group, it has exactly one link, no set-ID bit, and no extended attribute
other than a POSIX access ACL; the save path enforces the same rule and refuses
with `403 not_editable` and the same reason. Invalid UTF-8 is an error; the
interface offers the original file as a download.

A Markdown document has one segmented control with three views of the same
buffer. `Formatado` (the view a `.md` opens in) shows the sanitized rendered
HTML of the current buffer, including tables, images, links, and code blocks,
and accepts no typing. `Markdown` shows the source in CodeMirror 6 with every
marker visible and stable styling; it is the only view that accepts typing and
the Markdown commands. `Limpo` shows the clean text and accepts no typing.
Other text has no view control and one plain editor. A file that is not
editable shows `Somente leitura` with its reason, hides the formatting commands
and Save, accepts no typing, and keeps copy and download. Clean-copy and
plain-text recomposition follow the normative fixtures in the product
specification.

`PUT api/file` requires the session, exact Origin/Host checks, CSRF token, and
`baseVersion`. It serializes cooperating sessions with a private per-file lock,
checks both file and directory access, rereads immediately before replacing,
and returns the exact persisted content and `savedVersion`. Missing versions
return `428`; stale versions return `409`. Publication uses an exclusive
same-filesystem temporary, syncs content, and preserves UID, GID, permission
bits, and POSIX access ACL. Other extended/security metadata and set-ID files
are refused. A post-replacement failure returns `503 indeterminate` after a
best-effort reread; it never rolls back blindly. A lock-ignoring outside writer
can still race after the final check.

The browser holds dirty text only in memory, keeps later typing after an
in-flight save, orders baseline updates, and guards window exit. Listing refresh
and switching document tabs retain each in-memory buffer and its selection.
Nothing writes editor buffers into persistent interface state.

`GET api/preview` streams only signature-identified PNG, JPEG, GIF, WebP, and
PDF files, with 20 MiB and 100 MiB limits respectively. HTML and SVG can be
opened as text but are not served as active previews. Markdown preview disables
raw HTML and sanitizes generated markup. PDF.js renders pages to canvas with
local worker and data files and disables dynamic code evaluation. Office
conversion is not included. The specification remains the contract for
behavior beyond these implemented routes.

## systemd unit

`hopper_files.systemd_unit.render_service_unit` writes a unit that runs as the
registered account with `NoNewPrivileges=yes`, empty `CapabilityBoundingSet=`
and `AmbientCapabilities=`, and `UMask=0002`. It sets no `ProtectSystem`,
`ReadWritePaths`, `ReadOnlyPaths`, `InaccessiblePaths`, or `PrivateTmp`, so
`/tmp` and `/var/tmp` are the same as in the account's terminal and Linux
permissions decide every file access. Releases, dependencies, the selector,
and administrative configuration are protected by their root ownership and
modes, not by the unit. `MemoryMax=512M` is large enough for one scrypt
`maxmem` of 256 MiB. Concurrency is refused by the instance lock, not by
running a second KDF. The environment sets `PATH`, `LANG`, and
`HOPPER_FILES_INSTANCE`, and unsets SSH-agent and shell variables. The unit
belongs to `multi-user.target`, so it starts at boot once enabled. It sets no
`Restart=`: after a crash the service stays stopped until an administrator
restarts it. Rendering the text does not call `systemctl`; release validation
inspects the effective unit.

The service refuses extra arguments, a non-loopback bind, a missing account,
and a process that does not run as `serviceAccount`. It does not create Linux
accounts, change document permissions, or invoke `sudo`.

## State layout

The state directory belongs to one instance. Its directories are `0700` and
its files `0600`. The first registration, or `set-password`, creates:

| Path | Content |
| --- | --- |
| `instance.json` | Binds the directory to one `instanceId`; another instance refuses it. |
| `signing-secret` | Random key that signs sessions, operation tokens, file versions, search cursors, and trash metadata. |
| `dummy-salt` | Salt of the dummy password check. |
| `kdf.lock` | Lets one password check run at a time. |
| `sessions/`, `nonces/` | Sessions and login nonces. |
| `ui-state/ui-state.json` | The interface state. |
| `ui-state.lock`, `ui-state-initialized.json` | The lock of the interface state and the record of its first initialization. |
| `journals/` | Journals of interface-state changes (`ui-state/`) and trash changes (`trash/`). |
| `operations/` | File-operation tokens, journals, and results. |
| `trash/` | Trash entries (see [Trash](#trash)). |
| `cache/` | Search cursor snapshots. |
| `indexes/` | The derived note index, `notes.json`, and its lock, `notes.lock`. |
| `attachments/` | Pending image uploads (`pending.json`), image-move journals (`moves/`), and connected editor buffers (`buffers.json`). |

Other entries appear later: `credential`, the password verifier, after
`set-password`; `file-locks/`, with the editor's per-file locks, at each start;
`limiter.json` and `limiter.lock` at the first login attempt; `tag-folders.json`
when a folder is first marked for tags; and `migration/` only after a
migration. Every one of these stores belongs only to the bound instance; an
instance cannot read or write another instance's state.

`tag-folders.json` is `{"version": 1, "folders": [...]}` with at most 64
absolute addresses of monitored folders. It lives outside `ui-state.json`, so a
build that predates it ignores it; an invalid document fails closed instead of
reading as empty. The editor's lock files under `file-locks/` are named by a
SHA-256 of the instance and the address.

`ui-state/ui-state.json` is a schema-version-2 document. Its `stateRevision`
begins at zero and is independent of `version`; each durable state change
increases the revision once. It carries the seven fixed label IDs and colors,
items `{path,labelIds,favorite,emoji,inode,device}` identified by their
absolute path, tabs `{path,mode}` without document content (tabs may repeat),
`orphans` (version 1 records the migration could not map, kept only for
reporting and rollback and never shown as marks), and theme, ordering, and
density preferences, plus an optional `ignoredTags` list of normalized tags,
omitted when empty, and an optional `sidebarWidth` (200 to 420 pixels). Device
and inode fields are only reconciliation hints and never authorize or identify
an item by themselves. A version 1 document is read only by the migration; the
release refuses to serve it.

Authenticated `GET api/state` returns the complete document. `PUT api/state`
accepts a complete replacement plus `baseRevision`, but does not accept a
client-selected `stateRevision`; the server creates the next value. A missing
precondition receives `428`. A stale precondition receives `409` with the
current revision and no state from another instance. The caller must preserve
its local work and explicitly reload or resolve it; the server never overwrites
the current document on its behalf. The endpoint requires the same session,
Origin, Host, and CSRF protections as other authenticated mutations.

First initialization records a durable private intent, publishes the exact
revision-zero document, and then publishes the initialization witness. Recovery
can finish that recorded initialization only when the on-disk document is
exactly that default document. If no document was published, it removes only
the recorded first-initialization evidence and its own pre-publication
temporary files before a later initialization attempt. If the initialization
intent itself was not published, recovery likewise removes only its own
strictly named regular temporary files. Once the witness exists, a missing
document is a failure, never an implicit reset.

Each replacement holds the per-instance state lock across re-reading the base
revision, schema validation, durable journal intent, and atomic publication of
the full document. The file and its containing directory are synced before the
new revision is returned. An unfinished intent stays in `journals/ui-state/`
until recovery sees either exactly the old document or exactly the target
document. It then removes the terminal intent without replaying a write.
Recovery removes only regular, strictly named Hopper Files temporaries left
before a journal intent was published; any other journal entry, truncated
record, malformed state, or indeterminate state fails closed and preserves the
existing files for diagnosis. Move, delete, restore, and recovery code use
this same state mutation protocol.

`indexes/notes.json` is the derived note index: for each note of the corpus or
of a monitored folder it keeps the identity and version tuple observed, its
tags, and its raw Markdown image destinations, or why they could not be
derived. An entry is reused only while device, inode, size, modification time,
and change time are unchanged. A complete corpus traversal drops the entries of
notes it no longer finds, except notes still listed in read-only directories,
which only the tag index reads; a complete tag traversal drops only entries in
directories it listed. The index never authorizes an access and is rebuilt on
demand; it is kept across restarts and discarded by a migration. On a large
set of notes, the first derivation can stop at the 10-second budget: tag
results are then incomplete, collection keeps the candidate images, and moves
and renames are refused with `409` until later requests complete the index, which keeps its progress between requests. `migration/` holds the
migration record, reports, and the documents each conversion replaced.
