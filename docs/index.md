# Hopper Files documentation

Start with the repository [README](../README.md): it says what Hopper Files is
and has the [installation steps](../README.md#install). The notes below give
the details. The specification is the contract; this index does not restate
it.

| Document | What it is for |
| --- | --- |
| [Operations](operations.md) | Commands, configuration, installation details, checking and opening an instance, updates, trash, removal, backup, and recovery. |
| [Remote access](operations.md#remote-access-behind-an-https-proxy) | HTTPS through a reverse proxy, with Tailscale Serve and Caddy examples. |
| [Security](security.md) | Server and filesystem boundaries, and the limits that remain open. |
| [Runtime](runtime.md) | How a running instance behaves: access, files, editor, trash, service unit, and state layout. |
| [Development](development.md) | Tests, locked dependencies, frontend, wheel, and release archives from this source tree alone. |
| [Validation](validation.md) | What was checked, where, and with which limits. |
| [Specification](specification.md) | Normative product requirements. |
| [Repository inventory](repository-inventory.md) | Every public source path and its purpose. |
