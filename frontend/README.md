# Frontend build

The browser application bundles CodeMirror 6, Markdown parsing and sanitizing,
and PDF.js into static files served by Hopper Files. The inputs are
`frontend/src/` and the interface code `src/hopper_files/static/app.js`, which
`frontend/src/entry.js` imports. The build writes the generated files to
`src/hopper_files/static`: `app.bundle.js`, the `assets/` directory,
`frontend-bundle-manifest.json`, and `THIRD_PARTY_NOTICES.txt`. The server
answers `/app.js` with `app.bundle.js`, so a change to either input reaches
the browser only after a rebuild. The product has no Node.js runtime.

## Reproducible build

Use Node.js `v24.21.0` with npm `11.19.0`. The tested Linux x86-64 Node archive
is published by the official Node.js release site:

- Archive: <https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz>
- Official checksums: <https://nodejs.org/dist/v24.21.0/SHASUMS256.txt>
- SHA-256: `fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6`
- Node.js license: `LICENSE` in that archive (MIT; SHA-256
  `5888dbb9a1d2b18f2c3e6c5f6af1b39de658372b402a0577b002777f14c62ace`;
  see the archive for third-party notices).
- npm `11.19.0` is included in the Node archive. Its `lib/node_modules/npm/LICENSE`
  records Artistic License 2.0 and the licenses for its bundled dependencies;
  the file SHA-256 is `7610d223851f421d315df5e77974f1c68a04b97e02060e5bbbcf13d95e3ca257`.

Check the archive's SHA-256, extract it outside the clone, and put its `bin`
directory first on `PATH` for these commands, for example:

```sh
tar -xJf node-v24.21.0-linux-x64.tar.xz -C "$HOME/.cache"
export PATH="$HOME/.cache/node-v24.21.0-linux-x64/bin:$PATH"
node --version
```

From the repository root, install the lockfile dependencies from the official
npm registry, then build and run the JavaScript fixtures:

```sh
npm --prefix frontend ci --ignore-scripts --registry=https://registry.npmjs.org/
npm --prefix frontend run build
npm --prefix frontend test
```

Commit the generated files together with the change to their inputs.

`package-lock.json` pins every package version, registry URL, and npm integrity
digest. The build emits a SHA-256 manifest for generated browser assets;
`scripts/verify_repository.py` and `scripts/verify_built_wheel.py` check those
files. It also generates `src/hopper_files/static/THIRD_PARTY_NOTICES.txt`
from the licenses of bundled packages and PDF.js data resources. Review that
output with each dependency update. The generator removes trailing horizontal
whitespace while retaining the license text, so the notice is easier to diff.

Node.js and npm are build tools only. `frontend/node_modules` is local build
state and is not part of the public source or wheel.
