# Validation summary

This page summarizes how releases 0.1.0 to 0.1.6 were checked and what
remains open. It does not change the [specification](specification.md). A check listed here
does not by itself pass an acceptance requirement of specification section 13.
All checks used synthetic files and synthetic passwords.

## Release 0.1.6

Release 0.1.6 changes the interface and the sign-in, and keeps its browser
checks in the repository:

- **Sign-in:** the login and password change pages ask for the user name, the
  account the instance runs as, besides the password, with the hints that let
  a browser's password manager fill both. A wrong user name gets the same
  answer as a wrong password, after the same one KDF.
- **Sessions:** a session lasts `sessionDurationSeconds` after the last
  authenticated request instead of after the login: use renews it, at most once
  a minute, and the browser gets the cookie again with the full duration.
  Renewal and revocation share a lock, so a renewal never brings back a session
  that logout or a password change ended.
- **Text contrast:** secondary text, blue text on a highlighted background,
  white text on the blue primary button, the inactive options of segmented
  controls, and the secondary columns of the row a menu belongs to reach 4.5:1
  in both themes.
- **Context menus:** a menu opens beside the pointer or below its button and
  flips up or left where it would not fit, so it does not cover what opened it;
  Tab closes it, focus returns without scrolling, and the row it belongs to
  keeps its highlight under the pointer.
- **Drag and drop:** holding dragged items over a collapsed tree folder for
  0.6 s opens it; a drag that ends without a drop closes it again.
- **Editor:** on a fine pointer, a selection in the Markdown view shows a
  floating formatting bar.
- **Touch:** the tap areas of the first breadcrumb segment, the monitored-folders
  link, the tree tools, the new-tab button, and the sidebar resize handle no
  longer take taps meant for the controls next to them.

The new browser checks also found two older defects, now fixed: a change made
outside the application between a folder's first read and the start of its
watch appeared only with the next change answer, up to 20 seconds later; and a
PDF in a standard font that it does not embed logged a blocked font load (the
page policy now allows fonts from the application itself). DOMPurify is
updated to 3.4.16, which fixes two advisories about a sanitizing mode the
application does not use.

On the development machine, these checks passed:

- the browser suite in `frontend/browser/` in WebKit, 91 tests in 11 files, run
  twice in a row; the checks of the new interface behavior fail on 0.1.5;
- the frontend tests and two identical frontend builds;
- the Python tests, with the private marker list, including the new sign-in,
  session renewal, and repository verifier tests;
- the integration scripts and `scripts/verify-clean-checkout.sh`;
- the repository verifier with the private marker list, on the tree and on
  every commit of the release;
- the dependency advisory check: none for the locked npm and Python
  dependencies;
- two byte-identical builds of the release archive;
- an update from 0.1.5 with `update-release`.

## Release 0.1.5

Release 0.1.5 adds `Ignorar tag` to the context menu of a tag row in the
sidebar, opened by a secondary click, a long press, or the menu key like the
rows of the listing, and leaves `node_modules` directories out of the tag
index below a monitored folder (`dependency_directory`). On the development
machine, these checks passed:

- the frontend tests and two identical frontend builds;
- the Python tests, including the new `node_modules` cases for the tag index,
  search, and the corpus, and the repository verifier;
- the integration scripts and `scripts/verify-clean-checkout.sh`;
- in WebKit on a synthetic instance, with the real server: the menu with a
  mouse, with a keyboard event without a pointer, and with an emulated touch
  long press; the saved state with its base revision; the tag leaving the
  list, the counts, and the open view; a revision conflict; the limit of 256
  ignored tags; restoring in Settings; and the tags of a `node_modules`
  directory absent from the list. The same checks fail on 0.1.4;
- two byte-identical builds of the release archive;
- an update from 0.1.4 with `update-release`.

The test browser does not turn the menu key or Shift+F10 into a menu event,
for the listing either, so the keyboard check dispatches the menu event that
browsers send for those keys (no pointer position).

## Release 0.1.4

Release 0.1.4 fixes the refresh of 0.1.3 when the open folder is deleted and
created again outside the application: the listing now drops the deleted
entries and shows the new content when the folder returns, also when its
parent is deleted and when the folder returns after more than one change
request. On the development machine, these checks passed:

- the frontend tests and two identical frontend builds;
- the Python tests and the repository verifier;
- the integration scripts and `scripts/verify-clean-checkout.sh`;
- in WebKit on a synthetic instance, the checks of 0.1.3 again and the open
  folder deleted and created again after 1 and 25 seconds, and its parent
  deleted and created again; the same folder check fails on 0.1.3;
- two byte-identical builds of the release archive;
- an update from 0.1.3 with `update-release`.

## Release 0.1.3

Release 0.1.3 follows changes made outside the application. The open folder,
open tree branches, open documents, and image and PDF previews update within
about 2 seconds while the browser tab is visible, through the new
`POST api/changes` route and Linux inotify (`HF-API-008`, `HF-FILE-006`). On
the development machine, these checks passed:

- the frontend tests and two identical frontend builds;
- the Python tests, including the change-notice tests, and the repository
  verifier;
- the integration scripts and `scripts/verify-clean-checkout.sh`;
- in WebKit on a synthetic instance, with files changed by another process:
  create, rename, delete, and modify in the open folder and in an open tree
  branch, keeping scroll, focus, and marked items; a burst of 100 files; a
  document without edits, with unsaved edits, after its own save, and after
  removal and rename; an image and a PDF; a hidden tab that makes no request
  and reads again on return; a server restart; and an ended session;
- two byte-identical builds of the release archive;
- an update from 0.1.2 with `update-release`.

The hidden tab was simulated by the test, since the headless browser has no
real hidden tab. Changes made by another machine on a network filesystem are
not reported (see [security](security.md#limits-that-remain-open)).

## Release 0.1.2

Release 0.1.2 adds `Copiar caminho` to the context menu. It copies the absolute
path of a file or directory, or the paths of all checked items, one per line.
A symbolic link, an entry the account cannot open, a special file, and the `/`
node get a menu with only this entry. On the development machine, these checks
passed:

- the frontend tests and two identical frontend builds;
- the Python tests and the repository verifier;
- the integration scripts and `scripts/verify-clean-checkout.sh`;
- the context menu and the copied paths in WebKit, with mouse, keyboard, and
  touch, on a synthetic instance;
- two byte-identical builds of the release archive;
- an update from 0.1.1 with `update-release`.

## Release 0.1.1

Release 0.1.1 changes one text of the interface: the trash hint now says that
an item can be permanently deleted after 30 days, since nothing empties the
trash on its own. On the development machine, these checks passed:

- the frontend tests and two identical frontend builds;
- the Python tests and the repository verifier;
- the integration scripts and `scripts/verify-clean-checkout.sh`;
- two byte-identical builds of the release archive;
- an update from 0.1.0 with `update-release`.

The sections below describe the checks of 0.1.0, which still apply.

## Automated checks

On the development machine:

- **Python tests:** all passed. Two tests skip when their condition is
  missing: one needs root, and one reads a list of site-specific terms named by
  `HOPPER_FILES_FORBIDDEN_MARKERS`. Other tests skip on hosts without POSIX
  ACLs, extended attributes, set-ID bits, `/proc`, inode reuse, or a second
  writable filesystem.
- **Frontend tests:** all passed, with Node.js `v24.21.0` and npm `11.19.0`.
- **Integration scripts:** the five loopback scripts passed.
- **Browser suite:** the WebKit checks in `frontend/browser/` passed (from
  0.1.6 on).
- **Dependency advisories:** `npm audit` and `pip-audit` found none (from 0.1.6
  on).
- **Repository verifier:** passed.
- **Reproducible builds:** two frontend builds from a clean export matched
  each other and the generated files in the tree. Two builds of the release
  archive from one tree were byte-identical, and a build on a separate machine
  matched them.
- **Clean checkout:** `scripts/verify-clean-checkout.sh` passed.

## Installation on a new Debian 13 machine

A new disposable Debian 13 virtual machine followed the
[README](../README.md#install) and [operations](operations.md) as written. It
had an administrator account and a second account without `sudo`.

- **Install:** the release script, two instances (one per account), `check`,
  and the commands of "Check and open".
- **Browser:** in WebKit, through an SSH tunnel, the following worked:
  - sign in;
  - create a folder and a note and save it;
  - open a favorite folder as a tree;
  - move a folder to the trash;
  - change the password from the login page.

  The second account could not open the first account's home, and the page
  did not scroll sideways at a phone size.
- **Everyday administration:** `purge-trash`, `update-instance`, a restart,
  and the launcher in the foreground.
- **Update and roll back:** `update-release`, `rollback-release`, and
  `update-release` again. Each time, both instances answered within seconds.
- **Remove and reinstall:** `remove-instance` and `uninstall-shared` kept every
  configuration, state directory, and document. A new installation, with the
  kept configurations registered again, kept the password, the trash, and the
  documents.

That machine ran without hardware acceleration. There,
`scripts/verify-clean-checkout.sh` passed everything except two
image-collection tests: their scan exceeded the 10-second budget of
`HF-NAV-011`, so the result was inconclusive and the image was kept, as
`HF-IMG-005` requires.

## Remote access

- **Caddy:** both Caddyfiles of [remote access](operations.md#example-caddy)
  ran with Debian's `caddy` package and `tls internal`, one with a host name
  and one with a base path. Through the proxy, these worked:
  - sign in;
  - the session cookie flags;
  - file creation;
  - the redirect to the final `/`.

  A direct request to the port was refused with `403`.
- **Forwarded headers:** a synthetic instance in remote mode accepted a
  request only with `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto: https`.
  It refused these, as documented:
  - `HTTPS` in capitals;
  - a missing or repeated header;
  - a `Host` rewritten to the loopback address;
  - a missing or foreign `Origin` on a state-changing request;
  - a missing base path;
  - a missing final `/`.
- **Tailscale Serve:** the example was not run on the test machine.

## Browsers and devices

Checks used Chromium and WebKit, with emulated phone and tablet sizes and touch
input. They covered:

- the toolbar and menus, long press, and dialogs;
- breadcrumbs, wide tables, and code blocks;
- keyboard resizing of the sidebar;
- an automated accessibility check (axe) at desktop and tablet widths.

## Open limits

- Physical iPhone- and iPad-class devices, Safari on them, and Firefox are not
  validated (`HF-UI-003`, `HF-ACC-021`).
- In the WebKit build used for testing, the PDF preview rendered upside down.
  It rendered correctly in Chromium and must be checked on a device.
- `static/app.js` has no unit tests; its behavior is checked by the browser
  suite in WebKit only. The editor runtime has the frontend tests above.
- No acceptance requirement of specification section 13 is claimed as passed
  as a whole. [Security](security.md#limits-that-remain-open) lists the limits
  that remain open.
