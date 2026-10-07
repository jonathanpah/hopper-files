# Security Policy

Report suspected vulnerabilities in Hopper Files through GitHub's private vulnerability reporting.

## Report privately

Open [Report a vulnerability](https://github.com/jonathanpah/hopper-files/security/advisories/new), or select that option on the repository's [Security page](https://github.com/jonathanpah/hopper-files/security). A GitHub account is required.

Keep suspected vulnerabilities and exploit details out of public issues, discussions, and pull requests until disclosure is coordinated with the maintainer. Use public issues for ordinary bugs and proposals, and discussions for general questions.

Include:

- The affected release tag or commit and the relevant file, route, or documentation section.
- The operating system, Python version, access mode, proxy, and browser needed to reproduce the behavior, without private account details.
- A minimal reproduction using synthetic data and a disposable system.
- Expected and observed behavior, potential impact, and any uncertainty or limits in the evidence.

Do not submit passwords, access tokens, private keys, session cookies, personal files, or unredacted logs. Test only systems you are authorized to use.

## Scope and security expectations

This repository contains the Hopper Files server, its browser interface, the `hopper-files-admin` command, and their documentation. Relevant reports include flaws that let a request act without a valid session, reach files beyond what the instance's Linux account can reach, act on another instance, bypass the host, origin, or proxy checks, expose the password verifier or session secrets, or make an administrator command change more than it states.

The intended boundaries, detailed in [security](docs/security.md), are:

- Each instance runs as one existing Linux account, never root, and that account's Linux permissions are the only access limit.
- An instance listens only on a loopback address. In remote mode, it accepts requests only from the configured proxy address, with the expected forwarded headers.
- Every request that reads or changes files needs a valid session; every request that changes state also needs the matching origin and token.

Hopper Files does not replace the operating system's own controls. A report about a behavior that the account's Linux permissions already allow is not a vulnerability in Hopper Files.

## Versions and handling

Identify the version you used. A report is welcome even if you cannot safely check the latest version.

Jonathan Honorio reviews reports through the private reporting channel. Confirmed affected versions and fixes can be documented in repository changes, releases, or security advisories. Coordinate public disclosure through the private report. No response or remediation deadline is guaranteed.
