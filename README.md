# SerenAgent

**The per-node management plane for your Seren cluster.** One small service
on each box that knows how to start, stop, check, and report on the things
running there - so the cluster head can drive the whole constellation without
SSH-ing into every node by hand.

You don't usually talk to the agent directly. It's a *plane*, not a
destination: [SerenRuntimeHost](https://github.com/ChadRoesler) (the cluster
head) talks to it, aggregates every node's agents into one API, and serves
the dashboard. The agent is the thing on the far end that actually does the
work on each machine.

It's manifest-driven - it reads `~/.seren/services/*.json` to learn what
lives on its node, and dispatches lifecycle actions from there. Drop a new
service manifest, the agent knows about it. No code change.

---

## Read this first: the safety interlock

The agent can restart services and trigger a sudoers-backed reboot. That's a
lot of power for an HTTP endpoint, so the agent treats its auth token as a
**safety interlock, not a convenience knob.**

- The token lives in `~/.seren/secrets.json` (chmod 600), written by
  `seren-secrets.sh`. It is **not** a config field - you won't find it in the
  yaml, on purpose. Putting it there would add a second, weaker path to the
  one thing that gates rebooting your hardware.
- **Until that token exists, the agent fails CLOSED on anything that
  mutates.** Reads stay open (so monitoring still works on a fresh node), but
  every start/stop/restart/reboot returns `503` until you've provisioned a
  token. An unprovisioned agent on the network is never an open
  reboot-button.

So the first thing you do on a new node is run `seren-secrets.sh`. Before
that, the agent is a read-only status reporter. After that, it's the full
plane - and every mutating call needs `Authorization: Bearer <token>`.

This is also why the agent binds `0.0.0.0` by default (the opposite of
SerenMargin's localhost-only). It's *meant* to be reached across the trusted
LAN by the cluster head. The interlock - not the bind address - is what keeps
it safe.

---

## Quick start

```bash
# From the shared setup scripts (installs from the GitHub release by default):
bash seren-agent-setup.sh

# Want it to start on boot, too?
bash seren-agent-setup.sh --service

# Provision the token so mutating endpoints come alive:
bash seren-secrets.sh

# Or run it straight, zero config:
python -m seren_agent

# Or with a config file:
cp seren-agent.yaml.sample seren-agent.yaml
python -m seren_agent --config seren-agent.yaml
```

Defaults: `0.0.0.0:7777`. Reads its node + service manifests from
`~/.seren/`.

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
TOKEN=$(jq -r .agent_token ~/.seren/secrets.json)

# What's on this node, and how's it doing?
curl -H "Authorization: Bearer $TOKEN" localhost:7777/api/v1/system/node
curl -H "Authorization: Bearer $TOKEN" localhost:7777/api/v1/system/services
curl -H "Authorization: Bearer $TOKEN" localhost:7777/api/v1/system/health

# Drive a specific service (start/stop/restart/health/status/logs/manifest):
curl -H "Authorization: Bearer $TOKEN" \
  -X POST localhost:7777/api/v1/service/llama/restart
```

There's a browsable info page at `/` and full interactive docs at `/docs`.
The root page shows your auth state up front - "configured" or "DISABLED (no
token)" - so you can see at a glance whether the interlock is armed.

---

## What it logs (and where)

Every request is logged - timing, status, and full tracebacks on 500s - to
**both** stderr (so `journalctl` catches it) and a rotating file at
`~/seren-logs/agent-requests.log` (so you can read it without sudo). Auth
rejections are logged too, which turns out to be the single most useful debug
signal: "the dashboard is failing - is the token wrong, or is the route
actually 500-ing?" The log tells you which.

---

## Config

See `seren-agent.yaml.sample`. It follows the Seren convention (same shape as
SerenMemory and SerenMargin): a `server:` block, resolved `--config` →
`$SEREN_AGENT_CONFIG` → `~/seren-agent/seren-agent.yaml` → built-in defaults.

The yaml carries host/port **only** - the token is not here (see the
interlock section above). Fields you might touch:

- `server.host` (default `0.0.0.0` - the cluster-plane bind)
- `server.port` (default 7777)

Env vars override file values for systemd: `AGENT_HOST`/`AGENT_PORT`, or the
namespaced `SEREN_AGENT_HOST`/`SEREN_AGENT_PORT`.

---

## Deployment

The shared `setup-agent-service.{sh,ps1}` wrappers install the agent as a
systemd service (Linux), a launchd agent (macOS), or an NSSM service
(Windows) - all running as your user so paths and caches resolve to your
profile. One node, one agent, runs on boot. The cluster head finds it across
the LAN.

---

## What this is part of

SerenAgent is a piece of [Seren](https://github.com/ChadRoesler) - a fully
self-hosted local AI companion stack. It's the per-node muscle: the cluster
head ([SerenRuntimeHost](https://github.com/ChadRoesler)) is the brain that
aggregates and decides; the agent is what actually touches each machine. You
run one on every node in your cluster.

On its own it's a tidy, auth-gated, manifest-driven service manager for a
single box. As part of the constellation, it's how the whole thing becomes
one cluster instead of a pile of separate machines.

Rip it and win.