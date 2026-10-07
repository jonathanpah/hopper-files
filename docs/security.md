# Security boundaries and limits

The server, not the browser, decides authentication, address validation and
resolution, object type, and the size, count, and archive limits in the
specification. This note states the boundary an operator can rely on and the
limits the recorded checks do not close. It does not add a requirement.

## Process boundary

Each instance is one process running as its existing service account. The
process has no privilege escalation. It does not call `sudo`, does not create
Linux accounts, and does not widen document permissions. Lifecycle commands
that change the host are a separate root command, `hopper-files-admin`. They
are not exposed to the browser.

The systemd unit runs as the registered account with `NoNewPrivileges=yes`,
empty capability sets, and `UMask=0002`. It sets no filesystem restriction
(`ProtectSystem`, `ReadWritePaths`, `ReadOnlyPaths`, `InaccessiblePaths`,
`PrivateTmp`): the Linux permissions of the account decide every access, and
`/tmp` and `/var/tmp` are those of the account's terminal. Releases,
dependencies, the active selector, and administrative configuration are
protected by root ownership and modes. Rendering the unit text does not prove
what a host has loaded; `systemctl show` shows the effective unit.

The instance listens only on a loopback address. In local mode it accepts only
the exact `Host` and `Origin` of its `baseUrl`. In remote mode it accepts only
requests that come from `trustedProxyPeer` with the forwarded headers
described in
[remote access](operations.md#remote-access-behind-an-https-proxy); every
other request, including a direct one from the server, gets `403`.

Cookies are `HttpOnly` and `SameSite=Strict`, scoped to the instance base
path, with no `Domain`. Remote mode always sets `Secure`. Local mode omits
`Secure` only for its configured loopback origin. A forwarded protocol does
not downgrade that. Another instance's cookie, even a valid one, is rejected.
Login uses a pre-authentication nonce and two persistent token buckets. The
password change page linked from the login page uses the same nonce and
buckets and requires the current password; it ends every session of the
instance. Password verifiers are scrypt records with the parameters fixed in
the specification. The password is not an argument, a log field, or a session
value. Identity headers added by a proxy never replace the instance password.

## Filesystem boundary

Every instance navigates the filesystem from `/` with its service account's
Linux permissions. Hopper Files has no protected namespaces and no hidden,
read-only, or immutable areas of its own: what the account can read is listed
and opens, and what it can write can be changed, including SSH keys,
credentials, `.git`, `.env`, the instance configuration when its mode allows,
and the instance's own state directory. An authenticated session therefore has
the file access of a terminal of that account, and the web login is the only
application barrier in front of it. Choose the service account for that
consequence. Hopper Files provides no content-based secret detection.

An address is the absolute canonical path of the object; the API carries it
as the fixed identifier `fs` plus the path relative to `/`. The server walks
directory descriptors from `/`, rejects an address whose resolution meets a
symbolic link, and acts on the object the walk opened. A lexical prefix is not
authorization. The state and trash endpoints reach their own instance stores
only through validated logical identifiers, never through a client path.

New files and directories receive what a terminal of the same account would
create in that directory under `UMask=0002`: the account's owner, the group a
set-group-ID parent imposes, and the entries of a default POSIX ACL. Copy,
upload, and extraction do not import ownership, mode, ACL, set-ID, or
capability bits from the source. A cross-filesystem move or trash preserves
the source UID, GID, permission bits, access ACL, and directory default ACL
exactly, or stops before publication when it cannot, or when it finds set-ID
bits, capabilities, or unsupported security metadata. A save rewrites only a
file the account owns, whose group it belongs to, with one link, no set-ID
bit, and no extended attribute other than a POSIX access ACL.

Bounded traversals (search, the tag index, and attachment-reference scans)
never enter `/proc`, `/sys`, `/dev`, or a symbolic link. Reference scans read
only notes in directories the account can write. The tag index reads only the
folders the account marks as monitored, including notes it can read but not
write. Neither reads the internal trash, and what they derive is kept in a
private derived index that is never used to authorize an access.

## Failure behavior

Validation, authorization, parse, I/O, and conflict failures are not turned
into a silent overwrite or delete. A partial file operation reports committed,
uncommitted, and indeterminate items. A failure after a save has replaced the
destination does not roll back blindly and does not report definite success.
Trash quarantine blocks automatic restore and purge of that entry. Details
are in [operations](operations.md#recovery).

Untrusted filenames, labels, errors, and search snippets are inserted as
text. Markdown rendering is sanitized. HTML and SVG are not active previews.
Ordinary links accept only the schemes the specification allows.

## Limits that remain open

These limits are explicit. They are not a reason to weaken the checks above
or to treat unfinished evidence as a passed acceptance test. The
[README](../README.md#known-limitations) lists the functional limitations a
user meets.

- Interface checks used Chromium and WebKit with emulated phones, tablets,
  and touch input. Physical iPhone- and iPad-class devices, Safari on those
  devices, and Firefox are not validated, so `HF-UI-003` and `HF-ACC-021` are
  not closed.
- Installation, update, rollback, removal, and uninstall were exercised on a
  disposable Debian 13 virtual machine with two accounts; see
  [validation](validation.md). That is not evidence for other distributions,
  for production load, or for a host whose policies differ from a clean
  Debian 13.
- The note corpus is every writable `.md` file the account can discover from
  `/`, and the tag index reads every `.md` file under the monitored folders.
  Each traversal is bounded by 10 seconds; on a large corpus or monitored
  folder the first derivation can stop early and report the tag index or a
  collection check as incomplete until the derived index is complete. A note
  that cannot be parsed keeps every image whose name it contains and blocks
  moving those files.
- Search cursor state is bounded at 64 MiB. The bound was measured with a
  synthetic materializer, not offered as a production latency result.
- A writer that ignores Hopper Files locks can still change a file after the
  last check. The product detects the change it can observe and documents the
  residual race. It does not claim universal exclusion of uncooperative
  Linux writers.
- The service unit does not restart the process after a crash; an
  administrator restarts it.
- Change notices (`POST api/changes`) come from Linux inotify. Changes that
  another machine makes on a network filesystem are not reported and appear
  only when the view is read again. One instance watches at most 4,096
  directories, within the account's `fs.inotify.max_user_watches`.
- Version 1 does not define an additional byte ceiling for `PUT api/state`.
  This release does not add one. Login and logout bodies remain limited to
  64 KiB, and the file-operation JSON body remains limited to 1 MiB, as
  implemented.
- The exclusions in `HF-SCOPE-008` are absent on purpose: no synchronization
  service, AI review, terminal, external alerts, wiki backlinks, Git status,
  hash panel, naming-conformance panel, global filesystem undo, telemetry, or
  internal Office conversion.

Resource ceilings for upload, text, images, PDF, search, and ZIP are the
initial limits in the specification. Files beyond an inline viewer limit
remain downloadable when ordinary authorization allows it.
