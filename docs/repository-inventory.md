# Public repository inventory

This inventory is the positive list of public source paths. The repository
contains only the paths below, plus local Git metadata when checked out as a
repository. Generated files, virtual environments, build outputs, runtime
state, site configuration, credentials, and private planning material are not
source inputs. `scripts/verify_repository.py` checks the same list.

| Path | Purpose |
| --- | --- |
| `.gitignore` | Excludes generated local artifacts. |
| `.python-version` | Records the tested Python minor version. |
| `AGENTS.md` | Instructions for coding agents that work on this repository. |
| `CODE_OF_CONDUCT.md` | Expected conduct in issues, discussions, and reviews. |
| `CONTRIBUTING.md` | How to report problems, propose changes, and open a pull request. |
| `SECURITY.md` | How to report a suspected vulnerability privately, and the intended security boundaries. |
| `.github/pull_request_template.md` | Pull request description template. |
| `.github/ISSUE_TEMPLATE/bug-report.yml` | Issue form for reproducible problems. |
| `.github/ISSUE_TEMPLATE/change-proposal.yml` | Issue form for change proposals. |
| `LICENSE` | MIT license. Copyright holder: Jonathan Honorio. |
| `README.md` | What Hopper Files is, requirements, installation steps, first checks, everyday commands, and known limitations. |
| `pyproject.toml` | Project metadata, build settings, and direct pins. |
| `requirements.in` | Official-index dependency inputs. |
| `requirements.lock` | Hash-locked dependency resolution. |
| `docs/index.md` | Entry index for this documentation directory. |
| `docs/repository-inventory.md` | This positive source list. |
| `docs/runtime.md` | Behavior of a running instance: access, files, editor, trash, service unit, and state layout. |
| `docs/development.md` | Tests, locked dependencies, frontend, wheel, and release archives from the public source tree alone. |
| `docs/operations.md` | Commands, configuration, installation details, remote access behind an HTTPS proxy, updates, trash, removal, backup, recovery, and the migration of pre-release instances. |
| `docs/security.md` | Server and filesystem boundaries, and the limits the recorded checks do not close. |
| `docs/validation.md` | What was checked, where, and with which limits. |
| `docs/specification.md` | Normative product specification. |
| `scripts/verify-clean-checkout.sh` | Builds and tests an exported committed tree with hash-locked dependencies from the official index and the distribution's pip. |
| `scripts/build_release_artifact.py` | Reproducible release archive builder for one Git tree. |
| `scripts/verify_files_integration.py` | Loopback check of login, CSRF, interface-state revisions, create, upload, download, copy, ZIP, and extraction. |
| `scripts/verify_trash_integration.py` | The same loopback flow, then trash delete, restore conflict, and restore under another name. |
| `scripts/verify_navigation_integration.py` | Loopback check of navigation, search, tags, interface-state conflicts, and change notices, with an optional browser fixture. |
| `scripts/verify_editor_integration.py` | Loopback check of the editor and viewer routes, with an optional browser fixture. |
| `scripts/verify_images_integration.py` | Loopback check of image upload, collection, joint restore, and reference-preserving moves. |
| `scripts/build_frontend.mjs` | Reproducible browser bundling and license-notice generation. |
| `scripts/verify_built_wheel.py` | Positive wheel inventory, including the packaged MIT `LICENSE`, rejecting every other extra path. |
| `scripts/verify_repository.py` | Inventory, specification-hash, and optional marker check. |
| `frontend/README.md` | Pinned frontend build procedure, origins, and dependency-hash policy. |
| `frontend/package.json` | Direct pinned browser dependency declarations and build/test commands. |
| `frontend/package-lock.json` | Exact npm dependency graph, registry URLs, and integrity digests. |
| `frontend/src/entry.js` | Browser entry point: loads the editor runtime and `static/app.js`. |
| `frontend/src/editor-runtime.js` | Markdown editor, sanitized rendering, and clean-copy implementation. |
| `frontend/src/pdf-viewer.js` | Local PDF.js canvas renderer. |
| `frontend/test/clean-copy.test.mjs` | Normative clean-copy and plain-text recomposition fixtures. |
| `frontend/test/editor-commands.test.mjs` | Editor commands: selection-based formatting, crossing pairs, headings, bullets, links, attached images, and active formats. |
| `frontend/test/markdown-render.test.mjs` | Formatted-view rendering: tags (normalization, limits, and where they apply), task items, bullet markers, and raw HTML kept as text. |
| `src/hopper_files/__init__.py` | Package identity and version. |
| `src/hopper_files/access.py` | Host, origin, and trusted-proxy decision. |
| `src/hopper_files/admin.py` | The `hopper-files-admin` commands: installation, releases, instances, password, check, and trash purge. |
| `src/hopper_files/lifecycle.py` | Verified immutable releases, active selector, service registration, update, rollback, removal, uninstall, and migration. |
| `src/hopper_files/app.py` | Application assembly and request guard. |
| `src/hopper_files/clock.py` | Server clock abstraction. |
| `src/hopper_files/confinement.py` | Service umask. |
| `src/hopper_files/config.py` | Instance configuration and service identity. |
| `src/hopper_files/credentials.py` | Version 1 scrypt verifier. |
| `src/hopper_files/files.py` | Durable tokens, preflight, upload, copy, ZIP, and recovery. |
| `src/hopper_files/kdf.py` | Fixed scrypt parameters and runtime proof. |
| `src/hopper_files/limiter.py` | Persistent login token buckets. |
| `src/hopper_files/responses.py` | Generic HTTP responses. |
| `src/hopper_files/roots.py` | Single filesystem base, descriptor-based address resolution, listing, bounded traversal, and staging primitives. |
| `src/hopper_files/filetimes.py` | Birth time of a listing entry through `statx`, when the filesystem records it. |
| `src/hopper_files/corpus.py` | Note corpus traversal from `/` (writable directories, traversal limits). |
| `src/hopper_files/note_index.py` | Private derived per-note index of tags and image destinations. |
| `src/hopper_files/migration.py` | Version 1 to version 2 state migration and return, run as the service account. |
| `src/hopper_files/runtime.py` | Loopback entry point and active-release startup record. |
| `src/hopper_files/search.py` | Bounded literal search snapshots, secure cursors, and Markdown-tag derivation. |
| `src/hopper_files/sessions.py` | Sessions, nonces, and CSRF tokens. |
| `src/hopper_files/state.py` | Private state layout and atomic publication. |
| `src/hopper_files/systemd_unit.py` | Systemd unit renderer for selected-release execution without filesystem restrictions. |
| `src/hopper_files/trash.py` | Per-instance trash, durable delete and restore, and purge. |
| `src/hopper_files/editor.py` | Strong file versions, metadata-safe atomic editor saves, and cooperative locks. |
| `src/hopper_files/buffers.py` | Connected editor buffers, clean acknowledgements, and journaled move holds. |
| `src/hopper_files/changes.py` | Change notices: inotify watches of the directories clients name, numbered notices, and release of unnamed directories. |
| `src/hopper_files/image_refs.py` | Streaming Markdown image-reference parsing, identity resolution, and move rewrites. |
| `src/hopper_files/images.py` | Managed uploads, conservative image collection, and journaled image-aware moves. |
| `src/hopper_files/api/__init__.py` | API package marker. |
| `src/hopper_files/api/auth.py` | Login, logout, password change with the current password, and authenticated shell. |
| `src/hopper_files/api/request_body.py` | Streaming byte bounds for small control-request bodies. |
| `src/hopper_files/api/files.py` | Authenticated listing, raw download, and file operations. |
| `src/hopper_files/api/health.py` | Data-free health endpoint. |
| `src/hopper_files/api/search.py` | Authenticated search and derived-tag routes. |
| `src/hopper_files/api/trash.py` | Authenticated trash listing, deletion, and restoration. |
| `src/hopper_files/api/ui_state.py` | Authenticated interface-state read and revision-checked replacement. |
| `src/hopper_files/api/editor.py` | Authenticated file read/save and inert raster/PDF preview routes. |
| `src/hopper_files/api/buffers.py` | Authenticated editor-buffer registration, acknowledgements, and move events. |
| `src/hopper_files/api/changes.py` | Authenticated long-poll route that reports changes in named directories. |
| `src/hopper_files/api/images.py` | Authenticated image gallery, upload, preview, and pending-state routes. |
| `src/hopper_files/static/app.css` | Responsive Portuguese navigation and collection styles, served as written. |
| `src/hopper_files/static/app.js` | Interface code: navigation, collections, editor tabs, save state, preview client, and refresh after outside changes. An input of the generated bundle; not served directly. |
| `src/hopper_files/static/app.bundle.js` | Generated frontend entry bundle; the server answers `/app.js` with it. |
| `src/hopper_files/static/assets/` | Generated frontend chunks and PDF.js data; exact paths and hashes are in `frontend-bundle-manifest.json`. |
| `src/hopper_files/static/frontend-bundle-manifest.json` | SHA-256 inventory for generated frontend files. |
| `src/hopper_files/static/THIRD_PARTY_NOTICES.txt` | Licenses for bundled JavaScript packages and PDF.js resources. |
| `tests/conftest.py` | Synthetic instance fixtures. |
| `tests/test_access.py` | Host, origin, proxy, and base-path behavior. |
| `tests/test_app.py` | Health, Portuguese shell, and API route behavior. |
| `tests/test_auth.py` | Login, password change, CSRF, logout, expiry, and KDF behavior. |
| `tests/test_confinement.py` | Unit text, umask, environment, and loopback bind. |
| `tests/test_credentials.py` | Verifier encoding and local password command. |
| `tests/test_lifecycle.py` | Synthetic archive integrity, release selection/recovery, path-access refusal, removal, and preservation checks. |
| `tests/test_files.py` | Synthetic listing, streaming, names, copy, ZIP, limits, and recovery checks. |
| `tests/test_editor.py` | File-version, metadata preservation, conflict, limits, and post-publication tests. |
| `tests/test_isolation.py` | Cross-instance replay and password rotation. |
| `tests/test_limiter.py` | Persistent buckets, spoofing, and KDF saturation. |
| `tests/test_public_boundary.py` | Absence of site-specific markers, read from a private list outside the repository. |
| `tests/test_navigation.py` | Single base, visibility, address forms, links, permissions, and configuration version checks. |
| `tests/test_migration.py` | Synthetic version 1 migration, return, idempotence, journal preconditions, and installer ordering. |
| `tests/test_search.py` | Search, cursor, omission, and Markdown-tag contract checks. |
| `tests/test_trash.py` | Synthetic delete, restore, retention, quarantine, and metadata checks. |
| `tests/test_images.py` | Image references, upload, collection, joint restoration, and move checks. |
| `tests/test_image_collection_failures.py` | Scanner uncertainty, reference-size, and save-race preservation checks. |
| `tests/test_ui_state.py` | Schema, revision checks, process lock, persistence, and recovery checks. |
| `tests/test_changes.py` | Change notices: watched directories, create, delete, rename, modify, replace by rename, overflow, release, limits, and route errors. |
| `tests/test_http_regressions.py` | Regressions through the HTTP routes: ZIP temporary names, save conflicts and access metadata, stale interface state against moves and trash, reference rewrites with open buffers, and collection after a save. |
