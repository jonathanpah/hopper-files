# Contributing to Hopper Files

Contributions can fix defects, improve the documentation, verify behavior on another system, or propose a specific change. All public project content, including code comments, is written in English. The interface itself is in Portuguese.

## Start with the problem

For suspected vulnerabilities, follow the [Security Policy](SECURITY.md) and report privately. Do not include security-sensitive details in public issues, discussions, or pull requests before coordinating disclosure.

Use a bug report for a reproducible failure and a proposal for a specific change. Use [Discussions](https://github.com/jonathanpah/hopper-files/discussions) for questions or early ideas. Search existing issues before opening another report about the same behavior.

For a change in product behavior, explain the current behavior, the problem it creates, the desired behavior, evidence, and tradeoffs. [The specification](docs/specification.md) is the normative contract; a proposal is not approval to change it. Small documentation corrections can go directly to a pull request.

## Preserve the design

- An instance runs as one existing Linux account, never root. That account's Linux permissions are the only access limit, and the instance password is the only application barrier. Do not add application-level path restrictions unless the specification changes.
- Keep the instance listening on a loopback address. Remote access goes through an HTTPS proxy on the same host, as [operations](docs/operations.md) describes.
- Keep the specification, [operations](docs/operations.md), [runtime](docs/runtime.md), and [security](docs/security.md) consistent with the code in the same change.
- Keep code comments short and generic. Explain a rule or a non-obvious reason; leave out history, test sessions, and review notes.
- Do not claim performance or compatibility improvements without comparable measurements.

## Open a pull request

1. Fork the repository to your GitHub account. A fork is your own copy that you can edit.
2. Create a branch in your fork for one coherent change.
3. Edit the files and commit the change with a descriptive message.
4. Open a pull request from your branch to this repository's `main` branch. Link the relevant issue, if one exists.
5. Explain the problem, resulting behavior, verification, and limitations. Respond to review on the same branch.

A pull request proposes a change; it does not change this repository until the maintainer merges it.

## Verify proportionately

[Development](docs/development.md) describes the checks. Run the Python tests and `scripts/verify_repository.py` for every change. Also run the integration scripts when a change touches HTTP routes, state, or file operations, and the frontend tests and build when it touches `frontend/` or `src/hopper_files/static/app.js`.

Use synthetic data and disposable systems. Never point a check at real files, and never submit passwords, access tokens, private keys, personal data, or unredacted logs.

## Review and releases

Jonathan Honorio maintains the project and decides whether a change is accepted. Reviews consider the stated problem, evidence, consistency with the design, and maintenance cost. Review does not imply a promised response time or acceptance.

Accepted changes are merged into `main`. A release identifies a tagged version, its archive SHA-256, and its known limitations. Installing or updating remains a user's decision.

By submitting a contribution, you agree that your contribution is provided under this repository's [MIT License](LICENSE). Follow the [Code of Conduct](CODE_OF_CONDUCT.md).
