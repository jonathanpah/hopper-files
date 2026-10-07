# Hopper Files: instructions for coding agents

This repository is the public source of Hopper Files, a browser-based file
manager and Markdown editor for Linux. One FastAPI process serves each
instance on a loopback port; a local administrator command,
`hopper-files-admin`, installs shared releases under `/opt/hopper-files` and
registers each instance as a systemd unit. The tree holds product source,
tests, and public documentation. Private planning, site configuration,
credentials, user data, build outputs, and runtime state live outside it.

## Product model

- An instance runs as one existing Linux account, never root, and explores
  the filesystem from `/`. That account's Linux permissions are the only
  access limit, and the instance login is the only application barrier. Keep
  it that way unless `docs/specification.md` changes: the unit sets
  `NoNewPrivileges=yes`, empty capability sets, and `UMask=0002`, and
  restricts no path.
- The instance configuration is JSON version 2; `docs/operations.md` lists
  its fields. Configured roots, the System view, protected paths, creation
  policies, and `GET api/roots` belong to the retired version 1 model
  (specification section 15). They survive only in the version 1 migration
  (`migration.py`, `migrate-release`, `return-release`).
- The API carries an address as `rootId=fs` plus a path relative to `/`;
  `fs` is a fixed transport value. Internal names such as `roots.py` and
  `DocumentRoot` predate the single base.
- The interface and the messages of `hopper-files-admin` are in Portuguese.
  Error details, public documents, and code comments are in English. Keep
  comments short and generic.

## Sources of truth

- Product behavior: `docs/specification.md`, the normative contract. Read the
  requirements for the area you change before changing behavior.
- Installation, configuration, remote access through an HTTPS proxy,
  lifecycle, and recovery: `docs/operations.md`. The installation sequence
  and the release script: `README.md`.
- Tests, locked dependencies, the frontend bundle, and release archives:
  `docs/development.md` and `frontend/README.md`.
- Security boundary and open limits: `docs/security.md`. Request behavior and
  state layout: `docs/runtime.md`.
- What was checked, where, and with which limits: `docs/validation.md`.
- CLI commands and options: `env PYTHONPATH=src python3 -m hopper_files.admin --help`,
  and `--help` after a command name.

## Working rules

- Tests and integration scripts create synthetic data in temporary
  directories. Checks that read note content stay confined to their synthetic
  documents directory, as `tests/conftest.py` and the integration scripts do.
  Never point a check at a real account's files.
- `src/hopper_files/static/app.js` and `frontend/src/` are the inputs of the
  generated bundle (`app.bundle.js`, `static/assets/`, and
  `frontend-bundle-manifest.json`). After changing them, rebuild with the
  pinned Node.js procedure in `frontend/README.md` and commit the generated
  files. `app.css` is served as written.
- Examples use generic values: `example.com`, the account and instance
  `alice`, and port 8765. Site host names, real account names, and personal
  paths stay out of the tree, including tests.
- Public documents, code comments, and file names describe current behavior
  in plain, generic terms. They carry no internal stage or review labels, no
  dates of past work, and no notes about who asked for a change.

## Source controls

- `pyproject.toml`, `requirements.in`, and `requirements.lock` declare the
  same pins. Regenerate the lock only against `https://pypi.org/simple`, as
  `docs/development.md` shows.
- Each tracked path is listed in both `docs/repository-inventory.md` and
  `EXPECTED_FILES` in `scripts/verify_repository.py`. Each file of the Python
  package is listed in `EXPECTED_FILES` in `scripts/verify_built_wheel.py`.
  Update these lists in the same change as the file.
- A specification edit is complete when the SHA-256 pinned in
  `scripts/verify_repository.py` and `scripts/build_release_artifact.py`
  matches `docs/specification.md`.
- A behavior change is complete when the documents that describe it
  (`README.md`, `docs/operations.md`, `docs/runtime.md`, `docs/security.md`)
  match the code and `docs/validation.md` records how it was checked.
- The release version appears in `pyproject.toml`,
  `src/hopper_files/__init__.py`, and `scripts/verify_built_wheel.py`; keep
  the three equal, and use the tag `vX.Y.Z` and the release ID
  `hopper-files-X.Y.Z`. The README (its text, the release page address, and
  `TAG` and `RELEASE_ID` in the release script) and the examples in
  `docs/operations.md` and `docs/development.md` also name the version;
  update them in the same change.
- `LICENSE` is the MIT license of Hopper Files, with its copyright notice
  naming Jonathan Honorio. The generated browser files bundle third-party
  libraries under their own licenses, listed in
  `src/hopper_files/static/THIRD_PARTY_NOTICES.txt`.

## Verify a change

From the repository root, with `LOCKED_SITE` set to a directory of the locked
dependencies (`docs/development.md` shows how to create one):

```sh
env PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:"$LOCKED_SITE" \
  python3 -m pytest -q -p no:cacheprovider tests
env PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_repository.py
```

A change is verified when pytest passes, skipping only the checks whose
reason names something the host or the account running the tests lacks, or
that running as root makes unobservable, and the verifier prints
`repository verification passed.`. Two image-collection tests depend on the
10-second scan budget of `HF-NAV-011` and can fail on a very slow machine,
such as one without hardware acceleration. Also run the integration scripts listed in
`docs/development.md` when a change touches HTTP routes, state, or file
operations; `npm --prefix frontend test` when it touches the frontend; and
`./scripts/verify-clean-checkout.sh` (network required) before a release.

## Claims

Record what was checked, with its environment and limits, in
`docs/validation.md`. An acceptance requirement of specification section 13
counts as passed only with the evidence it names, for example physical
iPhone- and iPad-class devices for `HF-ACC-021`, or a Debian 13 installation
for `HF-ACC-001`. The usability requirement `HF-UI-003`, in section 8.1, also
needs those devices.
