# Hopper Files

Hopper Files is a browser-based file manager and Markdown editor for a Linux
server. Each instance runs as one Linux account and shows the filesystem from
`/` as that account sees it: what the account can read opens, and what it can
write can be changed. Hopper Files adds no access rules of its own; the
instance password is the only barrier in front of the account's files.

The web interface and the messages of the administrator command are in
Portuguese (Brazil). The documentation is in English.

It includes a tree and a listing that start at `/`, name and text search, tabs,
favorites, labels, Markdown tags, create, rename, move, copy, and delete with a
30-day trash, upload and download, ZIP creation and extraction, a Markdown
editor with formatted and clean views, a plain-text editor, and image and PDF
viewers.

The current release is 0.1.4, Git tag `v0.1.4`.

[Install](#install) · [Check and open](#check-and-open) ·
[Remote access](docs/operations.md#remote-access-behind-an-https-proxy) ·
[Operations](docs/operations.md) · [Security and limits](docs/security.md) ·
[Development](docs/development.md)

## Requirements

- Debian 13, the reference system. Other Linux systems with systemd and
  CPython 3.13 at `/usr/bin/python3` are not tested.
- `git`, `python3-pip`, and `curl` from the distribution.
- An administrator account that can use `sudo`.
- Outbound HTTPS to `pypi.org` and `files.pythonhosted.org` while you install
  or update a release. The installer downloads the pinned Python dependencies
  from there.
- One existing Linux account, other than root, for each instance. Hopper Files
  does not create accounts.

## Install

These steps install release 0.1.4 for one existing account, `alice`, with
local access on port 8765. Replace the account, port, and paths with your own.
Run every command as your administrator account, from a terminal on the
server.

1. Install the tools:

   ```sh
   sudo apt update
   sudo apt install git python3-pip curl
   ```

2. Clone the repository and enter it:

   ```sh
   git clone https://github.com/jonathanpah/hopper-files.git
   cd hopper-files
   ```

3. Open the release page,
   <https://github.com/jonathanpah/hopper-files/releases/tag/v0.1.4>. It lists the
   release ID, `hopper-files-0.1.4`, the Git tree, and the SHA-256 of the
   release archive. Copy the SHA-256.

   The build is reproducible: building tag `v0.1.4` with release ID
   `hopper-files-0.1.4` produces the same archive, byte for byte. The script in
   the next step builds it on your server and stops unless its SHA-256 matches
   the published one.

4. Save the script below as `~/hopper-files-release.sh`. Put the SHA-256 from
   the release page in `EXPECTED_ARCHIVE_SHA256`:

   ```bash
   #!/bin/bash
   # Build a Hopper Files release from its Git tag, check the archive against
   # the SHA-256 on the release page, then install or update it.
   # Run it from the repository clone: bash ~/hopper-files-release.sh install|update
   set -euo pipefail

   TAG=v0.1.4
   RELEASE_ID=hopper-files-0.1.4
   EXPECTED_ARCHIVE_SHA256=PASTE_THE_SHA256_FROM_THE_RELEASE_PAGE

   action=${1:-}
   if [[ "$action" != install && "$action" != update ]]; then
     echo "usage: bash hopper-files-release.sh install|update" >&2
     exit 2
   fi
   if [[ ! "$EXPECTED_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
     echo "Set EXPECTED_ARCHIVE_SHA256 to the SHA-256 on the release page." >&2
     exit 2
   fi

   cd "$(git rev-parse --show-toplevel)"
   TREE_ID=$(git rev-parse --verify "$TAG^{tree}")
   umask 077
   BUILD_DIR=$(mktemp -d /var/tmp/hopper-files-build.XXXXXXXX)
   BOOTSTRAP=
   cleanup() {
     rm -rf -- "$BUILD_DIR"
     if [[ -n "$BOOTSTRAP" ]]; then sudo rm -rf -- "$BOOTSTRAP"; fi
   }
   trap cleanup EXIT

   # Build the archive with the builder of the same tree.
   git archive --format=tar --prefix=source/ "$TREE_ID" | tar -xf - -C "$BUILD_DIR"
   printf 'gitdir: %s\n' "$(git rev-parse --absolute-git-dir)" > "$BUILD_DIR/source/.git"
   ARTIFACT="$BUILD_DIR/$RELEASE_ID.tar.gz"
   /usr/bin/python3 -I -B "$BUILD_DIR/source/scripts/build_release_artifact.py" \
     --tree "$TREE_ID" --release-id "$RELEASE_ID" --output "$ARTIFACT"

   # Stop unless the archive has the published SHA-256.
   printf '%s  %s\n' "$EXPECTED_ARCHIVE_SHA256" "$ARTIFACT" | sha256sum --check -

   if [[ "$action" == update ]]; then
     sudo hopper-files-admin update-release --artifact "$ARTIFACT" --sha256 "$EXPECTED_ARCHIVE_SHA256"
     exit 0
   fi

   # First installation: run the administrator code of the same tree as root,
   # from a new directory that only root can change.
   BOOTSTRAP=$(sudo mktemp -d /var/tmp/hopper-files-bootstrap.XXXXXXXX)
   git archive --format=tar --prefix=source/ "$TREE_ID" | sudo tar -xf - -C "$BOOTSTRAP"
   sudo env -i HOME=/root LANG=C.UTF-8 PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
     /usr/bin/python3 -I -B -c \
     'import sys; sys.path.insert(0, sys.argv[1]); from hopper_files.admin import main; raise SystemExit(main(["install-shared", "--artifact", sys.argv[2], "--sha256", sys.argv[3]]))' \
     "$BOOTSTRAP/source/src" "$ARTIFACT" "$EXPECTED_ARCHIVE_SHA256"
   ```

   Then run it from the clone. Do not start it with `sudo`; it calls `sudo`
   itself for the steps that need root:

   ```sh
   bash ~/hopper-files-release.sh install
   ```

   It takes a few minutes. It ends with `Instalação compartilhada ativa:
   hopper-files-0.1.4` and creates the command `hopper-files-admin`. The
   release lives in `/opt/hopper-files`; the script removes its temporary
   directories when it exits.

   If `sha256sum` prints `FAILED`, the script stops before it installs
   anything. Check that `TAG` and `RELEASE_ID` match the release page and that
   `git rev-parse 'v0.1.4^{tree}'` prints the Git tree shown there. If they
   match, report the problem; do not replace the SHA-256.

5. Write the instance configuration. It is a plain JSON file, owned by root
   and readable by `alice`:

   ```sh
   sudo install -d -m 0755 /etc/opt/hopper-files
   sudo tee /etc/opt/hopper-files/alice.json > /dev/null <<'EOF'
   {
     "version": 2,
     "instanceId": "alice",
     "serviceAccount": "alice",
     "timeZone": "UTC",
     "sessionDurationSeconds": 86400,
     "stateDirectory": "/home/alice/.local/state/hopper-files",
     "cookieName": "hf_alice",
     "access": {
       "mode": "local",
       "bindHost": "127.0.0.1",
       "bindPort": 8765,
       "baseUrl": "http://127.0.0.1:8765/",
       "trustedProxyPeer": null
     }
   }
   EOF
   sudo chmod 0644 /etc/opt/hopper-files/alice.json
   ```

   Set `timeZone` to the IANA name you want in generated file names, such as
   `UTC` or `Europe/Berlin`; `timedatectl list-timezones` lists them. There is
   no default. Every field is described in
   [operations](docs/operations.md#instance-configuration).

6. Create the state directory as `alice`, private to that account:

   ```sh
   sudo -u alice mkdir -p -m 0700 /home/alice/.local/state/hopper-files
   ```

7. Register the instance. The command checks the configuration as `alice`,
   prepares the state directory, and writes, enables, and starts
   `hopper-files-alice.service`:

   ```sh
   sudo hopper-files-admin register-instance --config /etc/opt/hopper-files/alice.json
   ```

   It prints `Instância registrada e ativa: alice`.

8. Set the password as `alice`. The command asks for it twice (`Nova senha`,
   `Confirme a nova senha`) and prints `Senha atualizada.`:

   ```sh
   sudo -u alice hopper-files-admin set-password --config /etc/opt/hopper-files/alice.json
   ```

9. Check the instance as `alice`:

   ```sh
   sudo -u alice hopper-files-admin check --config /etc/opt/hopper-files/alice.json
   ```

   It prints `Instância alice válida.`, followed by the mode, the listening
   address, the origin, and the time zone.

`set-password`, `check`, and `purge-trash` run as the instance's account. As
any other user, including root, they refuse to run.

## Check and open

```sh
systemctl status hopper-files-alice
sudo journalctl -u hopper-files-alice -n 20
curl -fsS http://127.0.0.1:8765/healthz
```

The last command prints `{"service":"hopper-files","status":"up"}`. After a
registration or an update, the service can take a few seconds to answer;
repeat the command if it fails the first time. The commands report success as
soon as systemd starts the service, so a start that fails a few seconds later,
for example on a port already in use, shows only here and in the log. Each
start writes a log line that begins with `Hopper Files active release=`.

Open the `baseUrl` exactly as configured, including the final `/`:

- In a browser on the server itself, open `http://127.0.0.1:8765/`. The
  address `http://localhost:8765/` is refused with `403`, because the host must
  match `baseUrl`.
- From another computer, keep local mode and open an SSH tunnel with the same
  port number, then open `http://127.0.0.1:8765/` on that computer:

  ```sh
  ssh -N -L 8765:127.0.0.1:8765 USER@SERVER
  ```

  `USER@SERVER` is any account you use to reach the server over SSH. The local
  port must also be 8765, because the port is part of the address.
- For HTTPS without a tunnel, use remote mode behind a reverse proxy; see
  [remote access](docs/operations.md#remote-access-behind-an-https-proxy).

Sign in with the password you set. The login page also links to a page that
changes the password with the current one.

## Everyday administration

| Task | Command |
| --- | --- |
| Apply an edited configuration | `sudo hopper-files-admin update-instance --config /etc/opt/hopper-files/alice.json` |
| Update to a new release | Run `git fetch --tags` in the clone, set `TAG`, `RELEASE_ID`, and `EXPECTED_ARCHIVE_SHA256` in the script to the new release, then run `bash ~/hopper-files-release.sh update` |
| Return to an installed release | `sudo hopper-files-admin rollback-release --release-id hopper-files-0.1.0` |
| Empty expired trash entries | `sudo -u alice hopper-files-admin purge-trash --config /etc/opt/hopper-files/alice.json` |
| Recover a forgotten password | `sudo -u alice hopper-files-admin set-password --config /etc/opt/hopper-files/alice.json` |
| Restart after a crash | `sudo systemctl restart hopper-files-alice` |
| Remove the instance | `sudo hopper-files-admin remove-instance --instance-id alice` |
| Remove the shared application | `sudo hopper-files-admin uninstall-shared` |

Removing an instance or the application keeps every configuration file,
state directory, and document. [Operations](docs/operations.md) explains each
command, a second instance, updates, backups, and recovery.

## Known limitations

- The interface is in Portuguese only.
- Nothing empties the trash on its own. Entries become removable 30 days after
  deletion; run `purge-trash`, for example from the account's crontab.
- A symbolic link itself cannot be renamed, moved, or moved to the trash, so
  Hopper Files cannot delete it. Opening a link opens its target.
- Copying, zipping, moving, or renaming a folder is refused when it contains
  a symbolic link, a special file, or an item the account cannot read, or when
  it has more than 3 GiB, 10,000 items, or 32 levels. A file over 3 GiB cannot
  be moved or renamed. This holds even for a rename within one filesystem, for
  example of a project folder with a `.venv` or `node_modules`.
- An item on another filesystem whose owner, group, set-ID bits, or extended
  metadata the account cannot reproduce cannot be moved to the trash, so
  Hopper Files cannot delete it.
- A folder cannot be downloaded as one file; create a ZIP first.
- An upload takes up to 20 files, 200 MiB per file, and 400 MiB in total. The
  editor opens text files up to 5 MiB. Previews show images up to 20 MiB and
  PDFs up to 100 MiB. Larger files can still be downloaded.
- The trash restores one item at a time. Restoring a note can bring back its
  images with it.
- A folder monitored for tags does not follow a rename or a move.
- With many notes, the first tag listing, move, or rename can report an
  incomplete result or refuse until the derived note index is complete; later
  requests continue the work.
- The service does not restart by itself after a crash.
- The open folder, open tree branches, and open documents follow changes made
  outside the app within about 2 seconds, while the browser tab is visible.
  Changes made by another machine on a network share appear only after the
  view is read again, for example when the tab returns to view.
- With a base path such as `/files/`, the address without its final `/`
  answers `401` with a JSON body, or `404` when a session is open, instead of
  redirecting to the login page.
- Interface checks used Chromium and WebKit with emulated phones and tablets.
  Physical iPhone- and iPad-class devices and Firefox are not validated.

[Security](docs/security.md) lists the limits of the recorded checks, and
[validation](docs/validation.md) records what was checked.

## Development

[Development](docs/development.md) explains how to test, build the frontend,
and build a release archive from this source tree alone. Contributors who use
coding agents will find their instructions in [AGENTS.md](AGENTS.md).

## Repository layout

- `src/hopper_files/`: the Python package, including the Portuguese interface
  and the generated frontend files in `static/`.
- `frontend/`: the CodeMirror, Markdown, and PDF viewer sources, their pinned
  dependencies, and the build procedure ([frontend/README.md](frontend/README.md)).
- `tests/`: automated tests with synthetic data.
- `scripts/`: the release builder, source and wheel inventory checks, the
  clean-checkout check, and loopback integration checks.
- `docs/`: [index](docs/index.md), [specification](docs/specification.md),
  [operations](docs/operations.md), [security](docs/security.md),
  [runtime](docs/runtime.md), [development](docs/development.md),
  [validation](docs/validation.md), and the
  [repository inventory](docs/repository-inventory.md).
- `requirements.in` and `requirements.lock`: the pinned Python dependencies
  from the official Python Package Index.

## Report problems and contribute

Report problems through the repository's issue tracker,
<https://github.com/jonathanpah/hopper-files/issues>. Report a suspected
vulnerability privately, as the [Security Policy](SECURITY.md) describes.
[Contributing](CONTRIBUTING.md) explains how to propose a change, and the
[Code of Conduct](CODE_OF_CONDUCT.md) applies to every discussion.

## License

Hopper Files is licensed under the MIT License. The copyright holder is
Jonathan Honorio. See [LICENSE](LICENSE).

The generated browser files in `src/hopper_files/static/` (`app.bundle.js` and
`assets/`) bundle third-party libraries, such as CodeMirror and PDF.js, under
their own licenses. `src/hopper_files/static/THIRD_PARTY_NOTICES.txt` lists
them.
