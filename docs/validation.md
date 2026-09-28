# Validation summary

This page summarizes how release 0.1.0 was checked and what remains open. It
does not change the [specification](specification.md). A check listed here
does not by itself pass an acceptance requirement of specification section 13.
All checks used synthetic files and synthetic passwords.

## Automated checks

On the development machine:

- **Python tests:** all passed. Two tests skip when their condition is
  missing: one needs root, and one reads a list of site-specific terms named by
  `HOPPER_FILES_FORBIDDEN_MARKERS`. Other tests skip on hosts without POSIX
  ACLs, extended attributes, set-ID bits, `/proc`, inode reuse, or a second
  writable filesystem.
- **Frontend tests:** all passed, with Node.js `v24.21.0` and npm `11.19.0`.
- **Integration scripts:** the five loopback scripts passed.
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
- `static/app.js` has no unit tests; its behavior is checked in browsers. The
  editor runtime has the frontend tests above.
- No acceptance requirement of specification section 13 is claimed as passed
  as a whole. [Security](security.md#limits-that-remain-open) lists the limits
  that remain open.
