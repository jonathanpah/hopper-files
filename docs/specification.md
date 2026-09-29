# Hopper Files Product Specification

**Document status:** normative product specification for version 1. This file
defines requirements; implementation and validation outcomes are recorded
separately in [the public validation record](validation.md). The contents of
this specification do not by themselves claim that any requirement has passed.

Requirement identifiers are permanent. Later revisions may clarify a
requirement, but must not reuse an identifier for a different rule. A retired
identifier stays listed in [section 15](#15-retired-identifiers) with its
replacement and is never reused. The words **MUST**, **MUST NOT**, **SHOULD**,
and **MAY** express requirement strength.

## Contents

- [1. Product definition](#1-product-definition)
  - [1.1 Included capabilities](#11-included-capabilities)
  - [1.2 Explicit exclusions](#12-explicit-exclusions)
- [2. Installation, runtime, and lifecycle](#2-installation-runtime-and-lifecycle)
  - [2.1 Shared installation and per-account services](#21-shared-installation-and-per-account-services)
  - [2.2 Lifecycle](#22-lifecycle)
- [3. Instances and the filesystem](#3-instances-and-the-filesystem)
  - [3.1 Filesystem base and visibility](#31-filesystem-base-and-visibility)
  - [3.2 Writing, notes, and traversal](#32-writing-notes-and-traversal)
- [4. Access, authentication, and sessions](#4-access-authentication-and-sessions)
  - [4.1 Explicit access modes](#41-explicit-access-modes)
  - [4.2 Authentication](#42-authentication)
- [5. HTTP API and path model](#5-http-api-and-path-model)
- [6. Navigation and filesystem operations](#6-navigation-and-filesystem-operations)
- [7. Names and creation](#7-names-and-creation)
- [8. User interface and UI metadata](#8-user-interface-and-ui-metadata)
  - [8.1 Layout and responsive behavior](#81-layout-and-responsive-behavior)
  - [8.2 Tags, labels, favorites, and state](#82-tags-labels-favorites-and-state)
- [9. Markdown, editor, viewing, and saving](#9-markdown-editor-viewing-and-saving)
  - [9.1 Markdown and editor behavior](#91-markdown-and-editor-behavior)
  - [9.2 Save protocol and dirty state](#92-save-protocol-and-dirty-state)
- [10. Images and portable references](#10-images-and-portable-references)
- [11. Trash and retention](#11-trash-and-retention)
- [12. Resource limits and security](#12-resource-limits-and-security)
  - [12.1 Initial limits](#121-initial-limits)
  - [12.2 Server-side enforcement](#122-server-side-enforcement)
- [13. Acceptance and validation contract](#13-acceptance-and-validation-contract)
  - [13.1 Installation and isolation](#131-installation-and-isolation)
  - [13.2 Filesystem navigation and paths](#132-filesystem-navigation-and-paths)
  - [13.3 File operations and limits](#133-file-operations-and-limits)
  - [13.4 Editor and concurrent saving](#134-editor-and-concurrent-saving)
  - [13.5 Images and trash](#135-images-and-trash)
  - [13.6 Rendering and interface](#136-rendering-and-interface)
- [14. Completion conditions](#14-completion-conditions)
- [15. Retired identifiers](#15-retired-identifiers)

## 1. Product definition

**HF-SCOPE-001 — Purpose.** Hopper Files is a lightweight, browser-based file
manager and Markdown editor for Linux. Version 1 targets Linux systems managed
with `systemd`; Debian 13 is the reference platform. macOS, Windows, mobile
native applications, and general platform portability are outside version 1.

**HF-SCOPE-002 — Data model.** Files and directories remain ordinary filesystem
objects that are usable without Hopper Files. Primary content is not stored in
a database. Small configuration, authentication, session, index, UI-state,
journal, and trash metadata MAY use local versioned files. No database server,
Docker runtime, or permanent Node.js server is required.

**HF-SCOPE-003 — Technology baseline.** The backend uses Python, FastAPI, and
Uvicorn. The frontend uses HTML, CSS, JavaScript, and CodeMirror. Markdown,
sanitization, and PDF-viewing libraries must be pinned and reviewed before a
release. A build tool MAY use Node.js during development; serving the product
MUST NOT require a permanent Node.js process.

**HF-SCOPE-004 — Version 1 interface language.** Public technical documentation
is written in English. The approved version 1 user interface remains in
Portuguese. Translating the interface or adding an internationalization system
is outside this version.

### 1.1 Included capabilities

**HF-SCOPE-005 — Included file operations.** The product includes navigation,
name and text search, create, open, edit, rename, move, copy, delete, upload,
download, ZIP creation and extraction, trash, restoration, and directory size
calculation on demand. It also includes tabs, favorites, labels, tags, a
desktop split view, and responsive mobile navigation.

**HF-SCOPE-006 — Included editing and viewing.** The product includes Markdown
source editing with formatted and clean views of the same buffer (HF-NAV-008),
clean copy, editor-local undo and redo, image insertion, and viewing of text,
Markdown, images, and PDF files. Office files
remain storable, organizable, downloadable, and openable by external software.

**HF-SCOPE-007 — Included local diagnostics.** The product MAY keep local
technical logs needed to diagnose its own operation. Logs MUST avoid secrets
and unnecessary personal or document content.

### 1.2 Explicit exclusions

**HF-SCOPE-008 — Excluded integrations and features.** Version 1 MUST NOT
include:

- file synchronization or a bundled synchronization service;
- AI review, AI renaming, workers, queues, or schedules;
- browser-based terminal or agent sessions;
- external alerts;
- wiki-style note links, backlinks, or link autocompletion;
- Git status indicators;
- file-hash calculation in the interface;
- a naming-conformance panel;
- global undo or redo for filesystem operations;
- integrated bug reporting or telemetry;
- internal Office conversion or viewing through an office suite.

Dependencies, buttons, background jobs, state, and service definitions for
these exclusions MUST NOT remain in the delivered product.

## 2. Installation, runtime, and lifecycle

### 2.1 Shared installation and per-account services

**HF-INST-001 — Administrative installer.** Installation and system lifecycle
operations use a terminal command run by an authorized administrator with
`sudo`. Each invocation names one explicit operation and its inputs, such as an
archive and its digest, a configuration file, or a release or instance
identifier, and acts on them without a further prompt. The state migration and
its return (HF-NAV-012) change every registered instance at once: before any
change they show the affected instances, accounts, and paths and require
explicit confirmation, which an explicit option may record in advance. The web
application itself MUST never invoke `sudo`.

**HF-INST-002 — Existing Linux accounts.** Each Hopper Files instance runs as
one explicitly registered, existing Linux account. The installer MUST NOT
create Linux users or groups and MUST NOT enumerate all system accounts for
automatic exposure. It MUST NOT change ownership or permissions of document
trees automatically.

**HF-INST-003 — Validation as the service identity.** Before registering or
updating an instance, the installer MUST validate the state location, the
instance configuration, and the required access as the target service UID.
Administrative access held by the installer is not proof that the service can
access a path.

**HF-INST-004 — Shared immutable application.** One shared installation serves
independent per-account instances. The recommended layout is identified
releases under `/opt/hopper-files/releases/<release-id>`, an administrator-owned
`/opt/hopper-files/current` selector, shared administrative configuration under
`/etc/opt/hopper-files`, and per-account state under
`$XDG_STATE_HOME/hopper-files` or `~/.local/state/hopper-files` when the XDG
variable is absent. Installations MAY select equivalent FHS-compatible paths.
Application processes MUST NOT modify releases, dependencies, the active
release selector, or directories that would let them replace those objects.

**HF-INST-005 — Per-instance isolation.** Each instance has its own process,
Linux identity, instance identifier, authentication material, signing secret,
sessions, cache, indexes, journals, UI state, and trash. No one of these stores
may be shared between accounts. An optional landing page lists only explicitly
registered instances and links to them; it accepts no credentials and performs
no automatic login.

**HF-NAV-010 — Service without file restrictions.** Each service unit runs as its
registered account without capabilities or privilege escalation:
`NoNewPrivileges=yes`, an empty `CapabilityBoundingSet=`, and an empty
`AmbientCapabilities=`. The unit applies no filesystem restriction beyond Linux
permissions: it sets no `ProtectSystem`, `ReadWritePaths`, `ReadOnlyPaths`,
`InaccessiblePaths`, or `PrivateTmp`, so `/tmp` and `/var/tmp` are the same as
in the account's terminal. The unit sets `UMask=0002`. A file or directory
created by Hopper Files receives the owner, group, mode, and access ACL that the
same account would obtain from a terminal in the same directory with that umask,
including the group inherited from a set-group-ID directory and entries from a
default POSIX ACL; Hopper Files adds no creation policy of its own. Upload,
ordinary copy, and extraction create new objects in that way and never import
ownership, group, set-ID bits, capabilities, access ACLs, or mode bits from
client, archive, or source metadata. These rules never repair, widen, or revoke
access on an existing object.

The unit receives no SSH-agent socket or operational shell environment; its
environment and `PATH` contain only runtime necessities. Releases,
dependencies, the active selector, and administrative configuration are
protected by their ownership and modes (HF-INST-004), not by the unit. Secrets
of the instance, such as its credential store and signing key, remain readable
only by their owner. Options that do not restrict files MAY remain.
Configuration alone is not proof: release validation tests the effective
unit.

**HF-INST-007 — Instance time zone.** Naming timestamps use an IANA time-zone
identifier that the administrator writes in the instance configuration. The
product has no default time zone and does not derive one from the host.
Changing the setting affects only future names and never renames existing
content.

**HF-INST-008 — Clean source installation.** A release is built and installed
from a clean, identified product source checkout, tag, or release artifact and
from pinned dependencies obtained through documented official sources. Private
planning material, site administration trees, credentials, user data, and files
from an earlier installation are not build or installation inputs.

### 2.2 Lifecycle

**HF-LIFE-001 — Supported lifecycle.** The installer supports initial shared
installation, instance registration, instance configuration updates, state
migration and its rollback (HF-NAV-012), application release updates, rollback
to an installed release, instance removal, and application uninstall.

**HF-LIFE-002 — Release update and rollback.** An update installs a new,
identified immutable release before switching the active selector atomically.
Rollback switches to a previously validated installed release. Neither action
changes user files or per-instance state formats silently. A state-format
migration follows HF-NAV-012.

**HF-LIFE-003 — Preservation on removal.** Removing an instance or uninstalling
the shared application preserves document data and per-account state by
default. Destructive state removal must be a separate, explicit administrative
action that names the affected instance and paths. Uninstall MUST NOT remove or
alter user files.

**HF-LIFE-004 — No implicit permission repair.** Registration, update,
rollback, and uninstall MUST NOT repair file access by widening modes,
changing owners, or adding the service account to unrelated groups. A failed
access check reports the exact configured path and required administrator
action.

**HF-NAV-012 — Migration and return.** Moving an instance from a root-based
release to this model, and back, is an explicit, journaled administrative
procedure. Converting state and trash runs as the service identity
(HF-INST-003); replacing configuration and units is done by the administrative
installer with confirmation (HF-INST-001).

Forward conversion covers every per-instance store that holds addresses:

- version 1 `ui-state.json` becomes version 2 (HF-META-004) and version 1 trash
  metadata becomes version 2 (HF-TRASH-001), resealed, by mapping each
  `(rootId,path)` to the absolute canonical path under the configuration in
  effect before the procedure, with the System view mapped to `/`;
- a UI record whose `rootId` has no mapping, such as a retired one, is kept as an
  orphaned record and is never dropped or remapped by guess; a trash item that
  cannot be mapped keeps its version 1 metadata and stays quarantined under
  HF-TRASH-002 until an explicit restore destination is chosen;
- UI records that map to the same absolute path are merged deterministically:
  favorite when either record is a favorite, the union of label IDs in the fixed
  order of HF-META-003, the emoji of the document-root record when both records
  have one, and the identity hints of that same record; every merge is reported;
- tabs keep their order and may repeat;
- terminal operation results are converted to absolute addresses; a terminal
  result that cannot be converted is kept as a terminal record, so that its
  token can never start a new operation (HF-API-005);
- derived stores, namely search cursors and tag-index data, are discarded
  explicitly, reported, and rebuilt on demand;
- the instance configuration loses `roots`, `system`, `protectedPaths`, and
  `creationPolicies`, and the unit is regenerated under HF-NAV-010.

The procedure starts only when no store holds a nonterminal journal: operation
journals, trash journals, attachment-move journals, or buffer-move commands.
Otherwise it refuses, names the pending journals, and asks for their recovery
under the release that created them; it never discards or rewrites a pending
journal. The same precondition applies to the return.

A release that implements version 2 refuses to serve an instance whose UI state
or trash metadata is still version 1: it fails closed with a clear report and
never initializes an empty state over the old one. Because the active release
selector is shared, the installer stops every instance registered under it,
converts each one, replaces their configuration and units, and only then
switches the selector and starts them. If any instance cannot be converted, the
installer returns the instances already converted and keeps the previous release
selected.

The procedure keeps the version 1 state, trash metadata, configuration, and unit
that it replaced. A return to the previous release stops the instances, switches
the selector back, restores that configuration and unit, and converts the
current version 2 records before any instance starts:

- inside a former document root, the record maps to that root;
- outside every former document root, it maps to the former System view only
  where that release represented it, that is, outside its protected namespaces
  (its immutable defaults and the saved `protectedPaths`) and outside document
  roots;
- a record that cannot be represented, such as a mark or tab in a place the
  previous release did not expose, is preserved in a return report together
  with the version 2 documents for a later migration, and is never silently
  discarded;
- a trash item that the previous release cannot represent remains in trash and
  is quarantined by that release rather than purged or restored;
- terminal results and derived stores are handled as in the forward direction.

Migration and return are idempotent, report the counts of converted, merged,
orphaned, expired, and unrepresentable records, and are tested before any
production use.

## 3. Instances and the filesystem

### 3.1 Filesystem base and visibility

**HF-NAV-001 — Single base.** Every instance navigates the filesystem from `/`
with the ordinary Linux permissions of its service account. There are no
configured document roots and no optional System view: registering an instance
needs only the instance settings and its service account. Hopper Files never
uses `sudo`, setuid helpers, or a browser-triggered privilege change, and the
service keeps `NoNewPrivileges=yes` (HF-NAV-010).

**HF-NAV-002 — Visibility.** A directory listing returns exactly the entries the
service account obtains when it lists that directory, including names that
begin with a dot and the pseudo-filesystem mount points `/proc`, `/sys`, and
`/dev`. Each entry reports its name, its type (directory, regular file,
symbolic link, or other), whether the account can open it, its size when it is
a regular file, its modification time, and its creation (birth) time when the
filesystem records it through `statx`. A filesystem or kernel without a birth
time reports it as null, and the interface shows it as unavailable instead of
substituting another time.

- A directory opens only when the account has both read and search permission
  on it. A directory the account can see in its parent but cannot open, such as
  `/root` or another account's home, is shown dimmed, without an expansion
  control, and does not open.
- A symbolic link is listed with its literal target, shown as `name → target`,
  and is never expanded in the tree. Activating it navigates to its resolved
  destination, whose real canonical path becomes the address (HF-NAV-005). A
  broken link, a link whose destination the account cannot open, and a link to
  an object that is neither a directory nor a regular file are dimmed and do not
  open.
- Devices, sockets, FIFOs, and other objects that are neither regular files nor
  directories are listed but never opened as documents, previewed, or read.
- A regular file with more than one link is listed and follows the ordinary
  rules, except that HF-NAV-007 excludes it from editing; HF-FILE-002 defines
  how a cross-filesystem move or trash separates its link.
- Regular files inside `/proc` and `/sys` may be opened for reading within the
  text limit of HF-LIMIT-001; saving there fails under HF-SAVE-003 because a
  temporary file cannot be created.
- The staging names that Hopper Files operations use,
  `.hopper-stage-<32 lowercase hex>.tmp` and `.hopper-stage-<32 lowercase
  hex>.dir`, are listed like any other entry; the safety of an operation rests
  on exclusive creation and journaled identity checks, not on hiding its
  names.
- An entry whose name is not valid UTF-8 is listed, but it has no address
  (HF-NAV-005). Its display name is lossless and unambiguous: every byte that
  is not part of valid UTF-8 is shown as `\xHH` in hexadecimal, and every
  literal backslash of that name as `\\`. It is marked as having no address,
  is shown dimmed, does not open, accepts no operation, and carries the reason
  in its tooltip. The address rule and its transport do not change.

### 3.2 Writing, notes, and traversal

**HF-NAV-003 — Writing.** Create, rename, move, copy, upload, extract, save, and
move to trash are permitted wherever the service account has the Linux
permission that the action needs on the concrete target, checked when the
operation runs. A permission failure is reported as `Sem permissão para gravar
aqui` or an equivalent operation-specific message, without effect on disk and
without blaming the user. The interface MAY disable actions where a listing
reports that a directory is not writable, but the filesystem remains the
authority. Hopper Files keeps no list of writable or read-only paths of its
own.

**HF-NAV-004 — Note features.** Note discovery, generated names, managed
attachments, and attachment collection apply to Markdown notes in any directory
that the service account can write. The note corpus of an instance is the set of
regular `.md` files whose parent directory is writable by the account and that
the traversal of HF-NAV-011 discovers from `/`. The instance's internal trash is
outside the corpus, so deleted notes do not return to it. A directory the
account cannot list cannot be discovered, so notes inside it are outside the
corpus; this is a documented limit rather than an error. The tag index does not
use the corpus: it reads the monitored folders of HF-META-002.

**HF-NAV-011 — Traversal limits.** Search, the tag index, note discovery, and
attachment-reference scans are bounded traversals. They never enter `/proc`,
`/sys`, `/dev`, or their descendants, and never follow symbolic links; the tag
index, note discovery, and attachment-reference scans also never enter the
instance's internal trash (HF-NAV-004). Manual navigation still reaches all of
those places under HF-NAV-002. Search starts at the directory that the request names
and descends from it (HF-API-004). Note discovery and attachment-reference scans
start at `/`, consider only the note corpus of HF-NAV-004, and include `/tmp`
and `/var/tmp` when they are writable. The tag index starts at each monitored
folder of HF-META-002. Below a monitored folder it does not enter a subdirectory
whose name starts with `.`, reported as the `hidden_directory` exclusion, nor
`/tmp` or `/var/tmp`, reported as `excluded_path`; such a directory is read only
from a monitored folder at or below it. A monitored folder that no longer
exists, is not a directory, or cannot be listed is reported as
`unavailable_folder`, and a note the account cannot read as `unreadable_file`;
neither makes the tag index incomplete. A directory the account cannot list is
counted and skipped. A budget stop under HF-LIMIT-001, a read error on a note
the traversal reads, or a race makes the result explicitly incomplete. Search and the tag index run outside the loop that
serves requests: while they run, listing directories and opening files are
not delayed by them. Release validation measures and reports the traversal
cost for each real account, including `/tmp` and `/var/tmp`.

## 4. Access, authentication, and sessions

### 4.1 Explicit access modes

**HF-ACCESS-004 — One mode at a time.** An instance selects exactly one access
mode. Local and remote modes are alternative configurations, not simultaneous
authorized entry points. In remote mode, the backend loopback connection is
proxy transport and does not add an authorized local browser origin or session.

**HF-ACCESS-001 — Local mode.** Local mode serves authenticated HTTP only on an
explicit loopback address and port. It accepts only the configured loopback
`Host` and exact local `Origin`. A local-mode cookie is `HttpOnly`,
`SameSite=Strict`, scoped to the instance base path, and has no `Domain`.
`Secure` may be omitted only in this explicitly configured local mode. The
application MUST reject non-loopback publication of this mode.

**HF-ACCESS-002 — Remote mode.** Remote mode places the loopback HTTP service
behind an explicitly trusted HTTPS reverse proxy. The configured external base
URL is HTTPS. Session cookies are always `Secure`, `HttpOnly`,
`SameSite=Strict`, scoped to the instance base path, and have no `Domain`.
Forwarded host and scheme headers are accepted only from the configured trusted
proxy. The application MUST NOT disable `Secure` or weaken origin checks because
one request arrived over HTTP. Tailscale Serve is one optional proxy choice,
not a product dependency; public exposure is not implied.

**HF-ACCESS-003 — Exact public origin and base path.** Each instance has one
configured external base URL containing scheme, host, port when non-default,
and base path. Its expected HTTP `Origin` is derived from scheme, host, and port
only, because the `Origin` header contains no path. Every browser state-changing
request requires that exact `Origin`, the expected `Host`, and a request path
under the configured base path. CORS is closed. Redirects remain within the
configured external base URL.

### 4.2 Authentication

**HF-AUTH-001 — Independent authentication.** Selecting or opening an instance
does not grant access. Each instance authenticates against only its own
credential store and accepts only its own cookie name, signature key, account,
and instance ID. A proxy identity header, when available, does not replace the
instance credential. The store contains no plaintext or reversible password.
Its version 1 verifier is
`{version:1,algorithm:"scrypt",n:131072,r:8,p:1,dklen:64,salt,digest}`,
where each password definition or change uses a new 16-byte random salt from
the operating system and unambiguous binary encoding. The password is taken as
exact UTF-8 without normalization or truncation and is rejected before the KDF
when it exceeds 1,024 bytes. Verification uses Python `hashlib.scrypt` with
`maxmem=268435456` and compares same-length digest byte strings only with
`hmac.compare_digest`. Startup and release validation MUST prove that the
runtime supports exactly these parameters and sufficient memory; an unknown or
malformed record, unavailable KDF, or incompatible resource limit fails closed
and never selects weaker parameters. A missing or invalid record and a wrong
password receive the same external response; when needed to avoid exposing
credential state, the server runs a dummy verification with the same parameters.
At most one password KDF runs concurrently per instance. Passwords and verifier
material never enter process arguments, logs, sessions, or recoverable UI state.
Every login or password-change attempt consumes its nonce; after a failed
attempt the page obtains a fresh one before the next. `GET app` without a valid
session answers `401` with the login page, and the login page with a valid
session redirects to the application. After a `429`, the login page states the
wait given by `Retry-After`.

**HF-AUTH-002 — Cookie isolation.** Cookie names are unique per instance. Port
and path scoping are defense-in-depth, not account boundaries: servers MUST
reject a valid cookie replayed to another instance or port. Instance secrets,
sessions, CSRF tokens, and password files are never shared.

**HF-AUTH-003 — CSRF.** Every authenticated state-changing request requires a
CSRF token bound to the session. `POST login` and `POST password` require a
pre-authentication nonce obtained from `GET login` or `GET password`; the nonce
grants no document access. Requests lacking the required browser origin receive
no implicit exception. Before any password KDF, every login or password-change
attempt consumes one token from each of two persistent token buckets: the
network-origin bucket has capacity 5 and restores one token per 60 seconds; the
global instance bucket has capacity 20 and restores one token per 15 seconds.
Network origin comes from the peer socket
or an explicitly trusted proxy, never a free client header. An empty bucket or an
already active verifier returns `429` with the same generic body and a
`Retry-After` equal to the whole-second delay until both buckets can next admit
an attempt; KDF saturation uses a documented minimum of one second. The
response never reveals the account state, limiting bucket, remaining attempts,
or credential validity. Success does not refill either bucket. Limiter state
survives service restart without resetting protection or creating a permanent
lockout and is isolated between instances; logs contain only minimal outcome
and operational metadata, never the attempted password, verifier, or nonce.

**HF-AUTH-004 — Session termination.** Logout and expiry invalidate the session
and its CSRF token for that instance only. Session duration is an explicit
instance setting; changing it does not affect another instance. The login page
links to a password change page that needs no session: it takes the current
password and the new password typed twice. `POST password` refuses a mismatched,
empty, or over-1,024-byte new password before any KDF, verifies the current
password with one KDF under the limits of HF-AUTH-003, and answers a wrong or
unverifiable current password with the same generic failure as a login. Recovery
of a forgotten password is a local command run as the instance's account,
which an administrator runs with `sudo -u`; it reads the new password without
terminal echo and never accepts it as a process argument.
Either path atomically publishes a new `0600` verifier with a fresh salt,
increments the instance credential epoch, and invalidates every existing session
and CSRF token for that instance; after the change from the login page, the
login page states that the password changed. In the application, a `401` on any
request, including opening or saving a document, stops periodic requests and
shows that the session ended,
with a way to sign in again; the open content stays on screen, a document with
unsaved changes is named, and a later action by the user that fails the same
way shows the notice again.

## 5. HTTP API and path model

**HF-NAV-005 — Address.** A document address is the absolute canonical path of
the object. It begins with `/`, uses `/` as its only separator, has no trailing
separator except the base `/` itself, and contains no `.` or `..` component,
empty component, NUL, ambiguous encoding, or double-decoded form. Every
component is resolved without following symbolic links (HF-API-007); an
address whose resolution meets a symbolic link is rejected, and a client reaches
a link's destination through the resolved real path reported by the listing.
The source and destination of an operation are separate addresses. The API MAY
transport an address as a fixed base identifier with a path relative to `/`,
provided that every API, metadata record, cursor, and version observes the
absolute canonical path as the identity.

**HF-API-003 — Core endpoints.** Under the configured instance base path, the
contract includes `GET/POST login`, `GET/POST password`, `POST logout`,
`GET app`, `GET api/list`, `GET api/file`, `PUT api/file`, `GET api/raw`,
`POST api/files`, subordinate
operation-token issue and status routes, `GET api/search`, `GET/PUT api/state`,
`GET api/tags`, `GET/POST api/tag-folders`, `GET/POST api/trash`,
`GET api/dir-size`, and `POST api/changes` (HF-API-008). Route subdivision may change if the same typed inputs,
authorization, outcomes, and error semantics remain.

`GET api/tag-folders` returns the monitored folders of HF-META-002, each with
its absolute address and whether it is currently a directory the account can
list. `POST api/tag-folders` takes one absolute canonical `path` and a boolean
`monitored`, requires the CSRF token, and returns the complete resulting list.
Marking requires a directory the account can list, reached without symbolic
links; otherwise it returns `403` without a write (HF-API-006). A 65th folder
returns `413`. Marking a listed folder or unmarking an absent one changes
nothing, so a repeated request has the same outcome.

`GET api/state` returns the complete versioned UI state and its current
`stateRevision`. `PUT api/state` requires `baseRevision`; omission returns
`428`, and a value different from the current revision returns `409` without a
write. `stateRevision` is generated only by the server and cannot be chosen by
the client. A successful response returns the complete committed state and its
new revision. A conflict response returns the current revision but does not
expose another instance's state.

**HF-API-004 — Query semantics.** `GET api/list` returns entries and a listing
version. `GET api/search` accepts `mode=name|text`, a `scope` that is the
address of a directory the account can open (the open directory in the UI), a
query of at most 256
Unicode code points, a `limit` whose default is 100 and maximum is 200, and an
optional opaque cursor. It trims surrounding whitespace and requires at least
one code point for `name` or two for `text`. The complete query is a literal
substring, never a regular expression, glob, or `ext:` operator. Comparison is
`NFC(casefold(NFC(value)))`; it unifies canonical equivalents and case without
removing accents. Search descends recursively from the scope under the limits of
HF-NAV-011 and never follows symbolic links; a `text` search never reads
through a link. Dotfiles and the instance's own state directory, including its
internal trash, are searchable like any other readable directory. The initial
traversal has a 10-second execution budget and materializes at most 10,000
ordered results and 64 MiB of serialized snapshot data in protected
per-instance cursor state.

`name` compares the basename of regular files, directories, and symbolic links;
a matching link is returned as the link itself and is never followed. `text`
reads only regular files no larger than the 5 MiB text limit whose
complete bytes are valid UTF-8 and contain no NUL. It compares within each
logical line and returns at most one item per file: the lowest-numbered matching
line, with that line number and a literal snippet of at most 200 code points.
It never falls back to a legacy encoding or matches across a line break. Name
results and text-file results have the deterministic total order of their
absolute canonical paths, ascending by UTF-8 byte order. Page boundaries do not change
that order. This search corpus does not change the larger streamed-Markdown rule
used only by the attachment-reference scanner in HF-IMG-005.

A search response is
`{items,nextCursor,cursorExpiresAt,complete,omissions}`. It sets
`complete=true` only on a final page with no omission. A read error, entry race,
execution-budget stop, result cap, or snapshot-size cap may preserve safe
results from other entries only with `complete=false` and a categorized
omission. A directory inside the scope that the account cannot list is reported
as a categorized count and does not by itself make the result incomplete. Text
files excluded as
too large, invalid UTF-8, NUL-bearing, symbolic links, or special files are
outside the text corpus and are reported as
aggregate categorized counts rather than silently treated as searched.
A scope that is not an address of a directory the account can open fails the
whole request.

The authenticated cursor binds the instance, mode, normalized query, scope,
limit, the immutable materialized
result snapshot, and the next offset. It expires 15 minutes after the initial
request, after which its protected snapshot state is removed. Page size never
raises the result, memory, or time bounds and never makes a bounded snapshot
appear complete. Reuse with changed parameters returns
`400`; expiry returns `410`; a cooperative application mutation that
invalidates an observed directory version returns `409` and
requires a fresh search. Before serving a later page, the server revalidates
captured directory and result-object versions needed for that page; an external
change observed during materialization or revalidation also returns `409`.
Writers outside Hopper Files remain subject to HF-SEC-004 rather than an
impossible universal snapshot guarantee. A safe materialized result set may
produce `nextCursor`; an omission outside that snapshot does not pretend that
pagination can recover it. The UI mode `aqui` remains a separate immediate
client-side name filter over the current `GET api/list` result: it matches a
case-insensitive substring after NFC normalization and includes only the
immediate file and directory children of the open directory. It does not read
content, descend, call the search API, or enter search
history, and it clears when the filter closes or the open directory changes.
`GET api/dir-size` calculates one directory's size on demand; no continuous
tree-size scan runs in the background. It walks under the HF-NAV-011 rules (no
symbolic link, no `/proc`, `/sys`, `/dev`, or other pseudo filesystem) within a
3-second execution budget, outside the request loop, with at most two
measurements at a time; a queued measurement for a client that has already
disconnected is skipped. It returns the bytes of the regular files it counted,
the file and directory counts, `complete`, and categorized omissions. A budget
stop, an unreadable subdirectory, a race, or a name that is not UTF-8 yields
`complete: false` with the partial total instead of an error. Measuring a
pseudo filesystem itself returns zero bytes with `complete: false`. `GET api/raw` streams bytes with a safe MIME type and suitable
`Content-Disposition`.

**HF-API-005 — Mutation semantics.** `POST api/files` supports explicit
`create`, `copy`, `move`, `rename`, `upload`, `zip`, and `extract` actions. Each
request has a server-issued opaque operation token and each source and
destination is its own address (HF-NAV-005). The token contains an
unpredictable UUID plus authenticated instance, `issuedAt`, and `expiresAt`
claims. It permits an operation to start for seven days. The first submitted
payload binds it to a canonical fingerprint of the actions, addresses, and
exact content digests. Reuse with a different fingerprint returns `409` before
mutation. An expired token cannot start or restart mutation, even after its
stored result has been garbage-collected.

Before publishing any item, the server durably records and syncs its intent,
action, source and destination addresses, observed source identity, staged
identity and digest, and recovery state in the per-instance journal. A replay
with the same token and fingerprint returns the stored per-item result and may
resume only an item proved not to have been published. If publication may have
occurred before its completion record, recovery compares the intended identity
and digest with the destination and source; it records the observed commit or
an indeterminate state and never publishes a second copy blindly. A batch
response reports every committed, uncommitted, and indeterminate item. A
failure after a partial commit MUST NOT be disguised as a generic total
failure.

Terminal results remain available until seven days after `expiresAt`.
Nonterminal journals remain until internal recovery reaches a terminal state,
including after token expiry; the status route remains available for them,
while expiry still forbids client-triggered mutation. After a terminal result
is garbage-collected, the authenticated expiry still prevents the old token from
becoming a new operation. Each instance has a documented cap on active and
indeterminate operations and rejects new tokens while that cap would be
exceeded, so unresolved recovery cannot grow state without bound.

**HF-API-006 — Errors.** Version or destination conflicts use `409` and make no
unreported change. Missing required preconditions use `428`. Limit violations
use `413`. An expired operation token uses `410` and makes no change. Invalid
input uses `400` or `422`. A forbidden or out-of-scope target uses `403` or a
non-disclosing `404`. Unexpected failures never imply that no item was
committed when the server knows otherwise.

**HF-API-007 — Containment.** The server resolves every component of an address
from `/` using directory descriptors or an equivalent race-resistant method,
without following symbolic links, and rejects an address whose resolution meets
a link. Content reads and mutations therefore act on the real object that the
address names. Authorization is the service account's Linux permission at
operation time; there is no root or protected-namespace boundary to enforce
(HF-NAV-009). A lexical prefix or an earlier `realpath` result alone does not
establish what an address names.

**HF-API-008 — Change notices.** `POST api/changes` tells a client which of the
directories it shows changed on disk, by any writer. It requires a session
(`401` otherwise) and the CSRF token (`403` otherwise). The JSON body, at most
64 KiB (`413` otherwise), is `{rootId,paths,epoch,seq}`: `paths` names at most
256 directories in the transport form of `GET api/list`, and `epoch` and `seq`
repeat the previous response, or are `null` and `0` on the first request.
Another shape returns `422`. The server watches only named directories, through
the kernel's file notices, never by scanning the filesystem; it validates each
one as a listing does (HF-API-007) and reports in `rejected` the ones it cannot
watch, including ones beyond its watch limit. A response is
`{supported,epoch,seq,resync,changes,rejected}`. `changes` lists `{path,name}`
for each changed entry of a named directory; `name` is `null` when the
directory itself changed, the name is not UTF-8, or too many names changed. The
server answers about 200 ms after the first notice in a named directory, or
with no change after at most 20 seconds. A first request, a different `epoch`
(the server restarted), a `seq` the server no longer holds, or a lost notice
queue returns at once with `resync: true`, and the client reads again what it
shows. `supported: false` means that no notice is available; the client then
stops asking. A directory that no request names for 60 seconds stops being
watched. Once change notices are in use, `GET api/list` and `GET api/file` start
watching the directory they read before reading it, so a change between the
read and the client's next request is reported. The kernel does not report
changes that another machine makes on a network filesystem; those appear when
the view is read again (HF-FILE-006).

## 6. Navigation and filesystem operations

**HF-FILE-001 — Navigation.** The UI shows the filesystem tree from `/`
(HF-NAV-006), search, trash, favorites, tags, and labels. Opening, creating,
renaming, moving, copying, deleting, uploading, downloading, ZIP operations,
and size calculation act only as the service account's Linux permissions allow
(HF-NAV-003). A listing refresh MUST NOT discard an editor buffer. Move, copy,
and a conflict alternative choose the destination in a folder picker (a
breadcrumb, the subfolders of the folder being shown, and the item name) instead
of a typed path; it shows the reason whenever its confirm button is disabled.
Items dragged from the listing can be dropped on a folder in the listing or the
tree (move, or copy with Option/Alt), on Favorites, or on a label; files dropped
from the computer are added to the open folder or to the folder under them.

**HF-FILE-002 — Exclusive destinations.** Copy, move, rename, extraction, and
restoration publish to an exclusive destination. An occupied destination returns
a conflict and requires an explicit alternative; no operation silently
overwrites. A cross-filesystem copy or move first writes to exclusive
destination staging, verifies the complete bytes and the access policy required
for that operation, syncs the staged objects, publishes the destination
exclusively, syncs its parent, and durably records destination publication
before any source removal. An ordinary copy creates new objects under
HF-NAV-010. A cross-filesystem move preserves the source UID, GID, permission
bits excluding set-user-ID and set-group-ID, POSIX access ACL, and a directory's
POSIX default ACL exactly; it refuses before publication if those values cannot
be established and verified or if the source has capabilities, set-ID bits, or
unsupported extended or security metadata. It does not preserve source
timestamps as an access guarantee or clone unrecognized metadata blindly. Copy
never removes the source. Move removes it only after that record, then syncs the
source parent. Failure before publication removes only operation-owned staging
and keeps the source. Failure after publication may leave both copies; journal
recovery reconciles their identities and digests and never deletes both. A
same-filesystem rename retains the inode and its metadata. The same ordering and
metadata rules apply recursively to a directory as one planned operation. A
regular file with more than one link that is moved across filesystems follows
the cross-filesystem move rules above, including exact preservation of the
source UID, GID, permission bits, and access ACL and the same refusals; it never
falls back to the new-object rules of HF-NAV-010. Only the destination inode is
new: only the addressed name is removed, the other names keep the original
inode, and the response reports that the link was separated. Moving such a file
to trash across filesystems preserves the same metadata under HF-TRASH-005. A
same-filesystem rename keeps the inode and all its links.

A move or rename that changes the path of a Markdown note, a referenced image,
or a directory containing either MUST preserve the target identity of every
affected relative Markdown image reference. Before any publication, the server
builds one journaled plan. It resolves affected saved references, maps old and
new note and image addresses, and stages the minimum URL-destination rewrites
needed for each reference to resolve to the same logical image after the move.
Moving only a note resolves that note's references and their targets. Moving an
image scans the note corpus of HF-NAV-004 to prove and rewrite every saved
reference that targets it. Moving a directory applies both rules to its notes
and images. Only Markdown image destinations affected by the move are rewritten;
ordinary links, wiki links, and backlinks are not analyzed or introduced.

The plan records the strong base version and before/after digest of every note
to rewrite. Only affected open buffers in connected sessions participate: each
must acknowledge the same saved base version and be clean, and is held from
editing during the short publication step before it is reloaded at the new
version. A dirty, changed, or unreachable affected buffer, an incomplete required
reference scan, or an unsafe rewrite returns `409` before any part of the move or
rewrite is published. All staged destinations and rewritten notes are verified
and synced before publication. Each staged rewrite satisfies HF-SAVE-002 and
HF-SAVE-003 before any part of the plan is published. The per-item journal makes
an interrupted multi-file commit recoverable without
blind rollback or silent restoration. A collection decision cannot treat these
planned rewrites as removal of the image reference.

**HF-FILE-003 — Multi-item preflight and partial results.** Before a multi-item
operation, the server validates destinations, limits, and available capacity as
far as possible. If a later item fails after earlier commits, the response
identifies both sets and preserves a safe retry path. A failed second upload,
for example, cannot hide a successful first upload.

**HF-FILE-004 — ZIP safety.** ZIP creation and extraction stream large data,
check free space, and use unpredictable exclusive temporary names. Extraction
rejects absolute paths, `..`, repeated destinations, links that escape policy,
special files, excessive depth or count, and expansion beyond configured
limits. A pre-existing regular file resembling a temporary name is never
truncated.

**HF-FILE-005 — Office files.** Office documents use normal file operations,
download, and external opening. The UI exposes no internal conversion or office
suite preview.

**HF-FILE-006 — Changes made outside the application.** While the browser tab
is visible, the interface asks `POST api/changes` (HF-API-008) about the
directories it shows: the open folder, the open branches of the `/` tree and of
favorite trees, and the folders of open documents and image or PDF previews.
It asks again after each answer, and at once when that set changes. A folder
that starts being watched is read once more after its first answer, so a change
made just before its watch began is not lost. A change reported in one of them
is shown within about 2 seconds, without any action:

- the listing and the tree read the folder again and redraw only when entries
  differ, keeping the list scroll position, keyboard focus, and marked items
  that still exist; a redraw waits while a context menu, a dialog, a drag, or a
  pointer press is in progress, and folder sizes already measured are kept;
- when the open folder is deleted or can no longer be opened, the listing
  drops its entries and says so; the interface also watches the folder's
  ancestors, and when the folder exists again, its new content is shown;
- a burst of changes costs a few reads per folder, not one per changed entry;
- an open text document without local edits shows the new content, keeps its
  view position, and shows a short notice; one with unsaved edits keeps the
  buffer and at once shows `Mudou fora do app` with the conflict choices of
  HF-SAVE-006 (copy or reload the server version);
- the user's own save produces no notice; a content-identical change updates
  only the stored version;
- a document removed or renamed outside the application shows
  `Removido fora do app` and stays open for copy or download;
- an open image or PDF shows its new version.

While the tab is hidden, the interface makes no change request and reads
nothing. On return it reads again the open folder, open branches, and open
documents, then resumes asking. After a network or server failure it waits
longer before each retry; after `401` it stops. An answer with `resync` reads
again everything shown.

## 7. Names and creation

**HF-NAME-001 — Proposed name.** The creation dialog offers exact-name mode and
generated-name mode in one segmented control, with a preview of the name that
will be created. Exact-name mode suggests a free name in the open directory
(`novo-arquivo.txt`, `nova-nota.md`, or `nova-pasta`, then `-2`, `-3`, … when
taken) with its stem selected. Generated-name mode presents subject,
extension, and a live preview. The reference format is
`subject-YYYYMMDD-HHMMSS.ext`; Markdown is the default extension when a note is
appropriate. Directories retain the entered name without a required
timestamp. A disabled Create button always shows its reason next to it.

**HF-NAME-002 — Slug.** The generated subject slug is Unicode NFD with combining
marks removed, lowercased, split into `[a-z0-9]+` blocks joined by hyphens, and
limited to 60 characters, cutting at the last hyphen when practical. An empty
result becomes `nota`. The chosen extension is preserved. This transformation
applies only to generated-name mode, including a generated upload name. In
exact-name mode the complete entered filename is preserved without case,
Unicode, whitespace, punctuation, extension, slug, or timestamp normalization,
subject only to the path, containment, and supported-filename checks that apply
to every item. No heuristic list of recognized tools or file types selects a
mode.

**HF-NAME-003 — Timestamp and collision.** The effective timestamp is created
from the server clock and configured instance time zone when the user commits
creation or a generated-name upload, not when the preview first appears. In
generated-name mode, a collision adds `-2`, `-3`, and so on before the
extension, with exclusive creation and at most 30 proposals before returning
an explicit conflict. Exact-name creation and original-name upload never add a
suffix automatically: an occupied destination returns `409` and the UI
requires an explicit alternative name or mode choice.

**HF-NAME-004 — Exact technical names.** Exact-name mode is the defined path for
dotfiles, extensionless names, tool names, source-code and configuration files,
and any other literal filename; the server does not classify them through a
mutable list or filename heuristic. Generated-name mode is used only when the
user explicitly chooses it: the dialog opens in exact-name mode unless the user
chose generated-name mode the last time they created that kind of item in the
same browser, and the selected mode is always visible and can be changed
before creation. Creating an item does not move the current navigation
location.

**HF-NAME-005 — New note.** In exact-name mode, a note name without `.md` or
`.markdown` receives `.md`. A new Markdown note is atomically created with
`# <title>\n` under HF-NAV-010. Failure leaves no
empty file and no partially initialized note.

**HF-NAME-006 — Upload choice.** Upload shows a preview per file and one batch
choice, visible and changeable before sending: preserve original names exactly
or apply the generated naming format. Original names are the initial choice
unless the user chose generated names in their previous upload in the same
browser. Original-name collisions return `409` and require an
explicit alternative; generated collisions use the suffix rule in HF-NAME-003.
Version 1 has no retroactive naming-conformance panel.

## 8. User interface and UI metadata

### 8.1 Layout and responsive behavior

**HF-UI-001 — Desktop layout.** The desktop interface has the left sidebar of
HF-NAV-006; a central top area with tabs and, for a Markdown document, the
single view control of HF-NAV-008; a breadcrumb row with the absolute path; a
listing; and an optional resizable secondary split pane, which never leaves the
listing narrower than 260 px nor the document narrower than 300 px. The initial sidebar
width is 250 px and is adjustable. Both light and dark themes are supported.
The listing has the columns Name, Size, Created (desktop only), and Modified,
and each one sorts the listing. A file shows its size from the listing; a
directory shows the size that `GET api/dir-size` measures while the directory is
shown, with `…` during the measurement, a trailing `+` on a partial total, and
`—` for a pseudo filesystem or a directory the account cannot read. Dates are
shown in the instance time zone as `Hoje, HH:MM`, `Ontem, HH:MM`, or
`DD/MM/AAAA HH:MM`, with the full date in the tooltip. Items checked in the
listing go to the Trash through a toolbar button, the Delete key
(Cmd/Ctrl+Backspace), or the context menu, after one confirmation for the whole
selection.
Checked items form the selection: a checkbox, Cmd/Ctrl+click, Shift+click for a
range, Cmd/Ctrl+A for all, and Esc to clear. The toolbar and the context menu
act on the whole selection. The context menu of every item with an address
offers `Copiar caminho`, which copies the item's absolute path to the
clipboard. Opened on one of several checked items, the menu offers `Copiar os
N caminhos` instead, which copies their paths, one per line, in listing order.
The `/` node, a symbolic link, a special file, an entry that a listing reports
the account cannot open, and an item whose parent directory cannot be listed
have a context menu with only `Copiar caminho`. An entry without an address
has no context menu. In the listing, the arrow keys move between rows
(one Tab stop for the list), Enter opens, Space checks, F2 renames, and Delete
or Cmd/Ctrl+Backspace sends to the Trash. Redrawing the listing keeps keyboard
focus on the same row or column header, and a folder opened with Enter takes
focus to its first row. The second click of a double click
does not open the item that took the first one's place, and a row of the
previous listing clicked while another folder loads opens that row's own item.
Columns drop by
available width (Created, then Modified, then Size) so the name never
disappears. Browser Back and Forward move between the folders, documents, and
sidebar views (Trash, a tag, a label) visited in the application. A step that
happened in another tab returns to that tab while it still shows the same
folder, a step that would change nothing on screen is skipped, and returning
to a folder by Back, Forward, a tab, or closing a document restores the
listing's scroll position and focuses the row that was opened. Open document
tabs and the active tab return after a reload in the same browser tab (no
unsaved text is stored). In the tree, the
arrow keys move between nodes and open or close folders, and a node has the
same context menu as its listing row.

**HF-NAV-006 — Sidebar.** From top to bottom, the sidebar has: the title with
Settings and Theme; search; Favorites; the `Arquivos & Pastas` section with a
single tree whose first node is `/`; the Trash, in a block separated from the
tree by a rule; and Tags and Labels. The title, Settings, Theme, and search stay
fixed at the top while the rest scrolls, and the sidebar never scrolls
sideways. Labels appear in the fixed order of their colors. On first access `/` is collapsed, and its
children appear only when it is expanded. The `/` tree has no root selector,
separate root row, special folder, badge, or shortcut, and nothing in it
expands on its own when the application starts. When the location changes
outside the trees (listing, breadcrumb, link, tab, search result, or an opened
document), the tree expands the ancestors of the current item, marks that item as
current, and scrolls only as much as needed to show it. Reloading the same
folder does not reopen a branch the user collapsed. Files appear
in the tree together with directories. The chevron expands and collapses only
its node; activating a directory's name navigates to it and expands it without
collapsing it. Symbolic links behave as HF-NAV-002 defines. Collapsing the
`Arquivos & Pastas` section hides the whole tree, including `/`, and the same
browser keeps it collapsed after a reload. Favorites appear only when at least
one exists; to tell a directory from a file, the parent directory of each
favorite is listed. Each favorite directory that the account can open is the
first node of its own tree, which follows the rules above; any other favorite
is a single row that behaves like its row in the `/` tree, so a file opens, a
symbolic link leads to its target, and an entry the account cannot open stays
dimmed with its reason (HF-NAV-002). Each favorite tree keeps its own expanded
branches, and the same browser restores them after a reload. While the
Favorites section is open and the current item is a favorite other than `/`
or lies inside one, the `/` tree does not expand on its own: the deepest of the
expanded favorites that contain the item expands to it instead, and no tree
expands when every favorite that contains it is collapsed. Activating a
directory or file row in one tree does not expand the other trees; activating a
symbolic link counts as a change of location outside the trees. Items dropped
anywhere on Favorites become favorites; no row under Favorites is a move
target. When the
sidebar is narrow, the section title shortens with an ellipsis and its tools
stay visible. The breadcrumb
shows the absolute path starting at `/`, emphasizes only its last segment, and
scrolls horizontally so that the last segment, the current item, is visible.

**HF-UI-002 — Mobile layout.** At the 720 px responsive threshold, the sidebar
becomes a button-controlled column and the split pane is hidden. Touch layouts
provide larger targets, and the menu and Home buttons keep their size when the
open column narrows the toolbar. While items are checked on a narrow screen,
the toolbar shows the selection actions in place of the creation actions. On
touch and narrow screens the toolbar wraps instead of hiding buttons past the
edge. On a touch screen, holding a finger still on an item row for half a
second opens that item's menu, the same menu as a secondary click, and while
items are checked a `⋯` toolbar button opens the menu for them. A tap anywhere
in a row's selection cell toggles its checkbox. The interface honors
reduced-motion preferences.

**HF-UI-003 — Usability.** Desktop, iPhone-sized, and iPad-sized layouts must be
tested for overflow, touch targets, focus, contrast, keyboard operation, and
absence of excluded commands. Responsive simulation is useful but does not
replace validation on the intended browser/device classes.

### 8.2 Tags, labels, favorites, and state

**HF-META-001 — Markdown tags.** A tag is ordinary Markdown text beginning with
`#` followed by a Unicode letter; its remaining body contains letters, digits,
or hyphens and ends in a letter or digit. Tags normalize to NFC and compare
case-insensitively. Text inside fenced or inline code, a link destination or
URL, and hexadecimal color notation is not indexed as a tag. A note has at most
64 unique tags and each tag has at most 80 characters.

**HF-META-002 — Derived index.** Tags are indexed per instance across the
folders that the account marks as monitored, traversed under HF-NAV-011. There
is no default: with no monitored folder the index is empty. A monitored folder
covers itself and everything below it, and nothing above it. Below it, a
subdirectory whose name starts with `.` and the directories `/tmp` and
`/var/tmp` are left out; such a directory is read only from a monitored folder
at or below it. The rule considers only directories: every regular `.md` file
in a directory the traversal reads is indexed, whether its own name starts with
`.` and whether or not the account can write to it. A monitored folder that the
traversal of another one already read is not read again; any other is read
from itself, including one below a directory the account can pass through but
not list. The interface marks a directory from its context menu or its
information panel; for a directory that a monitored folder already reads, it
names that folder instead of offering the mark. The list holds at most 64
absolute canonical addresses in the per-instance document `tag-folders.json`,
version 1, separate from `ui-state.json` so that a release without this
feature ignores it. It is a setting, not item metadata: a move, rename, or
deletion leaves it unchanged, and a listed folder that is gone is reported as
unavailable until it exists again or is unmarked. The index is derived state: an
indexing failure does not modify Markdown. A traversal that stops early makes
any operation requiring a complete tag view explicitly incomplete. An instance
may ignore normalized tags chosen in its settings: while ignored, a tag is
absent from the tag list, its counts, and per-tag lookup. Ignoring or restoring
a tag never modifies Markdown or other files.

**HF-META-003 — Label identities.** The seven stable technical label IDs and
colors are:

| ID | Color |
| --- | --- |
| `vermelho` | `#ff3b30` |
| `laranja` | `#ff9500` |
| `amarelo` | `#ffcc00` |
| `verde` | `#34c759` |
| `azul` | `#0a84ff` |
| `roxo` | `#af52de` |
| `cinza` | `#8e8e93` |

An account may rename the displayed label but not its ID or base color. An item
may have multiple labels, may be a favorite, and a marked directory may have an
optional emoji.

**HF-META-004 — Versioned UI state.** Per-instance `ui-state.json` is versioned.
`version` is the schema version and is independent of the content revision.
Version 2 is an object with `version: 2`; `stateRevision`, a persistent integer
that starts at zero and increases by exactly one for every committed state
change; `labels`, mapping the seven fixed IDs to `{name,color}`; `items`, a list
of `{path,labelIds,favorite,emoji,inode,device}`; `tabs`, a list of
`{path,mode}`; `orphans`, a list of version 1 records whose root could not be
mapped by HF-NAV-012, kept only for reporting and rollback and never shown as
active marks; and `preferences`, containing theme, ordering, and density
choices, an optional `ignoredTags` list, and an optional integer
`sidebarWidth` from 200 to 420 pixels. `ignoredTags` is omitted when empty;
otherwise it holds at most 256 unique tags in the normalized form of
HF-META-001. `path` is the absolute canonical address of HF-NAV-005. Item
identity is `path` and duplicate items are invalid; tabs may repeat. `labelIds`
contains only known IDs; `emoji` is a short string or null; inode and device
values are numbers or null and are hints, not authorization or permanent
identity. Tabs never contain document content or unsaved text. Version 1, which
identified records by `(rootId,path)`, is read only by the migration of
HF-NAV-012; the release refuses to serve an instance whose state is still
version 1 and never initializes an empty document over it.

**HF-META-005 — Metadata movement.** A move or rename performed by Hopper Files
moves related UI metadata atomically or through explicit recovery. For an
external move, reconciliation may use identity hints but reports an orphan when
uncertain. It never attaches metadata merely because another file later uses
the same path. Deleting an item removes its favorite, labels, and directory emoji, and those
of marked descendants when the item is a directory, from active UI state. The
same journaled operation transfers identity-scoped copies to that item's trash
metadata using paths relative to the trash payload. New items created at any of
the former paths receive no such metadata, regardless of inode reuse. Restoring
the trash item maps the saved relative records to the actual original or
explicitly chosen alternative base address; inode and device remain
reconciliation hints and never establish this identity by themselves. Every
application mutation of UI state, including move, delete, restore, and recovery,
commits under the same state lock, increments `stateRevision`, and cannot be
undone by a later `PUT api/state` based on an older revision.

**HF-META-006 — Safe state persistence.** Configuration and mutable metadata
files use schema versions, exclusive temporary files, atomic publication where
the filesystem supports it, and cooperative serialization. Under the state
lock, a writer rechecks `baseRevision` when the request has one, durably commits
the complete new document, and only then exposes the incremented revision. This
prevents lost updates between processes and sessions, not merely simultaneous
physical writes. A write failure does not silently replace valid state with an
empty, truncated, or older document. On `409`, the client preserves its local
changes and offers explicit reload or resolution against the returned current
revision; it never silently overwrites either side. Dismissing that choice
merges both sides, so later changes keep being saved. After a failed write for
any reason other than `401` or `422`, the client keeps the local change, says
so, and retries on its own with a growing delay and when the network returns;
the notice clears once a write succeeds. This state protocol does not make tabs
a store for dirty editor buffers.

## 9. Markdown, editor, viewing, and saving

### 9.1 Markdown and editor behavior

**HF-EDIT-001 — Portable Markdown.** Notes are UTF-8 `.md` files with no
proprietary embedded format. The displayed title comes from the first H1 outside
front matter and code fences, or from the filename when no such H1 exists.

**HF-EDIT-002 — Markdown editor.** CodeMirror holds the source document and is
the editing surface of the `Markdown` view (HF-NAV-008). Syntax styling remains
stable as the cursor moves and never hides Markdown markers in that view.
Commands include heading levels 1, 2, and
3, bold, italic, strikethrough, highlight using `==`, code, list, ordinary link,
and image. A heading control sets its level on the applicable lines, replacing
another level; when every applicable line already has that level, it removes
the heading. Inline formatting (bold, italic, strikethrough, highlight, and code)
acts on the selection when it lies within one line, wrapping only the selected
text and never surrounding whitespace or a heading, quote, list, or task
prefix. With no selection, it wraps the word that contains the cursor; outside
a word, including on an empty line, it inserts an empty pair with the cursor
between the markers. When the selection or cursor is already inside that
markup, or the text immediately around it is that pair, the command removes the
markup instead, except that a cursor right before the closing marker of a pair
with text moves past it, so the same shortcut starts and ends formatting while
typing. For a multiline selection it applies to the content of each
line, skips empty and code lines, removes the pair when every applicable line
already has it, and otherwise adds it only to unmarked lines. Code lines are
fenced-code lines and lines indented by four or more columns that are not list
items; an indented list item is ordinary content. Inside inline code only the
code command acts. The link command uses the selection, or the word or address
at the cursor, as the label and selects the placeholder destination; a label
beginning with `http://`, `https://`, or `www.` also becomes the destination,
with `https://` added before `www.`. Inside an existing link it selects that
link's destination instead of nesting a link. The palette marks as active the
inline markup around the main cursor, the heading level of its line, and the
bullet marker of its line.

The `-`, `*`, and `+` bullet controls insert the selected marker and one space
after existing indentation. Choosing the marker that every applicable line
already has removes it; choosing a different bullet replaces only its marker
(a numbered item's number included) and never duplicates a prefix. Heading and
bullet controls leave fenced-code lines unchanged. Each control shows the bullet shape that its
marker has in `Formatado`. An
empty line in a multiline selection is skipped, while invoking a bullet at a
cursor on an empty line creates one marker. `Tab` adds four spaces to each
applicable line and `Shift-Tab` removes up to four. Pressing Enter after a bullet
creates a raw following line without automatically inserting a marker or
indentation.

On a coarse-pointer device, the editor initially blocks only the virtual
keyboard while preserving cursor movement, selection, copying, and physical
keyboard input. A visible `Digitar` control enables or disables the virtual
keyboard, and loss of editor focus blocks it again. Pointer-fine layouts do not
show this control. `Cmd/Ctrl+S`, `Cmd/Ctrl+B`, `Cmd/Ctrl+I`, selection, and
editor-local undo/redo are supported.

**HF-EDIT-003 — Views and clean copy.** Markdown provides the `Formatado`,
`Markdown`, and `Limpo` views of HF-NAV-008. Clean copy first normalizes CRLF and CR
to LF, protects fenced-code bodies and inline-code content, and applies the line
recomposition below before removing Markdown syntax. It removes fence lines
while restoring fenced bodies unchanged after EOL normalization. It converts an
ordinary link to its label, a wiki-style token to its alias or identifier, an
image to its alt text, and an autolink to its URL. It removes quote and heading
prefixes, horizontal rules, table separator rows together with their line
break, border pipes, escapes, and
paired bold, italic, highlight, and strikethrough markers. It preserves the
complete marker, checkbox, and text of bullet, task, and hierarchical numbered
list items, but removes their indentation. Protected code content is restored
only after these transformations. These are text transformations only and add
no wiki-link or backlink behavior.

Line recomposition removes the greatest common leading margin within each
nonempty block while preserving relative indentation. Among non-code lines, let
`W` be the greatest line length and the join threshold be
`max(50, floor(0.88 * W))`. A line break is a candidate when the first line
reaches that threshold or when that line, one space, and the next first word
would exceed `W`. It never joins an initial YAML header, an empty line, a
Markdown hard break ending in two spaces or a backslash, a line with an aligned
column of three or more internal spaces, a heading, quote, table, rule, fence,
protected code, a continuation indented by four or more spaces, or a new block.
Recomposition activates only when the document has at least two candidate
breaks, and joins each accepted continuation with exactly one space. Final
cleanup removes trailing spaces and tabs, reduces consecutive empty lines to
one, removes leading empty lines, and trims trailing whitespace. Common-margin
removal still applies when fewer than two breaks qualify.

Plain `.txt` viewing performs only that line recomposition and final cleanup;
it does not interpret Markdown syntax. Source-code and other text formats remain
raw. The following fixtures are normative examples.

Markdown input:

````text
# Título **forte**

Este parágrafo sintético demonstra uma quebra automática de coluna
e esta segunda linha continua a mesma frase sem novo bloco
antes de terminar aqui.

   - item com ==marca==
      1.1. subitem *itálico*

```js
const raw = "**fica**";
```

[Guia](https://example.test) e ![Foto](img.png).
````

Expected clean output:

```text
Título forte

Este parágrafo sintético demonstra uma quebra automática de coluna e esta segunda linha continua a mesma frase sem novo bloco antes de terminar aqui.

- item com marca
1.1. subitem itálico

const raw = "**fica**";

Guia e Foto.
```

Plain-text input, where each line begins with four spaces:

```text
    **Texto** sintético demonstra quebra automática e preserva os sinais
    e esta segunda linha continua a mesma frase com ==destaque==
    antes de terminar com - marcador literal.
```

Expected plain-text output:

```text
**Texto** sintético demonstra quebra automática e preserva os sinais e esta segunda linha continua a mesma frase com ==destaque== antes de terminar com - marcador literal.
```

The short plain-text input `Rua A\nSala 2` remains on two lines because it has
fewer than two candidate breaks and neither line reaches the minimum threshold.

**HF-EDIT-004 — Other text and invalid UTF-8.** Other supported text formats use
a plain text editor that wraps long lines and saves with `Cmd/Ctrl+S`; the
shortcut also saves the open file from the `Formatado` and `Limpo` views. A file that is not valid UTF-8 is never silently decoded
and rewritten: the UI offers a clear error and download. Editing requires a
lossless decode.

**HF-EDIT-005 — Viewing.** Internal viewers cover text, Markdown, supported
raster images, and PDF. Untrusted Markdown, HTML, and SVG are sanitized or
served inertly and cannot execute script. Office conversion and preview remain
excluded.

**HF-EDIT-006 — No implicit autosave guarantee.** Version 1 requires an explicit
Save action and safe-exit guard. Autosave is not required and cannot replace
the no-data-loss contract. The product creates no automatic persistent buffer
copy outside the defined scope.

**HF-NAV-008 — Document views.** A Markdown document has one segmented control
with three views of the same buffer; there is no separate `Ler`/`Editar` level.

- `Formatado` shows the sanitized rendered HTML of the current buffer
  (HF-EDIT-005, HF-SEC-002), including tables, images, links, and code blocks,
  and accepts no typing. A code block wraps its long lines inside the block
  instead of scrolling sideways. A table wider than the pane scrolls sideways
  inside its own box, and the text around it stays in place. The list marker
  selects the bullet at any depth: `-` a filled disc, `*` a hollow circle, and
  `+` a square. A task item (`- [ ]`, `- [x]`) shows ☐ or ☑ instead of the
  bullet, with no form control. A tag of HF-META-001 in ordinary text, outside
  code, links, and URLs, is highlighted, carries its normalized name, and opens
  that tag's view when activated. An external link opens in a new browser tab.
- `Markdown` shows the complete source text in the CodeMirror editor with every
  Markdown marker visible, wrapping long lines to the pane width. It is the only
  view that accepts typing and the commands of HF-EDIT-002.
- `Limpo` shows the clean text of HF-EDIT-003 and accepts no typing, because
  derived text has no safe translation back to Markdown.

The `Salvo`/`• Não salvo` indicator and the Save action refer to the file in
every view. While an editable Markdown file is shown in `Formatado` or `Limpo`,
the editor bar states that editing happens in `Markdown` and offers a direct
switch to it. A `.md` file opens in `Formatado`, except a note just created
with New note, which opens in `Markdown` with the cursor at the end of the
text. A text file that is not Markdown
has no view control and one text view, editable when HF-NAV-007 allows. In a
file that is not editable, every view is read-only. Editing inside `Formatado`
is outside this version, because the editor's live preview does not render
tables, images, links, and code blocks as the rendered HTML does.

### 9.2 Save protocol and dirty state

**HF-SAVE-001 — Strong version precondition.** `GET api/file` returns content and
a strong file version. `PUT api/file` requires `baseVersion` and returns
`savedVersion` plus exactly the confirmed persisted bytes. Missing base version
returns `428`; divergence returns `409`. Neither case changes the file or clears
the client buffer. The version is an opaque authenticated server value bound to
the instance, the absolute canonical path, SHA-256 of the exact file bytes, and the
observed file identity tuple (`st_dev`, `st_ino`, `st_mtime_ns`, `st_ctime_ns`).
It changes when content changes or the addressed object is replaced. A timestamp
or size alone is never a valid version.

**HF-SAVE-002 — Cooperative serialization.** Sessions of the same instance use
the same cooperative per-file lock and revalidate the version immediately
before publication. An external writer that ignores the lock can still race;
ordinary locks and rename do not provide universal compare-and-swap. Observable
external changes must be detected, while the residual race is documented and
tested rather than described as impossible. Authorization requires both write
access to the existing file and the directory access needed for safe
publication. The ability to replace a directory entry does not let Hopper Files
rewrite an existing file that is deliberately non-writable to the service UID.

**HF-NAV-007 — Editable file.** When `GET api/file` returns a text document, it
also reports whether the file is editable and, when it is not, a
machine-readable reason, computed by the same rule that the save path enforces.
A file is editable only when, for the service account at that moment:

- it can write the file and the directory that contains it;
- it owns the file and is a member of the file's group, because HF-SAVE-003
  reproduces owner and group on the temporary file;
- the file is a regular file with exactly one link, without set-user-ID or
  set-group-ID bits, and without extended attributes other than a POSIX access
  ACL;
- the content satisfies HF-EDIT-004 and the text limit of HF-LIMIT-001.

For a file that is not editable, the interface hides the formatting commands,
the Save action, and the `Salvo` indicator, shows `Somente leitura` with the
reason in a tooltip,
accepts no typing in any view, and keeps copy and download. If the permission
changes during an edit, saving fails with a clear message and the buffer is
preserved under HF-SAVE-006.

**HF-SAVE-003 — Atomic publication.** The server creates an unpredictable,
exclusive temporary file on the target filesystem, writes the intended bytes,
calls `fsync` on the file when supported, and snapshots the destination's UID,
GID, permission bits excluding set-user-ID and set-group-ID, and POSIX access
ACL. It establishes and verifies that exact access policy on the temporary file
before atomically replacing the destination, then calls `fsync` on the
directory for durability. The rewritten file receives fresh content timestamps;
timestamps are not copied as if they controlled access. A destination carrying
capabilities, set-ID bits, or extended or security metadata outside the
implementation's explicitly supported preservation set is not rewritten: the
server reports the unsupported condition before publication rather than
copying, dropping, or interpreting that metadata blindly. Inability to read,
establish, or verify the supported access policy likewise fails before
publication. Every such failure leaves the prior file and its access intact and
removes only the server's own temporary object.

**HF-SAVE-004 — Post-publication uncertainty.** A failure after replacement,
including directory-sync failure, may mean that content was published or that
durability is uncertain. The server MUST NOT report definitive success or
perform a blind rollback. It re-reads version/content where possible, preserves
the user's buffer, and reports the indeterminate state for resolution.

**HF-SAVE-005 — Confirmed baseline.** The client baseline represents only bytes
confirmed by the server. Starting a save captures the submitted snapshot and
version. Typing while the request is in flight continues in the current
document. A successful response updates the baseline to the saved snapshot and
leaves the document dirty when current bytes differ. It never overwrites later
typing.

**HF-SAVE-006 — Ordered responses and exit guard.** A late response from an
older request cannot reset a newer baseline. Save-button state, tab/window exit
guards, and dirty indication use the same baseline comparison in CodeMirror and
the plain text editor. Conflict, space exhaustion, and I/O failure preserve
text, selection, and an option to copy or download the buffer.

## 10. Images and portable references

**HF-IMG-001 — Managed image types and location.** Button selection, paste, and
drop accept PNG, JPEG, GIF, and WebP. SVG is excluded from managed inline image
insertion. A suggested name is
`img-<note-id>-YYYYMMDD-HHMMSS.ext`, using the instance time zone, under the
visible `attachments/` directory beside the note, in the note's own directory.

**HF-IMG-002 — Relative Markdown references.** An inserted image uses an
ordinary percent-escaped relative Markdown reference
`![](relative-path)`, resolved from the note directory. No proprietary link
syntax is introduced. A relative reference may contain `..`; it resolves to the
absolute canonical path obtained by normalizing it against the note's
directory. Absolute and double-encoded references, targets reached through a
symbolic link, and targets that are not regular files the account can read are
rejected. This rule for Markdown source does not weaken the address rule in
HF-NAV-005.

**HF-IMG-003 — Reference identity.** A reference identity is the target's
absolute canonical path, with device/inode used only as an additional alias
hint. Different permitted relative or percent-encoded forms
that resolve to the same authorized file count as the same reference. A
journaled application move remaps the old and new addresses as one logical
image identity for that operation; its planned URL rewrite is not a removed
reference merely because the Markdown destination text changed.

**HF-IMG-004 — Collection trigger and scope.** After a confirmed note save
removes a previously saved image reference, the service checks the note corpus
of HF-NAV-004 under the limits of HF-NAV-011. It does not scan continuously.
Only after proving that no saved reference remains may it move the image to
trash. The candidate set contains only images whose previously saved reference
was actually removed, excluding identity-preserving rewrites from a move or
rename; it is not a global sweep of unreferenced attachments. A reference from a
note outside the note corpus is not counted, and the instance's internal trash
is never read.

**HF-IMG-005 — Conservative completeness.** The check reads Markdown larger
than the editor limit to completion as a stream. File size by itself never
makes the result inconclusive. A traversal stop under HF-NAV-011, an incomplete
or failed read, a parse error, a permission error on a corpus file, an
ambiguous identity, a content race, or an enforced scanner limit makes the
result inconclusive and preserves the image.

**HF-IMG-006 — Coordination.** Saves, managed uploads, and collection decisions
are serialized sufficiently to prevent the application from deleting an image
while a confirmed application operation is establishing its reference. This is
a cooperative guarantee within the instance, not control over arbitrary
external writers. The same coordination covers a path-changing move and all of
its planned reference rewrites; collection observes either the complete old
mapping or the recovered new mapping, never a textual intermediate state.

**HF-IMG-007 — Pending uploads.** An image that has been uploaded and inserted
in an editor but whose Markdown reference has not been confirmed by a save is
pending and protected. Cancel, save failure, or connection loss does not make it
an automatic collection candidate. The UI shows the pending state and resolves
it only through a confirmed save or a later explicit action.

**HF-IMG-008 — Restoration.** Restoring a note checks for corresponding images
in trash and offers joint restoration. A destination conflict is reported; no
reference is rewritten and no existing image is overwritten silently.

**HF-IMG-009 — Automatic normal flow.** Safe collection is triggered by the
relevant confirmed operations. Manual attachment cleanup is not required as the
normal workflow and no continuous whole-tree scan is introduced.

## 11. Trash and retention

**HF-TRASH-001 — Per-instance storage.** Trash is private per instance under
`STATE_DIR/trash/<uuid>/`, with `payload` and protected versioned `meta.json`.
Version 2 metadata contains
`{version,id,sourcePath,deletedAt,kind,size,reason,uiMetadata}`, where
`sourcePath` is the absolute canonical address of the deleted item.
`uiMetadata` is null when no mark exists, otherwise an identity-scoped list of
`{relativePath,labelIds,favorite,emoji}` records for the item and marked
descendants. `deletedAt` is server-generated UTC and is never accepted from the
client. Version 1 metadata, which recorded `sourceRootId`, is converted or kept
quarantined by HF-NAV-012.

**HF-TRASH-002 — Scope.** Trash is outside the note corpus, the tag index, and
attachment-reference scans (HF-NAV-004, HF-NAV-011); search may list it like any
readable directory. Invalid, incomplete, tampered, or unconvertible metadata
causes logical quarantine and blocks automatic restore and purge.

**HF-TRASH-003 — Retention.** Valid trash entries are retained for at least 30
days. Permanent deletion runs only through a documented local command, which
selects by itself the entries whose 30 days have elapsed by comparing the server
clock with protected metadata. The product installs no schedule for that
command; the administrator runs it or schedules it. Version 1 offers no button
for early permanent deletion. A legitimate entry remains governed by its
protected deletion timestamp.

**HF-TRASH-004 — Restore conflict.** Restore to a free original destination
uses that path. An occupied destination returns `409` and offers an explicit
alternative name or destination directory. It never overwrites the occupant or
removes the trash payload before successful completion. If the original
directory no longer exists or is not writable, restoration requires an explicit
destination and never guesses one. Existing active UI metadata at a proposed
alternative address is also a conflict rather than something to overwrite or
merge silently.

**HF-TRASH-005 — Cross-filesystem deletion.** When origin and trash are on
different filesystems (`EXDEV`), deletion copies to an exclusive trash staging
location, verifies bytes and the same preserved move-access metadata defined in
HF-FILE-002, syncs, publishes a recoverable entry with a state journal, and only
then removes the source. Capabilities, set-ID bits, or unsupported extended or
security metadata make a cross-filesystem deletion unsupported before
publication rather than being copied or discarded blindly. A same-filesystem
trash rename retains the existing inode and metadata. A regular file with more
than one link is separated from its other names as HF-FILE-002 defines. The
integrity rules above are a technical limit of reproducing an object as the
service account: an item on another filesystem whose owner, group, set-ID bits,
capabilities, or extended metadata the account cannot reproduce cannot move to
trash, and because version 1 has no permanent deletion in the interface, such an
item cannot be deleted through Hopper Files. Public documentation states this
limit. Failure during copy
leaves the source. Failure between publication and source removal may leave two
copies; recovery reconciles them without deleting both. The same durable
transaction transfers UI metadata from active state into `meta.json`. Recovery
exposes the marks as belonging to exactly one logical state, active or trashed,
and never loses them because filesystem publication and UI-state publication
completed in different steps.

**HF-TRASH-006 — Cross-filesystem restore.** Restoration across filesystems uses
the reverse order: exclusive staging at the destination, validation and sync,
exclusive publication, then removal of the trash payload. It restores and
verifies the same preserved access metadata or fails before publication; it
never falls back to the destination's new-object policy for an existing trash
item. Space is reserved as far as the platform allows. A directory is one
logical trash item with all children; a failure preserves a recoverable copy
and reports any partial state. The restore journal binds saved UI metadata to
the actual restored address and publishes that active state before removing its
trash copy. Recovery never assigns the marks to an unrelated occupant or relies
on inode reuse.

## 12. Resource limits and security

### 12.1 Initial limits

**HF-LIMIT-001 — Request and viewer limits.** Initial limits are:

- upload: 20 files and 400 MiB total per request, 200 MiB per file;
- text editing and direct text reading: 5 MiB per file;
- inline raster image: 20 MiB;
- inline PDF: 100 MiB;
- search: 10 seconds per initial traversal, 10,000 materialized results, and
  64 MiB of serialized cursor snapshot state;
- tag index, note discovery, and attachment-reference scans (HF-NAV-011): the
  same 10-second execution budget per traversal, after which the result is
  reported as incomplete;
- ZIP source set, and a directory that is copied, moved, or renamed, even
  within one filesystem: 3 GiB, 10,000 entries, depth 32, holding only
  directories and regular files the account can read;
- a file that is moved or renamed: 3 GiB;
- ZIP extraction: 10,000 entries, 3 GiB uncompressed, 200 MiB per entry,
  depth 32, and expansion ratio 120:1.

These are safety limits, not performance claims. Files beyond inline limits
remain downloadable when ordinary file authorization permits. Release
validation measures resource use before any limit is changed.

**HF-LIMIT-002 — Streaming and capacity.** Large upload, download, ZIP, and
cross-filesystem operations stream data and check quota and free space. The
server rejects before commit when possible and never truncates existing data to
make space.

### 12.2 Server-side enforcement

**HF-SEC-001 — Server authority.** Authentication, authorization, address
validation and resolution, object type, size, count, and archive limits are enforced by the
server independently of the browser.

**HF-SEC-002 — Untrusted content.** The Markdown renderer is sanitized. HTML,
SVG, and other active formats are served inertly or as downloads. MIME sniffing
and `Content-Disposition` policy must prevent an untrusted document from
executing with the application's origin. Every string obtained from the
filesystem, configuration, UI metadata, indexing, or an error is untrusted,
including filenames, paths, symbolic-link targets, Markdown-derived titles, tags, label
names, emoji, status and error text, and search snippets. UI surfaces insert
these values as text nodes or apply escaping for the exact output context; they
never interpret them as HTML. Only the Markdown rendering surface accepts
markup, through its reviewed sanitizer. Any untrusted value used in a URL or
attribute is separately validated for its context. Clickable ordinary links
accept only `https`, `http`, and `mailto` schemes; relative URLs are accepted
only where another requirement defines their authorized resolution.
Event-handler attributes, executable or unknown schemes, and markup-producing
string concatenation are forbidden.

**HF-SEC-003 — Failure policy.** Validation, authorization, parse, I/O, and
concurrency failures do not silently become overwrite, deletion, replacement,
or collection. Error responses are clear enough to distinguish no commit,
partial commit, conflict, and indeterminate post-publication state without
exposing another account's data.

**HF-SEC-004 — External modification boundary.** Hopper Files coordinates its
own processes and detects observable external changes, but cannot guarantee
atomic exclusion of writers that ignore its locks. Documentation and tests
state this residual Linux filesystem limitation explicitly.

**HF-NAV-009 — No protected namespaces.** Hopper Files has no protected
namespaces, no `protectedPaths` configuration, and no hidden, read-only, or
immutable areas of its own. What the service account can read is listed and
opens; what it can write can be changed. This includes SSH keys, credentials,
`.git`, `.env`, instance configuration readable by the account, and the
instance's own state directory. An authenticated session therefore has the
file access of a terminal of the service account, and the web login is the only
application barrier in front of it; deployment documentation states this
consequence. Address validation (HF-NAV-005), race-resistant resolution
(HF-API-007), and atomic saving (HF-SAVE-003) remain. Dedicated state and trash
endpoints still access their own instance stores only through validated logical
IDs and defined operations, never through a client-supplied filesystem path.
Hopper Files provides no content-based secret detection or DLP.

## 13. Acceptance and validation contract

This section defines evidence required for acceptance; recorded results and
their scope are maintained separately in the
[public validation record](validation.md). Passing results apply only to the
evidence and limits stated there. Tests use only synthetic data and must
contain assertions capable of failing when a requirement is violated.

### 13.1 Installation and isolation

**HF-ACC-001.** Install, register, update, rollback, remove, and uninstall on the
Debian 13 reference system from a clean, identified product source and pinned
official dependencies. Verify that no private/site tree or prior installation
is required, rollback changes the release but not documents or state except
through the migration return of HF-NAV-012, and default removal preserves
both.

**HF-ACC-002.** Register two existing Linux accounts with independent instances.
Replay credentials, cookies, CSRF tokens, and URLs across instances and ports;
every replay must fail without effect. Logout and expiry affect only the
originating instance. With no session, an invalid session, or an expired
session, requests to list, file, raw content, search, state, tags, trash,
directory size, and every mutation must be denied without returning document
content or causing an effect. The instance's own login flow and an optional
public landing page do not grant document access. With synthetic credentials,
verify that equal passwords produce different salts and digests, no store or log
contains the password, only the specified version and scrypt parameters are
accepted, over-1,024-byte input is rejected before hashing, wrong, absent, and
malformed credentials fail generically, and only one KDF runs per instance.
Exhaust both the 5/60-second network-origin bucket and the 20/15-second global
instance bucket; verify generic `429` plus `Retry-After`, persistence across
restart, rejection of spoofed origin headers, and isolation between instances. A
password change from the login page must require the current password, spend
the login nonce and limits, and refuse a mismatched confirmation before any
KDF. Both it and the local administrative password change must use a fresh
verifier and invalidate all of that instance's sessions and CSRF tokens without
exposing the secret.

**HF-ACC-026.** Inspect the effective units of both instances: the registered
account, `NoNewPrivileges=yes`, empty capability sets, `UMask=0002`, and no
`ProtectSystem`, `ReadWritePaths`, `ReadOnlyPaths`, `InaccessiblePaths`, or
`PrivateTmp`. From the service, `/tmp` and `/var/tmp` are the terminal's, writes
succeed exactly where the account's Linux permissions allow, `sudo` and set-ID
escalation are unavailable, and releases, dependencies, the active selector,
and root-owned administrative configuration remain unmodifiable. Confirm that
the configured memory limit permits one specified password verification without
allowing concurrent per-instance KDFs.

**HF-ACC-027.** With synthetic copies of version 1 state, trash, and
configuration for both accounts, including favorites, labels, emoji, tabs,
ignored tags, trash items from each former root and from the System view, a
retired root ID, records that map to the same absolute path, terminal operation
results, and search cursors: migrate, verify every converted, merged, orphaned,
and expired record and the report, verify idempotence, and use the migrated
instance. A pending journal of each kind in HF-NAV-012 blocks migration and
return with a report and without change. The new release refuses to serve an
instance left at version 1 without creating empty state, and a conversion
failure on one instance leaves both on the previous release. Create new marks, tabs, and trash
items inside and outside the former roots, roll back to the previous release,
and verify that it starts with its restored configuration and unit, maps a path
inside a former document root to that root even where the System view overlaps
it, shows every representable record, and keeps every unrepresentable record,
including one at a formerly protected path, in the report and the kept version 2
documents, while unrepresentable trash items remain
quarantined rather than purged. Migrate again and verify that no record is lost
or duplicated.

**HF-ACC-004.** Exercise explicit local and remote modes. Non-loopback local
access, wrong Host, wrong Origin, cross-origin POST including another port on
the same host, untrusted forwarded headers, and direct remote-mode loopback
session use must fail. Remote cookies must always carry `Secure`; no request may
trigger a downgrade. The service listener must not be network-public.

### 13.2 Filesystem navigation and paths

**HF-ACC-022.** Run the candidate as each real service account, with synthetic
fixtures wherever a write is needed. `/` starts collapsed; one activation shows
the account's complete first level, including `dev`, `proc`, and `sys`, dotted
names, and the symbolic links `bin`, `lib`, `lib64`, and `sbin` with their
targets; activating `bin` opens `/usr/bin`, and the address becomes the real
path. Directories the account can see but not open, such as `/root`,
`/lost+found`, and the other account's home, are dimmed and do not open. Dotted
entries such as `.ssh`, `.config`, and `.cache` in the home and `.pwd.lock` in
`/etc` appear. The release directory and a project directory appear and open
with the account's actual write permission. Devices, sockets, and FIFOs
are listed and do not open. Leftover `.hopper-stage-*` names are listed. A name
that is not valid UTF-8 is listed with its lossless `\xHH` display, dimmed,
without address, operations, or opening, and with the reason in its tooltip.
There is no root selector or root row, and collapsing `Arquivos & Pastas` hides
the whole tree. After start, open a folder from the listing, from the
breadcrumb, and through a link, and open a document from a search result:
each time the tree shows the ancestors expanded and the current item marked and
visible. Reload the same folder after collapsing one of its ancestors: the
branch stays collapsed. In a path wider than the breadcrumb, the current item
is visible. Every listing has the same entries as the same account's
terminal listing.

**HF-ACC-023.** Test plain and encoded `..`, relative, double-encoded,
trailing-separator, NUL, and empty-component addresses; addresses through
symbolic links in final and intermediate components; concurrent replacement of
a component by a link; special files; broken links; and a regular file with two
links. Invalid forms fail without effect; link components are rejected; the
real path of a link destination is accepted. The multiply linked file is listed,
readable, and searchable, is not editable, and a cross-filesystem move or trash
separates its link, keeps the other name on the original inode, reports the
separation, and gives the separated file exactly the source UID, GID, mode, and
access ACL. Search, the tag index, and attachment scans never enter `/proc`,
`/sys`, `/dev`, or symbolic links; the tag index and attachment scans also never
enter the internal trash, which search may list, and the tag index enters
hidden directories, `/tmp`, and `/var/tmp` only when they are marked. Manual
navigation lists all of them. Measure traversal time and counts for each real account, including
`/tmp` and `/var/tmp`, and verify truthful `complete` and omission reporting
when a budget stops a traversal. While a tag index or search request is still
running, a directory listing answers without waiting for it.

**HF-ACC-024.** In each account's writable directories, including places outside
the former roots such as the service account's home directory and `/tmp` for
both accounts, create, rename, move, copy, upload, extract, save, tag, and move to
trash succeed. Outside writable directories the same actions fail with the
permission message and no effect on disk. A file and a directory created by the
application in `/tmp`, in a set-group-ID directory, and in a directory with a
default POSIX ACL have the same owner, group, mode, and ACL as objects created
there by the same account's terminal with `umask 0002`. Upload, copy, and
extraction import no owner, group, mode, ACL, set-ID, or capability metadata
from their input. With no monitored folder, Tags is empty. After the home
directory is marked, a note containing `#tag` in it appears in Tags, including a
note whose file name starts with `.`, while a note in a hidden subdirectory
does not until that subdirectory is marked. After `/var` is marked, a note in
`/var/tmp` does not appear until `/var/tmp` is marked. A deleted note does not
return to the index from the internal trash, even if the trash is marked.

### 13.3 File operations and limits

**HF-ACC-009.** Verify special names, collision suffixes, exclusive create,
upload original/generated naming choice, download, copy/move across directories
and filesystems, batch
failure on a later item, ZIP temporary-name collision, hostile archive paths,
duplicate entries, count/depth/size/ratio limits, and out-of-space handling.
Under a directory with a default ACL and under a set-group-ID directory, verify
that note creation, upload, ordinary copy, and extraction produce the access
defined by HF-NAV-010 and import no owner, group, mode, ACL, set-ID, or
capability metadata from their input.
Exact technical names and original upload names remain unchanged; their
collisions return `409` until the user supplies an alternative, while only
generated names receive suffixes. Committed, uncommitted, and indeterminate
items must match the response. Replay the same operation token and fingerprint
before and after a partial response: already committed items are not duplicated
and only items proved unpublished may resume. A changed payload with the same
token returns `409`. Inject a crash after exclusive publication but before its
completion record and verify recovery uses the recorded identity and digest
rather than publishing again. Verify terminal-result retention, status of a
pending operation after expiry, rejection of expired mutation before and after
terminal-result garbage collection, and the per-instance active-operation cap.

**HF-ACC-010.** Exercise every boundary in HF-LIMIT-001 and one value above it.
Measure memory, disk, time, and response with representative synthetic trees;
do not claim performance from configuration or unit tests alone.

### 13.4 Editor and concurrent saving

**HF-ACC-011.** Open one file in two sessions at the same version. The first save
succeeds; the second returns `409`, leaves the file unchanged, and preserves its
buffer. An observable external edit before save is also detected. Repeat with
two sequential publications of different content having the same byte length
and forced identical modification timestamps, and with object replacement
containing the same bytes. Content or identity change must produce a different
opaque version; `mtime` and size alone must not pass the precondition.
Rewrite existing `0600` and `0640` files and a file with a group POSIX access
ACL; each successful save must preserve UID, GID, permission bits, and access
ACL exactly while updating content timestamps. Make the file non-writable to
the service UID while its directory remains replaceable, and inject inability
to read, set, or verify each supported access field: every case must refuse
before publication and preserve the original. Files with set-ID bits,
capabilities, or unsupported extended/security metadata must likewise remain
unchanged with a clear unsupported result.

**HF-ACC-012.** Induce an external write in the final validation/publication
window and record the outcome without claiming universal exclusion. Verify a
failure before replacement keeps the prior file, while failure after replacement
reports indeterminate state, re-reads where possible, preserves the buffer, and
does not blindly roll back.

**HF-ACC-013.** Type while a save is in flight in both CodeMirror and the plain
text editor. After the response, later typing remains, the document is dirty,
Save remains enabled, and the exit guard remains active. Deliver responses out
of order and verify an older response cannot reset the newer baseline.

**HF-ACC-014.** Refresh listings, change tabs, and restore a connection while a
buffer is dirty; buffer, selection, and dirty state remain. Validate the 5 MiB
editor boundary, invalid UTF-8 behavior, Markdown commands, list indentation,
the three views of HF-NAV-008, clean-copy output, and editor-local undo/redo. Clean-copy and
plain-text output must match the normative fixtures in HF-EDIT-003 exactly,
including preserved fenced-code content, list markers, literal Markdown in
`.txt`, and the short two-line non-join case. Verify highlight toggling per line,
inline formatting that leaves heading/list/task prefixes outside its markers,
bullet insertion and replacement without duplicate markers, raw Enter after a
bullet, and the coarse-pointer `Digitar` focus behavior in HF-EDIT-002. Open
`ui-state.json` revision R in two sessions. For both different-key and same-key
edits, the first `PUT` must commit R+1 and the stale second `PUT` must return
`409`, preserve both server and local changes, and offer reload or explicit
resolution; omission of `baseRevision` returns `428`. Interleave stale writes
with move, delete, restore, and recovery state updates and verify each internal
commit increments the durable revision and cannot be reintroduced or erased by
the stale full-state payload. The schema remains at `version: 2`, and tabs never
acquire dirty document content.

**HF-ACC-028.** With synthetic files changed by another process, create, rename,
delete, and modify entries in the open folder, in an open tree branch, and next
to open documents; write a file through a temporary name and a rename; and write
100 files at once; delete the open folder, and its parent, and create them again
after 1 second and after more than one change request. The listing and the tree
follow within 2 seconds with scroll, focus, and still-existing marked items
unchanged and a few reads per burst.
Verify HF-FILE-006 for a document without edits, with unsaved edits, after the
user's own save, and after removal and rename, and for an image and a PDF.
Verify that a hidden tab makes no change request and that its return reads
again what it shows. Verify HF-API-008 for a missing session, a wrong CSRF
token, invalid bodies, the answer within 1 second after a change, the empty
answer within 25 seconds, the release of unnamed directories, the watch limit, a
server restart, and a notice queue overflow.

**HF-ACC-025.** Open a readable Markdown file the service account does not own
and cannot write: it is reported as not editable with a reason, shows `Somente leitura` without `Salvo`, has no formatting commands
or Save, and no view accepts typing. Cover each reason of HF-NAV-007: no write
permission on the file or its directory, another owner, a group of which the
account is not a member, more than one link, set-ID bits, unsupported extended
attributes, invalid UTF-8, and a file above the text limit. In an owned,
writable `.md`: it opens in `Formatado`; `Formatado` and `Limpo` accept no
typing and their editor bar offers the switch to `Markdown`; typing in
`Markdown` marks `• Não salvo`; saving returns to `Salvo`; the three views show
the same buffer, and `Formatado` renders an unsaved edit. A note created with
New note opens in `Markdown`, ready for typing. A
writable `.txt` opens without the view control and accepts editing. Revoke
write permission during an edit: saving fails clearly and preserves the
buffer.

### 13.5 Images and trash

**HF-ACC-015.** Reference one image from notes in several directories using
permitted relative and percent-escaped aliases. Any saved reference in the note
corpus preserves the image. A reference from a note outside the note corpus is
not counted. Move and rename a note, a referenced image, and directories
containing each, within a directory tree and across directories and filesystems.
Every rewritten destination must resolve to the planned new address of the same
logical image, including references using `..`; unrelated Markdown remains
byte-identical and no identity-preserving rewrite triggers collection. Include
references from multiple notes and affected clean and dirty open buffers. A
clean affected buffer must reload at the rewritten version after success. A
dirty or changed affected buffer, an incomplete required scan, version change,
or injected failure before publication must reject the operation without
publishing any move or rewrite. Inject interruption during publication and
verify journal recovery yields one coherent mapping without silent rollback.

**HF-ACC-016.** Remove the last saved reference and verify collection occurs
only for that candidate image and only after a complete scan under HF-NAV-011,
without a budget stop, read error, or race. Include Markdown files from 2
through 5 MiB and above 5 MiB; size alone must not prevent collection when each
file is read to completion. For a file above 5 MiB, test both a completed stream
and an injected scanner-limit failure. An incomplete or failed stream,
unreadable file, parse failure, traversal stop, enforced scanner limit,
ambiguous alias, or content race makes the check inconclusive and preserves the
image.

**HF-ACC-017.** Upload and insert an image without completing the note save,
then cancel, fail the save, and disconnect. The image remains protected and its
pending state is visible. Joint note/image restore never overwrites a target or
silently rewrites a reference.

**HF-ACC-018.** Delete and restore both a file and a directory when the origin
and the trash are on the same filesystem and on different filesystems,
including `/tmp` on `tmpfs`. Inject failures
during copy, publication, source removal, and restore. At least one recoverable
copy remains and the reported state is truthful. For ordinary cross-filesystem
copy and move, verify staging is fully checked and synced before exclusive
publication, destination publication is durably journaled before source
removal, copy never removes the source, and recovery from every boundary never
deletes both copies. Verify that ordinary copy applies destination access policy,
same-filesystem move/trash retains the inode metadata, and cross-filesystem
move/trash/restore exactly preserves the supported UID, GID, mode, access ACL,
and directory default ACL. Unsupported set-ID, capability, or extended/security
metadata must refuse a cross-filesystem publication while preserving the
source or recoverable trash item.

**HF-ACC-019.** Verify restore before 30 days, destination collision with an
explicit alternative name, no early permanent-delete UI, purge only after 30
days using protected server time, instance isolation, invalid metadata
quarantine, and restoration when the original directory is missing or not
writable.
Delete favorite and labeled files and an emoji-marked directory containing
marked descendants: active marks must leave with each item, new items at the
same paths must inherit none, and restore must return the saved marks at the
original or explicit alternative base address. Test inode reuse, an
alternative-address metadata conflict, and failures between payload, trash
metadata, and UI-state publication; recovery must neither lose marks nor attach
them to the wrong object.

### 13.6 Rendering and interface

**HF-ACC-020.** Attempt active Markdown, HTML, SVG, misleading MIME types, and
download filenames. No content executes in the application origin, and safe
ordinary previews and downloads remain usable. Put distinct hostile strings in
filenames, directory names, paths, symbolic-link targets, H1-derived titles, tags, label
names, emoji, errors, status text, and search snippets, then exercise tree,
listing, breadcrumb, tabs, search, tag and label views, titles, dialogs, and
notifications. Every string must appear as literal text where displayed and
none may create markup, an event handler, or script execution. Test URL and
attribute contexts with executable and disallowed schemes; only explicitly
allowed, validated schemes may become links.

**HF-ACC-021.** Validate desktop, 720 px transition, phone-sized, and tablet-sized
layouts with keyboard and touch input. Check sidebar and split behavior,
overflow, focus, contrast, reduced motion, Portuguese UI labels, and absence of
all features excluded by HF-SCOPE-008. In `aqui` mode, compare NFC-equivalent
names with case differences, immediate files and directories, and descendants.
Only matching immediate children of the open directory appear;
changing directories clears the filter and makes no search API request.
For `api/search`, use a scope directory with fixtures covering recursive files
and directories, NFC/NFD and case equivalents, an accent-sensitive negative,
literal regex/glob-like characters, a dotfile, an unreadable subdirectory, and
a symbolic link to a directory. For `text`, cover valid UTF-8 at
exactly 5 MiB, a larger file, invalid UTF-8, NUL, symlink and special files, a
regular file with two links, which is searched, a match split by a newline, and
multiple matches. A `name` search returns a matching symbolic link without
following it. Verify one
lowest-line match per file, bounded literal snippets, deterministic path
ordering, categorized omissions/exclusions, and `complete` truthfulness. Page
at both 100 and 200 items without duplicates or omissions; changed parameters,
expiry, an observable mutation between pages, and an execution-budget stop must
produce the defined `400`, `410`, `409`, or incomplete result. None of these
requests changes the separate `aqui` behavior.

## 14. Completion conditions

**HF-DONE-001 — Implementation evidence.** Version 1 is not complete until its
own code has been reviewed, the acceptance contract above has been executed on
synthetic data, failures have been resolved or explicitly bounded, and results
are recorded without treating structural configuration as runtime proof.

**HF-DONE-002 — Operational evidence.** Release startup after reboot, update,
rollback, state migration and its rollback, instance isolation, HTTPS remote
access when configured, and sample recovery are demonstrated in the target
environment. Backup coverage is checked against existing site operations; Hopper
Files does not install an unrequested parallel backup, alerting,
synchronization, or monitoring system.

**HF-DONE-003 — Documentation.** Public installation, instance configuration,
update, rollback, uninstall, recovery, and security-boundary
documentation must match the released behavior. Public development and
contribution documentation is written in English and is sufficient to build
and test from the published product sources without a private site tree.
Site-specific credentials, hosts, account names, private audit evidence, and
personal paths do not belong in public product documentation.

## 15. Retired identifiers

An earlier revision of this specification replaced configured document roots,
the optional System view, and application protected namespaces with one
filesystem explorer from `/` that is limited only by the Linux permissions of
each instance's service account (HF-NAV-001 through HF-NAV-012). The identifiers
below belong to that root-based model and remain retired. They are never
reused; the replacement column points to the rules that now cover their
subject.

| Retired | Former subject | Replaced by |
| --- | --- | --- |
| HF-ROOT-001 | Root collection | HF-NAV-001 |
| HF-ROOT-002 | Root identity | HF-NAV-005 |
| HF-ROOT-003 | Root topology | HF-NAV-001 |
| HF-ROOT-004 | Linux permissions and creation policy | HF-NAV-001, HF-NAV-003, HF-NAV-010 |
| HF-ROOT-005 | Unavailable and removed roots | HF-NAV-012 (unmapped records) |
| HF-ROOT-006 | Root configuration generations | None; there is no root set |
| HF-SYS-001 | Optional System view | HF-NAV-001, HF-NAV-002 |
| HF-SYS-002 | System view semantics | HF-NAV-004 |
| HF-SYS-003 | System and root overlap | None; there is one base |
| HF-SYS-004 | Protected paths in the System view | HF-NAV-009 |
| HF-SYS-005 | System safe-trash subpaths | HF-NAV-003, HF-NAV-004 |
| HF-INST-006 | `systemd` confinement | HF-NAV-010 |
| HF-API-001 | Root-relative address | HF-NAV-005 |
| HF-API-002 | Root discovery (`GET api/roots`) | None; the route is removed |
| HF-SEC-005 | Protected namespaces and hard-link omission | HF-NAV-009, HF-NAV-002, HF-NAV-007 |
| HF-ACC-003 | Sandbox acceptance | HF-ACC-026 |
| HF-ACC-005 | Root configuration acceptance | HF-ACC-022 |
| HF-ACC-006 | Root lifecycle acceptance | HF-ACC-027 |
| HF-ACC-007 | Path and protected-namespace acceptance | HF-ACC-023 |
| HF-ACC-008 | System view acceptance | HF-ACC-022, HF-ACC-024 |

