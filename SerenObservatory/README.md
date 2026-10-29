# SerenObservatory

**The per-node management plane for your Seren cluster.** One small service
on each box that knows how to start, stop, check, and report on the things
running there - so the cluster head can drive the whole constellation without
SSH-ing into every node by hand.

You don't usually talk to the observatory directly. It's a *plane*, not a
destination: [SerenLodestar](https://github.com/ChadRoesler/SerenLodestar) (the
cluster head) talks to it, aggregates every node's observatory into one API, and
serves the dashboard. The observatory is the thing on the far end that actually does the
work on each machine.

It's manifest-driven - it reads `~/.seren/services/*.json` to learn what
lives on its node, and dispatches lifecycle actions from there. Drop a new
service manifest, the observatory knows about it. No code change. (An
install under a Starwright root reads `<root>/manifests/*.json` instead -
see `server.manifests_dir` under Config.)

---

## Read this first: the safety interlock

The observatory can restart services and trigger a sudoers-backed reboot. That's a
lot of power for an HTTP endpoint, so the observatory treats its auth token as a
**safety interlock, not a convenience knob.**

- The token lives in `~/.seren/secrets.json` (chmod 600) as
  `{"observatory_token": "..."}`, written by the installer when you pass
  `--gen-token` or `--token` (`-GenToken` / `-Token` on Windows), or by hand.
  It is **not** a config field - you won't find it in the
  yaml, on purpose. Putting it there would add a second, weaker path to the
  one thing that gates rebooting your hardware.
- The file's **location** can move (the token itself still can't): set
  `SEREN_OBSERVATORY_SECRETS=/path/to/secrets.json` in the environment, or
  `server.secrets_path` in the yaml. Env wins over the yaml; both win over the
  default `~/.seren/secrets.json`. `~` is expanded. That's for per-install
  roots (`~/seren/<install name>/...`), so two clusters on one host never
  share a token. Same rules for the moved file: `{"observatory_token": "..."}`,
  chmod 600.
- **Until that token exists, the observatory fails CLOSED on anything that
  mutates.** Reads stay open (so monitoring still works on a fresh node), but
  every start/stop/restart/reboot returns `503` until you've provisioned a
  token. An unprovisioned observatory on the network is never an open
  reboot-button.

So install it with a token, or write the file. Before that, the observatory
is a read-only status reporter. After that, it's the full
plane - and every mutating call needs `Authorization: Bearer <token>`.

This is also why the observatory binds `0.0.0.0` by default (the opposite of
SerenMargin's localhost-only). It's *meant* to be reached across the trusted
LAN by the cluster head. The interlock - not the bind address - is what keeps
it safe.

---

## Quick start

```bash
# From the shared setup scripts (installs from the GitHub release by default):
bash seren-observatory-setup.sh

# Want it to start on boot, too?
bash seren-observatory-setup.sh --service

# With a token, so the mutating endpoints come alive (written to ~/.seren/secrets.json):
bash seren-observatory-setup.sh --service --gen-token

# Or run it straight, zero config:
python -m seren_observatory

# Or with a config file:
cp seren-observatory.yaml.sample seren-observatory.yaml
python -m seren_observatory --config seren-observatory.yaml
```

Defaults: `0.0.0.0:7777`. Reads `~/.seren/node.json` and the service
manifests in `~/.seren/services/`.

---

## Using it (the HTTP API)

Two endpoints are public (no token) so the cluster head can liveness-check a
node before it's provisioned:

```bash
curl localhost:7777/api/v1/system/ping       # → {"ok": true}
curl localhost:7777/api/v1/system/version
```

Everything else needs the bearer token:

```bash
TOKEN=$(jq -r .observatory_token ~/.seren/secrets.json)   # or wherever SEREN_OBSERVATORY_SECRETS / server.secrets_path points

# What's on this node, and how's it doing?
curl -H "Authorization: Bearer $TOKEN" localhost:7777/api/v1/system/node
curl -H "Authorization: Bearer $TOKEN" localhost:7777/api/v1/system/services
curl -H "Authorization: Bearer $TOKEN" localhost:7777/api/v1/system/health
curl -H "Authorization: Bearer $TOKEN" localhost:7777/api/v1/system/carabiners   # the .kbh clips on the box

# Drive a specific service (start/stop/restart/health/status/logs/manifest):
curl -H "Authorization: Bearer $TOKEN" \
  -X POST localhost:7777/api/v1/service/llama/restart
```

There's a browsable info page at `/` and interactive docs at `/docs` (the
description is public; calling anything from it still needs the token).
Install a service and its lifecycle verbs are live on the next request - no
restart, the manifest is read when the call arrives.
The root page shows your auth state up front - "configured" or "DISABLED (no
token)" - so you can see at a glance whether the interlock is armed.

**Orchestrated services.** llama, kokoro, whisper and comfy are started by
Lodestar when someone needs them and stopped when nobody does, so between uses
they are off on purpose. A manifest with `"orchestrated": true` (the node
installer writes it for those four; an `ensure` that starts a service sets it
too) reports `idle` instead of `not_running`: `/system/health` lists it under
`idle`, stays `ok`, and the glance shows a grey "idle" chip with an "on demand"
badge rather than a red one. Flip it by hand with
`POST /service/<name>/orchestrated {"orchestrated": true|false}`.

**The senses on a node.** Beyond start/stop/health, three services have
routes of their own that Lodestar talks through: whisper
(`POST /service/whisper/transcribe`, a clip in, words out), the listener
(`POST /service/listener/listen` and `/compare`, a clip and a question in,
words about the sound out: an audio-language model on a node with the memory
for it) and the speaker (`POST /service/speaker/speak` with `{text, voice}`,
a WAV out in the voice the node has; `GET /service/speaker/voices` lists
them). Each goes to the service on the box at its manifest's port, and each
answers with a status when it cannot: 503 while it loads or when it is down,
502 when it refused what it was sent.

**Upgrades.** `POST /service/<name>/upgrade {"version": "5.0.2"}` upgrades a
pip-installed service as a job (`GET /upgrades/<id>` follows it): the
service's own snapshot, its config and `.env` files held, `pip install
--upgrade` with its extras plus `seren-sinew` and `seren-meninges`, an import
check before any restart, then restart, health, and the config put back if
anything changed it. Anything failing is rolled back: old versions, old config,
restarted. Where the service came from is read from the Starwright install
ledger (`~/.seren/installed`), so the GPU daemons the node installer builds are
"not upgradable" here, and the answer says so. `"dry_run": true` returns pip's
own plan. The Observatory can upgrade itself when something will start it
again: on systemd the restart is handed to systemd; on Windows it checks that
NSSM is minding it with a Restart exit policy and exits, and NSSM starts the
new version. Either way the Observatory that comes back closes the job. Run
any other way (a pid file, a launcher) it refuses and says to use its
Starwright card. Lodestar drives this across the cluster
(`POST /api/v1/upgrade` there), with a copy pulled off the box first.
Before anything is touched, the apps the new version talks to are checked:
a reader lists them in its pyproject as a `peers` extra nobody installs
(`peers = ["seren-memory>=5.2"]`), and when one is in the cluster but older,
the job ends `held`, unchanged, with the reason; the installed metadata is
checked again after pip. Lodestar sends the cluster's versions as `peers`;
asked by hand it is this node's own. `GET /upgrades/versions` lists every
service here an upgrade can see, its version and what it needs.

---

## On Windows

A Windows box (a desktop running Symposium and a few NSSM services) is a
node like any other, with three differences worth knowing:

- **Services are `windows_service` or `launcher` manifests**, driven with
  `sc.exe` through a per-service grant the Starwright card writes, or started
  detached for a per-user app. Their memory, CPU and uptime come from Win32
  (`seren_observatory.winproc`: the service's whole process tree, since an
  NSSM service is nssm.exe, the venv's launcher and the interpreter), and the
  box's memory and uptime on `/system/node` likewise. There is no load
  average on Windows; the key is left out.
- **A closed port is looked up, not probed.** With the firewall up, Windows
  drops a connection to a closed port instead of refusing it, loopback
  included, so a probe of a down service would wait its whole timeout. The
  Observatory asks the TCP table first and answers "nothing is listening on
  port N" at once. The same table drives the port wait in a restart (a bind
  test with `SO_REUSEADDR` always succeeds on Windows).
- **Reboot is `shutdown /r /t <seconds>`**, cancel is `shutdown /a`; the
  service's account needs the shut-down privilege.

## What it logs (and where)

Every request is logged - timing, status, and full tracebacks on 500s - to
**both** stderr (so `journalctl` catches it) and a rotating file at
`~/seren-logs/observatory-requests.log` (so you can read it without sudo). Auth
rejections are logged too, which turns out to be the single most useful debug
signal: "the dashboard is failing - is the token wrong, or is the route
actually 500-ing?" The log tells you which.

---

## Config

See `seren-observatory.yaml.sample`. It follows the Seren convention (same shape as
SerenMemory and SerenMargin): a `server:` block, resolved `--config` →
`$SEREN_AGENT_CONFIG` → `~/seren-observatory/seren-observatory.yaml` → built-in defaults.

The yaml carries host/port, *where* the secrets file is - the token itself
is not here (see the interlock section above) - and where the service
manifests are. Fields you might touch:

- `server.host` (default `0.0.0.0` - the cluster-plane bind)
- `server.port` (default 7777)
- `server.secrets_path` (default `~/.seren/secrets.json`)
- `server.manifests_dir` (default `~/.seren/services`) - this install's
  roster: the observatory lists, starts and reclaims the `*.json` in it.
  Starwright sets it to `<install root>/manifests` so two installs on one host
  keep separate rosters. When it's moved, `~/.seren/services` is still read
  too, as the **box** roster: the GPU daemons the node installers write there
  (whisper, llama, kokoro) belong to the box, so every install's observatory
  on the host lists them, and reclaim from any of them stops them - the GPU is
  the box's. A name in both rosters is the install's. Each manifest in
  `/api/v1/system/services` carries `_roster: "install"` or `"box"`.
  `node.json` doesn't move either: a node is one box, so that stays at
  `~/.seren/node.json`.

Env vars override file values for systemd: `AGENT_HOST`/`AGENT_PORT`, or the
namespaced `SEREN_AGENT_HOST`/`SEREN_AGENT_PORT`; `SEREN_OBSERVATORY_SECRETS`
for the secrets file; `SEREN_OBSERVATORY_MANIFESTS` for the roster. `~` is
expanded in both paths. The root page and the fail-closed `503` both name the
secrets file the observatory actually resolved, so if they point somewhere you
didn't expect, that's the path it's reading. The root page names the roster
directory the same way - if a service you installed isn't listed, check that
line first.

---

## Deployment

The shared `setup-observatory-service.{sh,ps1}` wrappers install the observatory as a
systemd service (Linux), a launchd observatory (macOS), or an NSSM service
(Windows) - all running as your user so paths and caches resolve to your
profile. One node, one observatory, runs on boot. The cluster head finds it across
the LAN.

---

## What this is part of

SerenObservatory is a piece of [Seren](https://github.com/ChadRoesler) - a fully
self-hosted local AI companion stack. It's the per-node muscle: the cluster
head ([SerenLodestar](https://github.com/ChadRoesler/SerenLodestar)) is the brain that
aggregates and decides; the observatory is what actually touches each machine. You
run one on every node in your cluster.

On its own it's a tidy, auth-gated, manifest-driven service manager for a
single box. As part of the constellation, it's how the whole thing becomes
one cluster instead of a pile of separate machines.

The shapes this service answers with are the dataclasses in
`seren_sinew.cluster.dtos`, the same ones Lodestar parses. `tests/
test_wire_contract.py` parses every polled route's real answer with them
strictly, so a key added here that the head does not know fails a test here
rather than vanishing on the head.

Rip it and win.