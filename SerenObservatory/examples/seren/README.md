# `~/.seren/` — what Observatory reads

Copy this tree to `~/.seren/` and Observatory will report those services.
That's the whole mechanism.

```
~/.seren/
  node.json              what this box is
  services/
    <name>.json          one per installed service
```

An install under a Starwright root keeps its `services/` roster at
`<root>/manifests/` instead (`server.manifests_dir` in the yaml, or
`$SEREN_OBSERVATORY_MANIFESTS`). `services/` here is still read as the box
roster - the node's GPU daemons live in it, shared by every install's
Observatory on the box - and `node.json` stays here either way, because it
describes the box.

**If a service isn't listed here, Observatory does not know it exists.** It
reports these files and nothing else — no directory probing, no port
scanning, no guessing. A node can be running six healthy services and look
completely empty, and that is the first thing to check when it does.

Files are re-read on **every request**, so adding, editing or deleting one
shows up immediately. No restart.

These samples are loaded by the test suite, so they can't quietly stop being
true. JSON has no comment syntax, which is why the annotation lives here
rather than inline.

---

## Who writes these

| writer | what it covers |
|---|---|
| `setup-seren-service.sh` | every `seren-*` constellation service (`systemd` type) |
| `setup-seren-service.ps1` | the same services on a Windows box (`windows_service` type) |
| `write_service_manifest` in node prep's `common.sh` | the inference-side services — llama, kokoro, comfy, whisper, coral |
| you, by hand | anything else, including `docker_compose` services |

Hand-writing one is a perfectly normal thing to do. That's why this
directory exists.

---

## The five service types

`service_type` decides how Observatory starts, stops and inspects a service.
**Omit it and you get `pid_file`** — which keeps every manifest written
before this field existed working untouched.

### `systemd` — a systemd unit

The constellation services. Lifecycle is `systemctl start|stop|restart`
against `systemd_unit`, run through `sudo -n`, so the sudoers rule has to be
in place or you get a fast clear failure instead of a password prompt.

See **`services/seren-memory.json`**. Required: `systemd_unit`, and `port`
(use `0` if it doesn't listen).

### `pid_file` — a start script and a PID file

The default, and what node prep writes for llama, kokoro, comfy and whisper.
Observatory runs `start_script` / `stop_script` and reads liveness from
`pid_path`.

See **`services/llama.json`**. Note `serviceSpecific` — Observatory ignores
its contents entirely; it's for tools that know about that particular
service. Model names and context sizes go there, not at the top level.

### `library` — no daemon at all

Code imported into a venv on demand. **`port` is always 0** and there are no
start or stop scripts; there is nothing to start. Coral's TPU bindings are
the case this exists for.

See **`services/coral.json`**.

### `docker_compose` — a container in a stack

Lifecycle is `docker compose -f <compose_file> up -d|down|restart
<compose_service>`.

See **`services/searxng.json`**. Required: `compose_file` and
`compose_service` — `compose_service` defaults to `service` if omitted, but
say it explicitly; the stack name and the service name inside it are not
always the same, and when they diverge the failure is a confusing no-op.

### `windows_service` — a Windows service

The constellation services on a Windows box, installed with NSSM by the
Starwright PowerShell cards. Lifecycle is `sc.exe start|stop` against
`windows_service`, waiting for RUNNING or STOPPED; restart is a stop that
finished, then a start. The Observatory runs unprivileged: the card's service
core grants its account start, stop and query on that one service (`sc
sdset`), the Windows version of the node's `seren-systemctl` rule. An access
denied means the grant is missing; reinstalling the service with its card
writes it again. Memory, CPU and uptime come back `null` (there is no `/proc`).

Required: `windows_service`, and `port` (use `0` if it doesn't listen).

---

## Fields Observatory actually reads

Everything else is ignored, so extra keys are harmless — put deployment
notes in there if you like.

| field | meaning |
|---|---|
| `service` | the name. Falls back to the filename stem if absent. |
| `service_type` | one of the five above. Defaults to `pid_file`. |
| `port` | what it listens on. `0` means "doesn't listen". |
| `health_url` | probed for the health endpoint. |
| `schema_version` | a manifest **newer** than Observatory understands is skipped, not guessed at. Current: 2. |
| `systemd_unit` | `systemd` only. |
| `windows_service` | `windows_service` only. |
| `start_script`, `stop_script`, `pid_path` | `pid_file` only. |
| `log_path` | the file `/logs` tails (`pid_file`, `windows_service`). |
| `compose_file`, `compose_service` | `docker_compose` only. |
| `implementation` | free text, shown in the dashboard. |
| `serviceSpecific` | opaque to Observatory; for service-aware tooling. |

## node.json

One per box, written during node prep's foundation phase. Feeds the
dashboard's "what is this box?" panel, combined with live load and memory
read from `/proc` at request time.

`platform`, `jetpack_release` and `cuda_arch` are Jetson-shaped because
that's where they came from; on a Spark or a NUC set what's true and leave
the rest `"unknown"` rather than inventing values.

## Checking your work

```bash
curl -s http://127.0.0.1:7777/api/v1/system/services | python3 -m json.tool
```

A service you just added and can't see is almost always a JSON syntax error —
the loader skips unparseable files silently, on purpose, so one bad manifest
can't take down the report for all the others. Run your file through
`python3 -m json.tool` and it'll tell you where.
