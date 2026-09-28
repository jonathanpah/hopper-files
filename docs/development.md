# Development

Build and test Hopper Files from this repository alone; no private tree, site
profile, or installed release is an input. The contract is
[the specification](specification.md). Administration is in
[operations](operations.md), and the security boundary and open limits are in
[security](security.md).

## Language and boundary

Public technical documents and code comments are in English. The interface is
in Portuguese. Do not add site host names, account names, credentials, private
audit notes, or personal paths to this repository; examples use
`example.com`, the account and instance `alice`, and port 8765. The MIT
license is [LICENSE](../LICENSE); the copyright holder is Jonathan Honorio.

## Python and the locked dependencies

`.python-version` and `pyproject.toml` require CPython `>=3.13,<3.14`. The
direct dependencies are pinned in `requirements.in` and hash-locked, with
their own dependencies, in `requirements.lock` against
`https://pypi.org/simple`. The test dependencies are the `test` extra in
`pyproject.toml` (`pytest` and `httpx`, at the locked versions). The three
files declare the same pins.

A checkout does not install the package. Commands run from the repository
root with `src` on `PYTHONPATH`. The administrator module needs only `src`,
because it uses the Python standard library. The runtime, the tests, and the
integration scripts also need a directory with the locked dependencies. Create
it once, outside the clone, with the distribution's `pip` (Debian package
`python3-pip`):

```sh
LOCKED_SITE="$HOME/.cache/hopper-files/site"
/usr/bin/python3 -m pip install --require-hashes --only-binary=:all: \
  --index-url https://pypi.org/simple --target "$LOCKED_SITE" -r requirements.lock
```

Keep it outside the clone: the repository verifier refuses files it does not
know, except `.venv`, `build`, `dist`, `node_modules`, and caches. Do not
download a standalone pip wheel and run it.

### Regenerate the lock

Change `requirements.in` and `pyproject.toml` together, then regenerate
`requirements.lock` with pip-tools 7.6.1 against the official index:

```sh
/usr/bin/python3 -m pip install --target "$HOME/.cache/hopper-files/pip-tools" pip-tools==7.6.1
PYTHONPATH="$HOME/.cache/hopper-files/pip-tools" /usr/bin/python3 -m piptools compile \
  --generate-hashes --no-header --output-file=requirements.lock requirements.in
```

pip-tools keeps the versions already in the lock unless the change needs
another one. It does not write the first five lines of the file: three comment
lines, `--index-url https://pypi.org/simple`, and an empty line. Put them back;
the release builder and the installer refuse a lock without that index line.
Then review the diff. With the lock unchanged, this procedure reproduces it
exactly.

## Run the checks

From the repository root:

```sh
env PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:"$LOCKED_SITE" \
  python3 -m pytest -q -p no:cacheprovider tests
env PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_repository.py
```

A test skips, and says why, when the host or the account lacks what it needs:
root for the isolated lifecycle test; the private list of site markers for the
site-marker test, which reads the file named by
`HOPPER_FILES_FORBIDDEN_MARKERS`; and, on some hosts, POSIX ACLs, extended
attributes, set-ID bits, `/proc`, inode reuse, or a second writable
filesystem. A few permission tests skip when they run as root, because root
reads and lists everything. Two image-collection tests depend on the 10-second
scan budget of `HF-NAV-011`; on a very slow machine, such as one without
hardware acceleration, the scan reports an incomplete result and they fail.
The verifier prints `repository verification passed.`. `--forbid TEXT` also
fails when `TEXT` appears in a public source file.

The integration scripts start a synthetic instance on a free loopback port,
exercise it over HTTP, and remove their temporary files when they exit. Run
each with the same environment:

```sh
env PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:"$LOCKED_SITE" \
  python3 scripts/verify_files_integration.py
```

| Script | What it checks |
| --- | --- |
| `scripts/verify_files_integration.py` | Login, CSRF, interface-state revisions, create, streamed upload and download, copy, ZIP, extraction, and the absence of the retired root route. |
| `scripts/verify_trash_integration.py` | The same flow, then the trash: delete with marks, list, a restore conflict, and a restore under another name. |
| `scripts/verify_navigation_integration.py` | Navigation, search, tags of a monitored folder, and interface-state conflicts. |
| `scripts/verify_editor_integration.py` | The editor and viewers: module loading, file versions, `428` and `409`, invalid UTF-8, inert SVG, and image and PDF previews. |
| `scripts/verify_images_integration.py` | Image upload, pending images, collection after a save, joint restore of a note and its images, and moves that rewrite references, across filesystems when a second writable filesystem is available. |

`--serve-for-browser` on the
navigation and editor scripts, and `--hold-browser` on the image script, keep
the synthetic instance running for a manual browser session and print its
address and synthetic password; stop the script to remove it. These scripts
are not installed-service commands.

## Clean-checkout check

```sh
./scripts/verify-clean-checkout.sh [TREEISH]
```

`TREEISH` is a tag, branch, commit, or tree ID; the default is `HEAD`. The
script tests a committed tree: uncommitted changes are not included. It
exports the tree to a temporary directory, installs the hash-locked
dependencies there with `/usr/bin/python3 -m pip` from the official index,
builds the wheel and checks its inventory, installs it, and runs the
repository verifier, the five integration scripts, and the tests. It needs
network access to the official index and removes its temporary directory when
it exits.

## Wheel

```sh
env PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$LOCKED_SITE" \
  python3 -m hatchling build -t wheel -d dist
python3 scripts/verify_built_wheel.py dist/hopper_files-0.1.1-py3-none-any.whl
```

`hatchling` must be the pinned `1.32.4` from the locked dependencies. The
wheel contains the Python package and the MIT license, not the `docs/` tree.
`scripts/verify_built_wheel.py` lists every file the wheel may contain and
refuses any other.

## Frontend

Node.js is a build tool only; the product never runs it. The interface code is
`src/hopper_files/static/app.js` and `frontend/src/`. The browser receives the
generated bundle: `/app.js` returns `app.bundle.js`, which loads the chunks in
`static/assets/`. After editing either input, rebuild with the pinned
procedure in [the frontend note](../frontend/README.md) and commit the
generated files; without a rebuild the change never reaches the browser, and no
check reports it. `app.css` is served as written.

`scripts/verify_repository.py` checks the generated files against
`frontend-bundle-manifest.json`.

## Tests and real files

The test suite writes only below its temporary directories. Tests that read
note content (image-reference scans, collection) confine the corpus to their
synthetic documents directory through the harness (`tests/conftest.py`); the
product itself always discovers the corpus from `/`. Tag-index tests mark only
folders inside that directory as monitored. The integration scripts do the same
for the runtime they start. Do not point any check that reads content at a
real account's files.

## Release archive

A release archive is built from a full Git tree ID, not from the working
directory. For tag `v0.1.1`:

```sh
TREE=$(git rev-parse 'v0.1.1^{tree}')
python3 scripts/build_release_artifact.py --tree "$TREE" \
  --release-id hopper-files-0.1.1 --output "$HOME/hopper-files-0.1.1.tar.gz"
```

`--tree` takes the full hexadecimal ID of a tree object; a commit or tag ID is
refused, and `git rev-parse 'TAG^{tree}'` gives the tree of a tag. `--release-id` has 1
to 64 letters, digits, `.`, `_`, or `-`, starting with a letter or digit; use
`hopper-files-X.Y.Z` for tag `vX.Y.Z`. The output must be outside the clone.
The builder refuses a tree whose specification does not match its pinned
SHA-256 or that fails the repository verifier. It prints the archive SHA-256
and writes it to `ARCHIVE.sha256`.

Run the builder of the same tag, from a checkout of it, or use the README's
release script, which does that. The builder of another commit may refuse the
tree, because the specification SHA-256 is pinned in it, or produce different
bytes.

The build is reproducible: the same tree and release ID give the same archive,
byte for byte, on another computer. The README's release script relies on
that: it builds the tag on the server and compares the result with the
SHA-256 published on the release page.

### Publish a release

1. Set the version in `pyproject.toml`, `src/hopper_files/__init__.py`, and
   `scripts/verify_built_wheel.py`; in the README, its text, the release page
   address, and `TAG` and `RELEASE_ID` in the release script; and in the
   examples of [operations](operations.md) and this page. Record the checks in
   [validation](validation.md).
2. Tag the commit `vX.Y.Z`.
3. Build the archive from that tag with release ID `hopper-files-X.Y.Z`.
4. On the release page, publish the archive, its SHA-256, the Git tree ID, and
   the release ID.

## Local commands without root

These commands run from the repository root and do not install a service:

```sh
env PYTHONPATH=src python3 -m hopper_files.admin --help
env PYTHONPATH=src python3 -m hopper_files.admin check --config CONFIG
env PYTHONPATH=src python3 -m hopper_files.admin set-password --config CONFIG
env PYTHONPATH=src python3 -m hopper_files.admin purge-trash --config CONFIG
env PYTHONPATH=src:"$LOCKED_SITE" HOPPER_FILES_INSTANCE=CONFIG \
  python3 -m hopper_files.runtime
```

`check`, `set-password`, and `purge-trash` run as the configured service
account. `set-password` reads the new password with `getpass`; it is never an
argument. `runtime` listens only on the configured loopback address. The
installed commands `hopper-files-admin` and `hopper-files-launch` select the
active release themselves; do not add a checkout `PYTHONPATH` to them.

## Inventory

`docs/repository-inventory.md` and `EXPECTED_FILES` in
`scripts/verify_repository.py` list every tracked path; add or remove both
together. `EXPECTED_FILES` in `scripts/verify_built_wheel.py` lists every file
of the wheel, including the packaged license; update it with any file added to
or removed from `src/hopper_files/`. `docs/specification.md` is pinned by its
SHA-256 in `scripts/verify_repository.py` and
`scripts/build_release_artifact.py`; a specification edit is complete when
both match the file. Do not add generated environments, private notes, or a
second product specification.
