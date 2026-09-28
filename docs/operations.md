# Operations

This note explains how an administrator installs and runs Hopper Files with
the command `hopper-files-admin`. The [README](../README.md#install) has the
complete installation sequence; this note explains each step and the tasks
that come after it. It follows the code of `hopper_files.admin` and
`hopper_files.lifecycle`; the [specification](specification.md) remains the
contract.

The examples use one account and instance, `alice`, the configuration file
`/etc/opt/hopper-files/alice.json`, the state directory
`/home/alice/.local/state/hopper-files`, and port 8765.

## Commands

`hopper-files-admin --help` lists the commands, and
`hopper-files-admin COMMAND --help` lists the options of one command. Messages
are in Portuguese; the details of an error are in English.

| Command | Run as | What it does |
| --- | --- | --- |
| `install-shared --artifact FILE --sha256 HEX` | root, once | Installs the first release and creates `hopper-files-admin`. The release script in the README runs it. |
| `update-release --artifact FILE --sha256 HEX` | root | Installs a new release beside the active one, selects it, and restarts every registered instance. |
| `rollback-release --release-id ID` | root | Selects another installed release and restarts every registered instance. |
| `verify-release --release-id ID` | any account | Rechecks the files of an installed release without selecting it. |
| `register-instance --config FILE` | root | Checks the instance as its account, then writes, enables, and starts its service. |
| `update-instance --config FILE` | root | Applies an edited configuration and restarts the instance. |
| `remove-instance --instance-id ID` | root | Stops the instance and removes its service; keeps configuration, state, and documents. |
| `uninstall-shared` | root | Removes every service, both commands, and all releases; keeps configuration, state, and documents. |
| `set-password --config FILE` | the instance's account | Sets the password, or replaces a forgotten one. |
| `check --config FILE` | the instance's account | Checks the configuration, the account, the state directory, and the password runtime. |
| `purge-trash --config FILE` | the instance's account | Permanently deletes trash entries at least 30 days old. |
| `migrate-release`, `return-release` | root | Only for instances created by pre-release builds; see the [appendix](#appendix-instances-created-by-pre-release-builds). |

The root commands refuse to run as any other user. `set-password`, `check`, and
`purge-trash` refuse to run as any account other than the instance's, root
included; run them with `sudo -u ACCOUNT`. Two root commands never run at the
same time: each holds a lock while it works.

## Paths

The administrator chooses the configuration file and the state directory. The
commands manage these paths:

| Path | Role |
| --- | --- |
| `/opt/hopper-files/releases/<release-id>` | An installed release: source, dependencies, and records. Owned by root and read-only. |
| `/opt/hopper-files/current` | Link to the active release. |
| `/opt/hopper-files/.staging` | Temporary space while a release installs. |
| `/var/lib/hopper-files/instances/<instance-id>.json` | Registry record: configuration path, account, state directory, and unit name. |
| `/etc/systemd/system/hopper-files-<instance-id>.service` | The instance's systemd unit. |
| `/usr/local/bin/hopper-files-admin` | The administrator command. It always runs the active release. |
| `/usr/local/libexec/hopper-files-launch` | The program each unit starts. |
| `/run/lock/hopper-files-lifecycle.lock` | The lock of the root commands. |

## Before you install

### Which account

The service account is the Linux account whose files the instance shows,
usually the person's own login account. The instance can read and change
exactly what that account can, including its SSH keys, its credentials, and the
instance's own state. Keep the password private; see [security](security.md).

The account must exist and must not be root. Hopper Files does not create
accounts, add them to groups, or change permissions. When a check fails, the
message names the path and the access the account lacks.

### Where the configuration and the state live

- The configuration is a regular file, not a symbolic link, that the account
  can read. It holds no secret. Keep it owned by root with mode `0644`, for
  example `/etc/opt/hopper-files/alice.json`, so that a web session cannot
  change it.
- The state directory belongs to the account, with mode `0700`, for example
  `/home/alice/.local/state/hopper-files`. It holds the password verifier,
  sessions, interface state, journals, and the trash. The commands create it
  only when its parent exists and the account can write there, so create it
  first as that account:

  ```sh
  sudo -u alice mkdir -p -m 0700 /home/alice/.local/state/hopper-files
  ```

  A directory the account does not own is refused, because the account cannot
  set its mode.
- Moving an item to the trash from another filesystem copies it into the
  state directory. Put the state directory on the filesystem where the account
  keeps most of its files.

## Instance configuration

The configuration is a JSON object, version 2, with exactly these fields:

| Field | Rule |
| --- | --- |
| `version` | `2`. |
| `instanceId` | 1 to 64 letters, digits, `.`, `_`, or `-`, starting with a letter or digit. Unique on the host. It names the unit `hopper-files-<instanceId>.service`. |
| `serviceAccount` | The existing account the instance runs as, not root. |
| `timeZone` | An IANA time zone name, such as `UTC` or `Europe/Berlin`, used in generated file names and in the dates of the interface. There is no default; `timedatectl list-timezones` lists the names. |
| `sessionDurationSeconds` | How long a session lasts, in seconds, from 1 to 31622400 (366 days). |
| `stateDirectory` | Absolute path of a directory for this instance only, owned by the account, mode `0700`. |
| `cookieName` | Letters, digits, and ``!#$%&'*+-.^_`\|~``, for example `hf_alice`. Unique among the instances of the host, because browsers do not separate cookies by port. |
| `access.mode` | `local` or `remote`. |
| `access.bindHost` | A numeric loopback address, normally `127.0.0.1`. A name such as `localhost` is refused. |
| `access.bindPort` | A free TCP port, one per instance. |
| `access.baseUrl` | The address typed in the browser, ending with `/`. Local mode: `http://`, the `bindHost` address, `:`, the `bindPort`, and a path, normally `/`. Remote mode: `https://`, the public host name, the port when it is not 443, and a path, normally `/`. |
| `access.trustedProxyPeer` | `null` in local mode. In remote mode, the address the proxy connects from, normally `127.0.0.1`. |

The file is plain JSON: no comments and no other fields. A version 1 file, or a
file with `roots`, `system`, `protectedPaths`, or `creationPolicies`, comes from
a pre-release build; see the
[appendix](#appendix-instances-created-by-pre-release-builds).

Local mode, reached on the server or through an SSH tunnel:

```json
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
```

Remote mode, behind an HTTPS proxy on the same host; see
[remote access](#remote-access-behind-an-https-proxy):

```json
{
  "version": 2,
  "instanceId": "alice",
  "serviceAccount": "alice",
  "timeZone": "UTC",
  "sessionDurationSeconds": 86400,
  "stateDirectory": "/home/alice/.local/state/hopper-files",
  "cookieName": "hf_alice",
  "access": {
    "mode": "remote",
    "bindHost": "127.0.0.1",
    "bindPort": 8765,
    "baseUrl": "https://files.example.com/",
    "trustedProxyPeer": "127.0.0.1"
  }
}
```

When a rule fails, `check` and `register-instance` name it, for example
`local base URL must match the loopback listener`,
`base path must start and end with /`, or
`remote mode requires one trusted proxy peer`.

## Install and register

The [README](../README.md#install) has the commands. The order matters:

1. Build and install the release with the release script (`install`). It runs
   `install-shared` once from the source of the same tag. Afterwards,
   `hopper-files-admin` exists and always runs the active release. Install
   later releases with `update-release`; `install-shared` refuses while a
   shared installation exists.
2. Write the configuration.
3. Create the state directory as the account.
4. Register the instance.
5. Set the password as the account.
6. Check the instance as the account.

`install-shared` checks the archive against the SHA-256 you give it and every
file against the archive's manifest. It then installs the pinned dependencies
from `https://pypi.org/simple` with the distribution's `pip`, using
`--require-hashes` and `--only-binary=:all:`, records `installed.json` and the
pip report, makes the release owned by root and read-only, creates the two
commands, and selects the release. A release ID that is already installed is
never replaced with different bytes.

`register-instance` first checks, as the instance's account, the
configuration, read and search access to `/`, the state directory and its
format, and the password runtime. It prepares an empty state directory on the
first registration. It then writes the registry record and the unit, and
enables and starts the unit, which also starts at boot. If a step fails, it
removes the unit and the record it wrote; the configuration, the state, and
the documents stay. It reports success as soon as systemd starts the service.
A start that fails a few seconds later, for example on a port already in use,
is not undone; [check the service](#check-the-service) after registering.

`set-password` reads the new password twice from the terminal; it is never a
command argument. It ends every session of the instance. Later, the account's
user can change the password from the login page with the current one;
`set-password` remains the way to replace a forgotten password.

`check` validates the configuration, the account, the state directory, and the
password runtime. It needs a registered instance, or at least a state
directory that `set-password` prepared. It does not open the port: a port in
use shows only when the service starts.

## Check the service

```sh
systemctl status hopper-files-alice
sudo journalctl -u hopper-files-alice -n 20
curl -fsS http://127.0.0.1:8765/healthz
```

In local mode, the last command prints
`{"service":"hopper-files","status":"up"}`. In remote mode, run it through the
proxy instead; a direct request is refused. After a registration, an update, or
a restart, the service can take a few seconds to answer.

Each start writes a log line with the active release ID, its Git tree, and the
path of the running code, starting with `Hopper Files active release=`.
`systemctl show hopper-files-alice` shows the unit as systemd loaded it.

The unit does not restart the process after a crash. If `systemctl status`
shows it stopped, read the log, then start it again:

```sh
sudo systemctl restart hopper-files-alice
```

To watch a start in the terminal, stop the unit, which holds the port, and run
the launcher in the foreground as the account. Stop it with Ctrl+C, then start
the unit again:

```sh
sudo systemctl stop hopper-files-alice
sudo -u alice env HOPPER_FILES_INSTANCE=/etc/opt/hopper-files/alice.json \
  /usr/local/libexec/hopper-files-launch
sudo systemctl start hopper-files-alice
```

## Open the interface

Open `baseUrl` exactly as configured. The host and the port must match it:

- In local mode, open `http://127.0.0.1:8765/` in a browser on the server, or
  from another computer through an SSH tunnel that uses the same port number:

  ```sh
  ssh -N -L 8765:127.0.0.1:8765 USER@SERVER
  ```

  Then open `http://127.0.0.1:8765/` on that computer. `localhost` and a
  different local port are refused with `403`.
- In remote mode, open the HTTPS address of the proxy.

With a base path other than `/`, such as `/files/`, include the final `/`.
Without it, the instance answers `401` with
`{"error":"authentication_required"}`, or `404` with
`{"error":"not_available"}` when a session is open, instead of redirecting. A proxy can
redirect `/files` to `/files/`; the Caddy example below does.

## Remote access behind an HTTPS proxy

Use local mode when the browser runs on the server or reaches it through an SSH
tunnel. Use remote mode for HTTPS without a tunnel. An instance uses one mode at
a time.

In remote mode, the browser talks HTTPS to a reverse proxy on the same host,
and the proxy forwards plain HTTP to `http://127.0.0.1:PORT`. Hopper Files
listens only on a loopback address, so the proxy must run on the same host; a
proxy in a container works only with host networking. The instance accepts a
request only when it comes from `trustedProxyPeer` and carries the forwarded
headers below. Any other request gets `403`, including a direct request to the
port from the server itself.

### Configure the instance

Set `access` as in the remote example above: `mode` `remote`, `baseUrl` the
public HTTPS address, and `trustedProxyPeer` `127.0.0.1`. For an instance that
is already registered, apply the change:

```sh
sudo hopper-files-admin update-instance --config /etc/opt/hopper-files/alice.json
```

### What any proxy must do

| Requirement | Why |
| --- | --- |
| Connect to `127.0.0.1:PORT`, not to `localhost`. | The instance accepts only connections from `trustedProxyPeer`. `localhost` can resolve to `::1`, where nothing listens. |
| Keep `Host` equal to the public host, with the port when it is not 443. | The instance compares `Host` with `baseUrl`. |
| Set `X-Forwarded-Host` to the same value. | The instance compares it with `baseUrl` too. |
| Set `X-Forwarded-Proto: https`, in lowercase. | Remote mode serves only HTTPS requests. |
| Send `Host`, `Origin`, and each `X-Forwarded-*` header at most once. Put several client addresses in one `X-Forwarded-For` line, separated by commas. | A repeated header is refused. The instance uses the last address of `X-Forwarded-For`. |
| Set `X-Forwarded-For` to the client address. | Login limits count attempts per client address; without it, every client shares the proxy's count. |
| Pass `Origin` unchanged. | Every `POST`, `PUT`, `PATCH`, and `DELETE` needs the exact origin `https://HOST` (with `:PORT` when it is not 443). |
| Keep the full path, including the base path. Do not remove a prefix. | Every route lives under the base path. |
| Leave `Location` and `Set-Cookie` unchanged. | Redirects already point under `baseUrl`, and the cookie is scoped to the base path with `Secure`. |
| Allow request bodies up to 400 MiB and long streamed responses. | Uploads accept up to 400 MiB per request; downloads stream large files. |

Plain HTTP/1.1 is enough; Hopper Files uses no WebSocket.

### Example: Tailscale Serve

[Tailscale Serve](https://tailscale.com/kb/1312/serve) publishes a local
service over HTTPS inside your tailnet only. It needs HTTPS certificates
enabled for the tailnet. On the server:

```sh
sudo tailscale serve --bg 8765
tailscale serve status
```

The service answers at `https://MACHINE.TAILNET.ts.net/`, where `MACHINE` and
`TAILNET` are your machine and tailnet names. Set `baseUrl` to that address and
`trustedProxyPeer` to `127.0.0.1`. Serve keeps `Host` and sets the forwarded
headers.

Mount each instance at `/` of its own HTTPS port, as in these commands, without
`--set-path`. Serve then forwards the full path, so `baseUrl` can also have a
base path, such as `https://MACHINE.TAILNET.ts.net/files/`. For a second
instance on port 8766:

```sh
sudo tailscale serve --bg --https=8443 8766
```

Its `baseUrl` is `https://MACHINE.TAILNET.ts.net:8443/`, with or without a base
path.

### Example: Caddy

[Caddy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
terminates HTTPS and forwards to the instance. On Debian 13, install it with
`sudo apt install caddy`, then write `/etc/caddy/Caddyfile`:

```caddyfile
files.example.com {
	tls internal
	reverse_proxy 127.0.0.1:8765 {
		# Caddy already does this; the lines show what Hopper Files needs.
		header_up Host {hostport}
		header_up X-Forwarded-Host {hostport}
		header_up X-Forwarded-Proto {scheme}
	}
}
```

Reload it with `sudo systemctl reload caddy`. Caddy also sets
`X-Forwarded-For` to the client address.

`tls internal` makes Caddy sign the certificate with its own local authority,
which is enough to try the setup. Browsers do not trust that authority until
you install its root certificate. With a public DNS name that points to the
server and ports 80 and 443 open, remove that line and Caddy obtains a publicly
trusted certificate.

With a base path, keep the prefix: use `handle`, not `handle_path`, and
redirect the address without the final `/`:

```caddyfile
example.com {
	tls internal
	redir /files /files/
	handle /files/* {
		reverse_proxy 127.0.0.1:8765
	}
}
```

Here `baseUrl` is `https://example.com/files/`.

### Check the proxy

```sh
curl -fsS https://files.example.com/healthz
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/healthz
```

The first command prints `{"service":"hopper-files","status":"up"}`. The second
prints `403`: in remote mode, a direct request is refused, as expected.

With `tls internal`, `curl` needs Caddy's root certificate, and the name must
reach the server. For a check on the server itself, with the Debian package:

```sh
sudo curl -fsS --resolve files.example.com:443:127.0.0.1 \
  --cacert /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt \
  https://files.example.com/healthz
```

### Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `{"error":"forbidden"}` (403) on every page through the proxy | The proxy connects from an address other than `trustedProxyPeer`; `X-Forwarded-Proto` is missing or not exactly `https`; `X-Forwarded-Host` is missing or differs from the host of `baseUrl`; the proxy rewrote `Host`; or a header arrives twice. |
| 403 only when signing in or saving | The proxy removes or changes `Origin`. |
| `{"error":"not_found"}` (404) everywhere | The proxy removes the base path, or `baseUrl` has a different path. |
| `{"error":"authentication_required"}` (401) at the base path | The address lacks its final `/`. |
| `{"error":"not_available"}` (404) at the base path while signed in | The address lacks its final `/`. |
| 502 from the proxy | The instance is not running (`systemctl status hopper-files-ID`), or the proxy targets `localhost` or `::1` while the instance listens on `127.0.0.1`. |
| 403 when opening the port directly | Expected in remote mode. |

### Several instances on one host

Each instance has its own port and its own `cookieName`, and also its own host
name, HTTPS port, or base path.

### Security notes

- Tailscale Serve publishes only inside your tailnet. `tailscale funnel`, or a
  Caddy site with a public name, puts the login page on the Internet; the
  password and the login limits are then the only protection.
- Identity headers added by a proxy do not replace the instance password.
- Login limits: 5 attempts per client address, then 1 per minute; 20 attempts
  per instance, then 1 every 15 seconds.

## Change an instance

After editing the configuration file, apply it. The command checks the
instance as its account, rewrites the unit, and restarts the service:

```sh
sudo hopper-files-admin update-instance --config /etc/opt/hopper-files/alice.json
```

It does not edit the file and does not move documents. It refuses a change of
the service account, the state directory, or the path of the configuration
file. For those changes, remove the instance and register it again with the
new file:

```sh
sudo hopper-files-admin remove-instance --instance-id alice
sudo hopper-files-admin register-instance --config /etc/opt/hopper-files/alice.json
```

The old state directory stays where it was. A new state directory starts
empty: set the password again with `set-password`. Favorites, labels, tabs,
and the trash stay in the old directory.

A second instance needs its own configuration file, `instanceId`, state
directory, port, and `cookieName`. Register it the same way.

## Update and roll back

To update, build the new release with the release script from the
[README](../README.md#install) and install it:

1. In the clone, run `git fetch --tags`.
2. Open the page of the new release and set `TAG`, `RELEASE_ID`, and
   `EXPECTED_ARCHIVE_SHA256` in the script to its values. If the README of the
   new release shows a different script, use that one.
3. Run `bash ~/hopper-files-release.sh update`.

`update-release` installs the new release beside the active one and checks it
before it selects it. It then restarts every registered instance. If
`systemctl` reports a failed restart, it selects the previous release again
and restarts the instances with it. It reports success as soon as systemd
starts the services, so a process that exits a few seconds later is not
undone: check each instance afterwards, and return with `rollback-release` if
one does not answer. The same archive can be retried when its release ID is already installed
with the same bytes; different bytes for an installed ID are refused.

To return to a release that is still installed:

```sh
sudo hopper-files-admin rollback-release --release-id hopper-files-0.1.0
```

`rollback-release` rechecks every file of that release before it selects it,
restarts every registered instance, and refuses a release that is already
active. `verify-release --release-id ID` runs the same check without selecting
anything.

Neither command changes documents, configuration, or the format of the state.

## Trash

Deleting an item moves it into the trash, inside the state directory. Nothing
empties the trash on its own. An entry becomes removable 30 days after
deletion; `purge-trash` permanently deletes those entries, run as the
instance's account:

```sh
sudo -u alice hopper-files-admin purge-trash --config /etc/opt/hopper-files/alice.json
```

It skips entries whose metadata is damaged (quarantined) and entries with an
unfinished operation. To run it every day, add a line to that account's
crontab with `sudo crontab -u alice -e`. If `crontab` is missing, install it
with `sudo apt install cron`:

```crontab
17 3 * * * /usr/local/bin/hopper-files-admin purge-trash --config /etc/opt/hopper-files/alice.json
```

The interface has no button for early permanent deletion.

## Remove and uninstall

```sh
sudo hopper-files-admin remove-instance --instance-id alice
sudo hopper-files-admin uninstall-shared
```

`remove-instance` stops and disables the unit, and deletes the unit and the
registry record. `uninstall-shared` does that for every registered instance,
then deletes both commands, every release, and the registry. Both keep every
configuration file, state directory, and document; no command deletes state
or documents. After `uninstall-shared`, remove the configuration files and
state directories by hand if you no longer need them.

## Back up

Back up each configuration file and each state directory. The state directory
holds the password verifier, the interface state (favorites, labels, tabs), and
the trash. Releases can be rebuilt from their tags.

## Recovery

| Event | What remains | What to do |
| --- | --- | --- |
| `install-shared` fails before selecting the release | No release is active. | Fix the reported archive, digest, pip, or path error and run it again. Do not point `current` at a partial tree by hand. |
| `update-release` or `rollback-release` fails while restarting, as `systemctl` reports it | Documents, configuration, and state are untouched. | The command selects the previous release again and restarts the instances with it. If that also fails, the error names the release left active; inspect it before another attempt. |
| An instance stops a few seconds after a registration, an update, or a restart | The command already reported success. Documents, configuration, and state are untouched. | Read `sudo journalctl -u hopper-files-ID`. After an update, return with `rollback-release`. Otherwise fix the cause, such as a port in use, and run `sudo systemctl restart hopper-files-ID`. |
| `register-instance` fails | The configuration, the documents, and any state stay. | The unit and registry record of the attempt are removed. Fix the reported problem and register again; the state the attempt created can stay. |
| `update-instance` fails | The edited configuration file stays as written. Documents and state stay. | When the command refuses the edit, nothing restarts: the running process keeps its previous settings, and its next restart, for example by `update-release`, reads the edited file. Fix or revert the file and run `update-instance` again. Do not delete the state directory to compensate. |
| `remove-instance` fails | Configuration, state, and documents stay. | If the unit file was already removed, the command writes it back and enables it again. |
| `uninstall-shared` fails while disabling units | Shared files are still present. | Removed units are restored and enabled again. Shared code is deleted only after that step succeeds. |
| A file operation reports an item as `indeterminate` | The stored result stays, and a destination that may already exist stays. | Repeating the same operation returns the stored result and does not create a second copy. Do not delete both the source and the destination. |
| The file-operation journal is damaged or unreadable | No item list is available, and a copy may already exist. | The response is `503` with `{"error":"indeterminate"}`. Do not delete both copies and do not start the operation again under a new token; inspect the journal in the state directory. |
| A trash entry is quarantined | Its content stays. | Restore and purge skip it. Inspect that entry; there is no second delete command. |
| The interface state cannot be recovered | The existing state file stays. | The service refuses to continue rather than replace the state with an empty one. |

## Appendix: instances created by pre-release builds

Skip this section for a new installation. It applies only to instances that a
pre-release build created with configured roots (configuration version 1).
Such an instance keeps version 1 interface state, trash metadata, and
configuration. The current release refuses to serve it and never writes empty
state over it.

### Migration and return

The active release is shared, so all registered instances move together:

```sh
sudo hopper-files-admin migrate-release --artifact ARCHIVE --sha256 SHA256
sudo hopper-files-admin return-release --release-id PREVIOUS_RELEASE_ID
```

Each command prints the instances, service accounts, and configuration and
state paths, and asks for confirmation; type `sim` to continue. `--yes`
records a confirmation already given. The command first checks every state as
its service account and refuses, naming them, while any operation, trash,
interface-state, or image-move journal, or editor-buffer move, is unfinished;
recover those under the release that created them. It then stops all
instances, converts each state as its service account, saves the version 1
configuration as `NAME.v1-before-single-base.json` beside the original (same
owner, group, and mode) and the previous unit under
`/var/lib/hopper-files/migration/`, writes the version 2 configuration and
the new unit, selects the new release, and starts the instances. If any
instance fails, the instances already converted return and the previous
release stays selected. `return-release` reverses the conversion under the
saved configuration, restores the saved configuration and unit, and selects
the previous release before starting.

The installed `hopper-files-admin` runs the active release, and a root-based
release has no `migrate-release`. Run the first migration with the new
release's own code: check the archive digest, extract the archive into a new
directory owned by root, and start its administrator module. When its
`requirements.lock` matches the active release's, the active release's
dependencies serve it:

```sh
sudo /usr/bin/python3 -I -B -c 'import sys; sys.path[:0] = sys.argv[1:3]; sys.argv = ["hopper-files-admin", *sys.argv[3:]]; from hopper_files.admin import main; raise SystemExit(main())' \
  EXTRACTED/source/src /opt/hopper-files/releases/ACTIVE_RELEASE_ID/dependencies \
  migrate-release --artifact ARCHIVE --sha256 SHA256
```

After the switch, the installed command runs the new release, which provides
`return-release`.

The conversion rules, counts, and reports are described in
[runtime](runtime.md#migration-from-the-root-based-model). Every run keeps the
replaced documents and a report under `STATE/migration/`, and both directions
can be repeated safely. A record the previous release cannot show, such as a
mark in a place it did not expose, stays in the return report and comes back
on the next migration; trash it cannot represent stays quarantined there
rather than being purged or restored. Test the procedure on synthetic copies
before using it on real state.

### Rolling back to a build without newer state files

Monitored tag folders live in `tag-folders.json`, outside the interface state
document. A build that predates them ignores the file, so a rollback needs no
change for it, and the list applies again after a return.

The optional interface preferences `ignoredTags` and `sidebarWidth` are
refused by builds that predate them. Before rolling back to such a build,
remove only those two keys with the newer release's own state code, as each
instance's service account. The change goes through the normal state lock and
journal and keeps every other field; it also works after the rollback, while
the newer release is still installed:

```sh
NEWER=/opt/hopper-files/releases/NEWER_RELEASE_ID
sudo -u SERVICE_ACCOUNT /usr/bin/python3 -B -c '
import sys; sys.path[:0] = [sys.argv[1] + "/source/src", sys.argv[1] + "/dependencies"]
from pathlib import Path
from hopper_files.state import mutate_ui_state
def strip(document):
    for key in ("ignoredTags", "sidebarWidth"):
        document["preferences"].pop(key, None)
print(mutate_ui_state(Path(sys.argv[2]), strip)["stateRevision"])
' "$NEWER" STATE_DIRECTORY
```
