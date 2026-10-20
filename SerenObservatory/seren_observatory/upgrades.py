"""
seren_observatory.upgrades - upgrade a service on this node without losing it.

    snap it -> snap its config -> upgrade -> check it -> put it back

One upgrade is a JOB (POST /api/v1/service/{name}/upgrade answers with it at
once; GET /api/v1/upgrades/{id} follows it), because a pip install takes
minutes and the thing being upgraded may be the Observatory itself. Steps:

  1. plan       the install it came from (below), the versions now, the spec
  2. snapshot   the service's own stores (POST /stores/snapshot), when it keeps
                any. Lodestar pulls a copy off the box first and then asks
                with snapshot: false.
  3. config     the config file and every *.env in the app folder, copied
                into the job's folder with their sha256
  4. install    `pip install --upgrade <package>[extras]==<version>` with the
                venv's own python, plus seren-sinew and seren-meninges, named
                so they move to the newest the new pins allow. Naming only
                the service left them where they were: pip's default upgrade
                strategy does not touch a dependency that is already
                satisfied (5.0.1 on the NUC ran on sinew 1.10 and meninges
                2.6 until the libraries were named, 12 Oct 2026).
  5. preflight  the venv imports the package. A wheel that will not import is
                rolled back here, before anything is restarted. Then the apps
                it talks to (PEERS, below) are checked against what the
                cluster runs; too old is rolled back here too.
  6. restart    then health, polled until the service answers or time runs out
  7. put back   the config is compared with the copy; anything that changed it
                is kept beside the copy as <name>.from-upgrade and the copy goes
                back (one more restart so the service reads it)
  8. ledger     the install ledger's version is brought up to date

A failure at 4 or 5 rolls the packages back to the versions in 1; a failure at
6 or 7 rolls them back, puts the config back and restarts again. The job says
which, and keeps pip's last lines.

PEERS. The Seren apps are installed apart and upgraded apart: each has its own
venv, its own pins and its own version number. Where they meet is over HTTP
(the Callosum reads Memory and Loci, the Hippocampus writes to Memory), so
what has to hold between them is the HTTP contract, not the packages. The
rule on the store's side: within a major version it only adds. The reader's
side is written down in its own pyproject, as an extra nobody installs:

    [project.optional-dependencies]
    peers = ["seren-memory>=5.2", "seren-loci>=5.0"]

which reads "where there is a Memory, it is 5.2 or newer". An app that is
not in the cluster at all is not required (pick and choose); one that is
there and too old holds the upgrade. Before anything is touched, pip's dry run
says what the new version needs: too old and the job ends "held", nothing
changed. After the install the installed metadata is checked again, the
backstop. What the cluster runs comes with the request (Lodestar sends every
node's versions as `peers`); asked by hand, it is this node's own. Of several
installs of a peer, the one with the reader's instance (seren-memory-wren for
seren-corpus-callosum-wren) is the one that counts, when there is one.

WHERE THE INSTALL IS. A manifest says how to run a service, not how it was
installed. The Starwright install ledger does: ~/.seren/installed/
<service>[@<instance>].json ($SEREN_INSTALLED_DIR moves it) names the
package, the venv and the extras. The record whose config (or app folder)
matches the manifest's is the one. A manifest that carries `package` and
`venv` itself wins over the ledger. No record: not something this Observatory
can upgrade (llama, kokoro and the rest are built by the node installer, not
pip), and the answer says so.

ITSELF. The Observatory upgrading itself installs, preflights, writes the job
as "restarting" and hands the restart to whatever started it: the restart is
someone else's job, so it finishes after this process is gone. The next
Observatory to start reads the job, checks the version that came up, and
closes it. Under systemd that is `systemctl restart` of its own unit. Under
Windows (an NSSM service) it is the exit policy: NSSM starts its program
again when it exits (AppExit Default = Restart, which is also NSSM's own
default), so the Observatory proves it is that program (its parent is the
service's process) and that the policy is Restart, then exits. Either proof
failing rolls the packages back and says why, the old code still running.
Until 16 Oct 2026 Windows refused outright ("the Starwright card upgrades
it"), on the belief that nothing would be left to start it again. NSSM is.
Anything else (a pid file, a launcher) still refuses: nothing would restart it.

WINDOWS. pip cannot replace a .pyd a running process has loaded, so on
Windows a service is stopped before the install and started after it. Not
itself: its own upgrade moves pure-Python packages under the running process
(.py files are not locked), and a compiled dependency that is would fail in
pip and be rolled back before any restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import lifecycle, manifests

log = logging.getLogger("seren_observatory.upgrades")

# Moved alongside every service unless the request names its own list: the
# shared libraries every Seren service pins.
DEFAULT_WITH = ("seren-sinew", "seren-meninges")
PIP_TIMEOUT_S = 900.0
PREFLIGHT_TIMEOUT_S = 120.0
HEALTH_WAIT_S = 90.0
HEALTH_POLL_S = 2.0
KEEP_JOBS = 50
TERMINAL = ("done", "held", "failed", "rolled_back", "rollback_failed")
INSTALLED_ENV = "SEREN_INSTALLED_DIR"
PEERS_EXTRA = "peers"


# ── where things are ─────────────────────────────────────────────────────────

def upgrades_dir() -> Path:
    """Jobs and config copies: `upgrades` beside the roster, so two installs on
    one box keep their own (~/.seren/upgrades on the default roster)."""
    return manifests.resolve_manifests_dir().parent / "upgrades"


def ledger_dirs() -> list[Path]:
    dirs = []
    env = os.getenv(INSTALLED_ENV)
    if env:
        dirs.append(Path(os.path.expanduser(env)))
    dirs.append(Path(os.path.expanduser("~/.seren/installed")))
    return dirs


def _same(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    return (os.path.normcase(os.path.realpath(os.path.expanduser(str(a))))
            == os.path.normcase(os.path.realpath(os.path.expanduser(str(b)))))


def venv_python(venv: str) -> Optional[str]:
    for rel in (("bin", "python"), ("Scripts", "python.exe")):
        p = Path(venv, *rel)
        if p.is_file():
            return str(p)
    return None


@dataclass
class Install:
    """What an upgrade needs to know about where a service came from."""
    package: str
    venv: str
    python: str
    extras: list[str] = field(default_factory=list)
    config: str = ""
    app_dir: str = ""
    ledger: str = ""          # the record this came from, "" when the manifest said it

    @property
    def module(self) -> str:
        return self.package.replace("-", "_")


def find_install(manifest: dict[str, Any]) -> Optional[Install]:
    """The manifest's own package/venv, else the ledger record whose config or
    app folder is the manifest's. None when neither says."""
    cfg, app = manifest.get("config_path") or "", manifest.get("app_dir") or ""
    if manifest.get("package") and manifest.get("venv"):
        py = venv_python(str(manifest["venv"]))
        if py:
            return Install(str(manifest["package"]), str(manifest["venv"]), py,
                           [str(e) for e in manifest.get("extras") or []], cfg, app)
    for d in ledger_dirs():
        if not d.is_dir():
            continue
        for rec_path in sorted(d.glob("*.json")):
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not (_same(rec.get("config"), cfg) or (app and _same(rec.get("app_dir"), app))):
                continue
            venv, pkg = str(rec.get("venv") or ""), str(rec.get("package") or "")
            py = venv_python(venv) if venv else None
            if not (pkg and py):
                continue
            ex = rec.get("extras") or {}
            extras = sorted(k for k, on in ex.items() if on) if isinstance(ex, dict) else [str(e) for e in ex]
            return Install(pkg, venv, py, extras, cfg or str(rec.get("config") or ""),
                           app or str(rec.get("app_dir") or ""), str(rec_path))
    return None


def is_self(inst: Install) -> bool:
    """True when the install is the venv this Observatory runs from."""
    return _same(inst.venv, sys.prefix)


# ── handing its own restart to NSSM (Windows) ────────────────────────────────

SELF_RESTART_TYPES = ("systemd", "windows_service")
NSSM_DEFAULT_EXIT = "Restart"


def nssm_exit_policy(service: str) -> dict[str, Any]:
    """What NSSM does when the service's program exits: the default value of
    HKLM\\SYSTEM\\CurrentControlSet\\Services\\<name>\\Parameters\\AppExit
    (`nssm set <name> AppExit Default <action>`). No key: NSSM's own default,
    Restart. Read-only, no admin needed. {"policy", "source"}, or {"error"}
    off Windows."""
    try:
        import winreg  # Windows only
    except ImportError:
        return {"error": "no registry here: not Windows"}
    path = "SYSTEM\\CurrentControlSet\\Services\\" + service + "\\Parameters\\AppExit"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, "")
        return {"policy": str(value), "source": "registry"}
    except FileNotFoundError:
        return {"policy": NSSM_DEFAULT_EXIT, "source": "nssm default (no AppExit key)"}
    except OSError as e:
        return {"error": f"could not read the service's AppExit: {e}"}


def _windows_process(pid: int) -> Optional[dict[str, Any]]:
    """{"parent": its parent's pid, "image": the full path of its program} for
    a running Windows process, read from kernel32 through ctypes (no psutil:
    the base install stays pyyaml-only). None off Windows or when the process
    is gone. A seam, so a test can draw the process tree."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):      # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return None
    parent = None
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ProcessID == pid:
                parent = int(e.th32ParentProcessID)
                break
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    if parent is None:
        return None
    image = ""
    handle = k32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                image = buf.value
        finally:
            k32.CloseHandle(handle)
    return {"parent": parent, "image": image}


def _minded_through_launcher(service_pid: int) -> bool:
    """A Windows venv's python.exe is a launcher, not Python: it starts the
    base interpreter as its child, waits, and exits with the child's code.
    Under NSSM the tree is nssm.exe -> <venv>\\Scripts\\python.exe ->
    <home>\\python.exe, so this process's parent is the launcher and the
    service is one further up. Exiting still ends the launcher, and NSSM
    starts it again. Found 19 Oct 2026: the desktop's Observatory refused
    every self-upgrade this way. True only when the parent's program is this
    venv's own python.exe (sys.executable) and the launcher's parent is the
    service."""
    parent = _windows_process(os.getppid())
    return bool(parent and parent["parent"] == service_pid and _same(parent["image"], sys.executable))


def nssm_handoff_check(manifest: dict[str, Any]) -> dict[str, Any]:
    """Can this process hand its restart to NSSM by exiting? Yes when the
    manifest names a Windows service, that service is RUNNING, its process
    is this process's parent, or the parent of this venv's own launcher (so
    this IS the program NSSM is minding), and NSSM's exit policy is Restart.
    {"ok": True, ...} or {"ok": False, "error"}."""
    name = lifecycle._windows_service_name(manifest)
    if not name:
        return {"ok": False, "error": "manifest missing windows_service"}
    state, pid = lifecycle._sc_query(name)
    if state != "RUNNING":
        return {"ok": False, "error": f"{name} is not RUNNING (state {state})"}
    if not pid or (pid != os.getppid() and not _minded_through_launcher(pid)):
        return {"ok": False, "error": f"this process is not {name}'s program (service pid {pid}, "
                                      f"parent pid {os.getppid()}): exiting would restart nothing"}
    policy = nssm_exit_policy(name)
    if policy.get("error"):
        return {"ok": False, "error": policy["error"]}
    if policy["policy"].lower() != NSSM_DEFAULT_EXIT.lower():
        return {"ok": False, "error": f"{name}'s exit policy is {policy['policy']}, not Restart "
                                      f"({policy['source']}): NSSM would not start it again"}
    return {"ok": True, "service": name, "service_pid": pid, "exit_policy": policy}


def _exit_process() -> None:
    """The last thing a Windows self-upgrade or self-restart does. A seam, so
    a test can watch it happen without it happening."""
    log.info("leaving so NSSM starts the new version")
    os._exit(0)


async def restart_self_via_nssm(manifest: dict[str, Any]) -> Optional[dict[str, Any]]:
    """POST /service/<itself>/restart on Windows. None when the manifest is
    not this process's own service (the caller restarts it the ordinary way).

    The ordinary way, for itself, was `sc stop` then `sc start` run INSIDE the
    request: the stop killed this process mid-answer, the client saw WinError
    10054 and no start ever ran (Chad, 20 Oct 2026: "observatory cannot
    restart itself, though nssm it seems to throw a stop then start"). The
    self-upgrade already knew the answer: answer first, then leave, and let
    NSSM start it again. Same hand-off here, same checks (this IS the
    program NSSM is minding, and its exit policy is Restart). When those
    checks fail for a manifest that names an Observatory, the answer says
    why and nothing is killed; a non-Observatory Windows service is None,
    the sc path."""
    if manifests.service_type(manifest) != "windows_service":
        return None
    from .system_routes import _is_an_observatory
    check = await asyncio.to_thread(nssm_handoff_check, manifest)
    if check["ok"]:
        log.info("restart of %s is my own: handing it to NSSM", manifest.get("service"))
        asyncio.get_running_loop().call_later(EXIT_GRACE_S, _exit_process)
        return {"ok": True, "handed_to": "nssm", "service": check["service"], "exit_policy": check["exit_policy"],
                "note": f"answering first, leaving in {EXIT_GRACE_S:g}s; NSSM starts the Observatory again"}
    if _is_an_observatory(manifest):
        return {"ok": False, "handed_to": None, "error": "this Observatory cannot hand its restart to NSSM: " + check["error"]}
    return None


EXIT_GRACE_S = 0.5


# ── running things (one seam, so tests replace it) ───────────────────────────

def _run(argv: list[str], timeout: float) -> dict[str, Any]:
    # UTF-8 both ways. On Windows a piped child writes cp1252, and pip's
    # --report prints a "→" it cannot encode: every desktop plan died in pip
    # with UnicodeEncodeError (found 14 Oct 2026, the first upgrade run).
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "out": f"timed out after {timeout:.0f}s"}
    except OSError as e:
        return {"ok": False, "exit_code": None, "out": str(e)}
    return {"ok": r.returncode == 0, "exit_code": r.returncode,
            "out": ((r.stdout or "") + (r.stderr or "")).strip()}


def _tail(text: str, n: int = 25) -> list[str]:
    return [l for l in (text or "").splitlines() if l.strip()][-n:]


_VERSIONS = ("import importlib.metadata as m, json, sys\n"
             "out = {}\n"
             "for d in sys.argv[1:]:\n"
             "    try: out[d] = m.version(d)\n"
             "    except Exception: out[d] = None\n"
             "print(json.dumps(out))")


def installed_versions(python: str, dists: list[str]) -> dict[str, Optional[str]]:
    r = _run([python, "-c", _VERSIONS, *dists], timeout=60)
    try:
        return json.loads((r.get("out") or "").splitlines()[-1]) if r["ok"] else {d: None for d in dists}
    except (ValueError, IndexError):
        return {d: None for d in dists}


_REQUIRES = ("import importlib.metadata as m, json, sys\n"
             "try: print(json.dumps(m.requires(sys.argv[1]) or []))\n"
             "except Exception: print('[]')")


def installed_requires(python: str, dist: str) -> list[str]:
    """The installed distribution's Requires-Dist, extras and all."""
    r = _run([python, "-c", _REQUIRES, dist], timeout=60)
    try:
        out = json.loads((r.get("out") or "").splitlines()[-1]) if r["ok"] else []
    except (ValueError, IndexError):
        return []
    return [str(x) for x in out] if isinstance(out, list) else []


# ── peers: the apps a service talks to ───────────────────────────────────────

def peer_needs(requires: list[str]) -> list[Any]:
    """The requirements under the `peers` extra, as packaging Requirements."""
    from packaging.requirements import InvalidRequirement, Requirement
    out = []
    for s in requires or []:
        try:
            r = Requirement(str(s))
        except InvalidRequirement:
            continue
        try:
            if r.marker is None or not r.marker.evaluate({"extra": PEERS_EXTRA}) or r.marker.evaluate({"extra": ""}):
                continue
        except Exception:  # noqa: BLE001 - a marker this packaging cannot read is not ours
            continue
        out.append(r)
    return out


def instance_of(service: str) -> str:
    """'seren-corpus-callosum-wren' -> 'wren'."""
    return service.rsplit("-", 1)[-1] if "-" in service else ""


def check_peers(service: str, package: str, version: Optional[str], requires: list[str],
                peers: list[dict[str, Any]]) -> dict[str, Any]:
    """Does the cluster (peers: [{service, node, package, version}]) run what
    this version of the service needs? {ok, needs, problems, unchecked}."""
    from packaging.version import InvalidVersion, Version
    needs = peer_needs(requires)
    inst = instance_of(service)
    problems, unchecked = [], []
    for req in needs:
        want = f"{req.name}{req.specifier}"
        name = _canon(req.name)
        seen = [p for p in peers or [] if isinstance(p, dict) and p.get("version")
                and _canon(str(p.get("package") or "")) == name]
        chosen = [p for p in seen if instance_of(str(p.get("service") or "")) == inst] or seen
        if not chosen:
            unchecked.append(f"{want}: none in the cluster")
            continue
        for p in chosen:
            try:
                ok = req.specifier.contains(Version(str(p["version"])), prereleases=True)
            except InvalidVersion:
                ok = False
            if not ok:
                where = str(p.get("service") or req.name) + (f" on {p['node']}" if p.get("node") else "")
                problems.append(f"{package}{' ' + version if version else ''} needs {want}; "
                                f"{where} is {p['version']}")
    return {"ok": not problems, "needs": [f"{r.name}{r.specifier}" for r in needs],
            "problems": problems, "unchecked": unchecked}


def node_versions() -> dict[str, dict[str, Any]]:
    """Every service on this node an upgrade can see: {name: {package,
    version, needs, self}}. What Lodestar collects as the cluster's peers."""
    out: dict[str, dict[str, Any]] = {}
    for name, m in sorted(manifests.load_services().items()):
        inst = find_install(m)
        if inst is None:
            continue
        v = installed_versions(inst.python, [inst.package]).get(inst.package)
        needs = [f"{r.name}{r.specifier}" for r in peer_needs(installed_requires(inst.python, inst.package))]
        out[name] = {"package": inst.package, "version": v, "needs": needs, "self": is_self(inst)}
    return out


def local_peers() -> list[dict[str, Any]]:
    return [{"service": n, "node": None, "package": d["package"], "version": d["version"]}
            for n, d in node_versions().items()]


def _spec(inst: Install, version: Optional[str]) -> str:
    name = inst.package + (f"[{','.join(inst.extras)}]" if inst.extras else "")
    return f"{name}=={version}" if version else name


def install_argv(inst: Install, version: Optional[str], with_: list[str], dry_run: bool = False) -> list[str]:
    argv = [inst.python, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check"]
    if dry_run:
        argv += ["--dry-run", "--quiet", "--report", "-"]
    return argv + [_spec(inst, version), *with_]


def rollback_argv(inst: Install, before: dict[str, Optional[str]]) -> list[str]:
    pins = [(_spec(inst, v) if d == inst.package else f"{d}=={v}") for d, v in before.items() if v]
    return [inst.python, "-m", "pip", "install", "--disable-pip-version-check", *pins]


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def config_files(inst: Install) -> list[Path]:
    """The config file, and every *.env in the app folder: what an operator
    edits by hand and an upgrade must not lose."""
    out: list[Path] = []
    if inst.config and Path(inst.config).is_file():
        out.append(Path(inst.config))
    if inst.app_dir and Path(inst.app_dir).is_dir():
        out += sorted(p for p in Path(inst.app_dir).glob("*.env") if p.is_file() and p not in out)
    return out


# ── the job ──────────────────────────────────────────────────────────────────

@dataclass
class UpgradeJob:
    id: str
    service: str
    package: str
    version: Optional[str]           # None = the newest the index has
    with_: list[str]
    reason: str
    self_upgrade: bool = False
    state: str = "queued"            # queued, running, restarting, done, held, failed, rolled_back, rollback_failed
    step: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    before: dict[str, Optional[str]] = field(default_factory=dict)
    after: dict[str, Optional[str]] = field(default_factory=dict)
    snapshot: Optional[dict[str, Any]] = None
    config: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    pip: list[str] = field(default_factory=list)
    peers: Optional[list[dict[str, Any]]] = None    # what the cluster runs; None = this node's own
    needs: Optional[dict[str, Any]] = None          # the peer check, last made
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["with"] = d.pop("with_")
        d["terminal"] = self.state in TERMINAL
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UpgradeJob":
        d = dict(d)
        d["with_"] = d.pop("with", []) or []
        d.pop("terminal", None)
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def note(self, step: str, ok: bool, **more: Any) -> None:
        self.step = step
        self.steps.append({"step": step, "ok": ok, "at": time.time(), **more})


def job_dir(job_id: str) -> Path:
    return upgrades_dir() / job_id


def save(job: UpgradeJob) -> None:
    d = upgrades_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{job.id}.json"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(job.as_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, p)


def load(job_id: str) -> Optional[UpgradeJob]:
    if not job_id or any(c in job_id for c in "/\\.") :
        return None
    p = upgrades_dir() / f"{job_id}.json"
    try:
        return UpgradeJob.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def recent(limit: int = 20) -> list[UpgradeJob]:
    d = upgrades_dir()
    if not d.is_dir():
        return []
    jobs = []
    for p in d.glob("*.json"):
        j = load(p.stem)
        if j:
            jobs.append(j)
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    return jobs[:limit]


def _prune() -> None:
    jobs = recent(limit=10_000)
    for j in jobs[KEEP_JOBS:]:
        try:
            (upgrades_dir() / f"{j.id}.json").unlink(missing_ok=True)
            shutil.rmtree(job_dir(j.id), ignore_errors=True)
        except OSError:
            pass


class Upgrader:
    """One upgrade at a time per Observatory."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.current: Optional[UpgradeJob] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def busy(self) -> bool:
        # The task, not the lock: the task exists the moment start() returns,
        # the lock only once it has run, and a second request in between must
        # still be told no.
        return self._lock.locked() or (self._task is not None and not self._task.done())

    # ── plan ──────────────────────────────────────────────────────────────
    @staticmethod
    async def _dry_run(inst: Install, version: Optional[str], with_: list[str]) -> dict[str, Any]:
        """pip's dry run: {ok, would_install, version, requires, out}. version
        and requires are what the service would be after: the new release's,
        or the installed one's when pip would not move it."""
        r = await asyncio.to_thread(_run, install_argv(inst, version, with_, dry_run=True), PIP_TIMEOUT_S)
        would: list[dict[str, Any]] = []
        new_version, requires = None, None
        if r["ok"]:
            out = r.get("out") or ""
            try:
                # The report is stdout, and _run puts stderr after it. A pip
                # warning there ("does not provide the extra 'mcp'") failed the
                # whole parse, so the plan said nothing would install and judged
                # the peers on the old version (found 19 Oct 2026, the nuc's
                # Workbench): read the report and stop where it ends.
                rep, _ = json.JSONDecoder().raw_decode(out, out.index("{"))
                for x in rep.get("install") or []:
                    md = x.get("metadata") or {}
                    would.append({"name": md["name"], "version": md["version"]})
                    if _canon(md["name"]) == _canon(inst.package):
                        new_version, requires = md["version"], list(md.get("requires_dist") or [])
            except (ValueError, KeyError, TypeError):
                pass
        if requires is None:
            requires = await asyncio.to_thread(installed_requires, inst.python, inst.package)
        return {"ok": r["ok"], "would_install": would, "version": new_version, "requires": requires,
                "out": r.get("out", "")}

    async def plan(self, name: str, manifest: dict[str, Any], version: Optional[str],
                   with_: list[str], peers: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """What an upgrade would do, without doing it: pip's own dry run, and
        whether the apps it talks to are new enough for what it would become."""
        inst = find_install(manifest)
        if inst is None:
            return {"ok": False, "service": name, "upgradable": False,
                    "error": "no install record for this service: not a pip install this Observatory can see"}
        dists = [inst.package, *with_]
        before = await asyncio.to_thread(installed_versions, inst.python, dists)
        dry = await self._dry_run(inst, version, with_)
        if peers is None:
            peers = await asyncio.to_thread(local_peers)
        needs = check_peers(name, inst.package, dry["version"] or before.get(inst.package), dry["requires"], peers)
        return {"ok": dry["ok"], "service": name, "upgradable": True, "dry_run": True,
                "package": inst.package, "extras": inst.extras, "venv": inst.venv, "ledger": inst.ledger,
                "self": is_self(inst), "before": before, "spec": _spec(inst, version), "with": with_,
                "would_install": dry["would_install"], "config": [str(p) for p in config_files(inst)],
                "needs": needs, "held": not needs["ok"],
                **({} if dry["ok"] else {"error": "pip could not resolve it", "pip": _tail(dry["out"])})}

    # ── start ─────────────────────────────────────────────────────────────
    def start(self, name: str, manifest: dict[str, Any], version: Optional[str], with_: list[str],
              snapshot: bool, reason: str, wait_seconds: float = HEALTH_WAIT_S,
              peers: Optional[list[dict[str, Any]]] = None) -> tuple[int, dict[str, Any]]:
        """(status, body). 202 with the job; 409 busy or not upgradable."""
        if self.busy:
            return 409, {"ok": False, "error": "an upgrade is already running on this node",
                         "job": self.current.as_dict() if self.current else None}
        inst = find_install(manifest)
        if inst is None:
            return 409, {"ok": False, "service": name, "upgradable": False,
                         "error": "no install record for this service: not a pip install this Observatory can see"}
        self_up = is_self(inst)
        if self_up and manifests.service_type(manifest) not in SELF_RESTART_TYPES:
            return 409, {"ok": False, "service": name, "error":
                         "the Observatory can upgrade itself only as a systemd unit or a Windows "
                         "service (something must start it again); upgrade it with its Starwright card"}
        job = UpgradeJob(id=time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6], service=name,
                         package=inst.package, version=version, with_=list(with_), reason=reason[:200],
                         self_upgrade=self_up, peers=peers)
        save(job)
        self.current = job
        self._task = asyncio.ensure_future(self._run_locked(job, inst, manifest, snapshot, wait_seconds))
        return 202, {"ok": True, "job": job.as_dict()}

    async def _run_locked(self, job, inst, manifest, snapshot, wait_seconds) -> None:
        async with self._lock:
            try:
                await self._run(job, inst, manifest, snapshot, wait_seconds)
            except Exception as e:  # noqa: BLE001 - a job always ends with a state, never a dead task
                log.exception("upgrade %s crashed", job.id)
                job.error = f"{type(e).__name__}: {e}"
                if job.state not in TERMINAL:
                    job.state = "failed"
            finally:
                if job.state in TERMINAL and job.finished_at is None:
                    job.finished_at = time.time()
                save(job)
                if job.state in TERMINAL:
                    _prune()

    async def _run(self, job: UpgradeJob, inst: Install, manifest: dict[str, Any],
                   snapshot: bool, wait_seconds: float) -> None:
        job.state = "running"
        dists = [inst.package, *job.with_]
        job.before = await asyncio.to_thread(installed_versions, inst.python, dists)
        job.note("plan", True, spec=_spec(inst, job.version), python=inst.python, ledger=inst.ledger)
        save(job)

        # 1b. the apps it talks to, for the version pip would bring: too old
        # and nothing is touched. A dry run pip cannot make is left to the
        # install to report.
        peers = job.peers if job.peers is not None else await asyncio.to_thread(local_peers)
        dry = await self._dry_run(inst, job.version, job.with_)
        if dry["ok"]:
            job.needs = check_peers(job.service, inst.package, dry["version"] or job.before.get(inst.package),
                                    dry["requires"], peers)
            job.note("peers", job.needs["ok"], **{k: job.needs[k] for k in ("needs", "problems", "unchecked")})
            if not job.needs["ok"]:
                job.state, job.error = "held", "; ".join(job.needs["problems"])
                return
        save(job)

        # 2. the stores
        if snapshot:
            snap = await take_snapshot(job.service, f"before upgrade {job.id}")
            job.snapshot = snap
            if snap.get("ok") is False:
                job.note("snapshot", False, error=snap.get("error"))
                job.state, job.error = "failed", f"no snapshot, so no upgrade: {snap.get('error')}"
                return
            job.note("snapshot", True, **({"id": snap.get("id")} if snap.get("id") else {"skipped": snap.get("skipped")}))
        save(job)

        # 3. the config
        held = job_dir(job.id) / "config"
        held.mkdir(parents=True, exist_ok=True)
        for p in config_files(inst):
            dst = held / p.name
            shutil.copy2(p, dst)
            job.config.append({"path": str(p), "copy": str(dst), "sha256": _sha(p)})
        job.note("config", True, files=len(job.config))
        save(job)

        windows = os.name == "nt" and not job.self_upgrade
        if windows:
            await lifecycle.stop(manifest)

        # 4. install
        r = await asyncio.to_thread(_run, install_argv(inst, job.version, job.with_), PIP_TIMEOUT_S)
        job.pip = _tail(r.get("out", ""))
        if not r["ok"]:
            job.note("install", False, exit_code=r.get("exit_code"))
            await self._roll_back(job, inst, manifest, restart=windows, reason="pip failed")
            return
        job.after = await asyncio.to_thread(installed_versions, inst.python, dists)
        job.note("install", True, after=job.after)
        save(job)

        # 5. preflight
        pre = await asyncio.to_thread(_run, [inst.python, "-c", f"import {inst.module}"], PREFLIGHT_TIMEOUT_S)
        if not pre["ok"]:
            job.note("preflight", False, out=_tail(pre.get("out", ""), 10))
            await self._roll_back(job, inst, manifest, restart=windows, reason="the new version does not import")
            return
        requires = await asyncio.to_thread(installed_requires, inst.python, inst.package)
        job.needs = check_peers(job.service, inst.package, job.after.get(inst.package), requires, peers)
        if not job.needs["ok"]:
            job.note("preflight", False, problems=job.needs["problems"])
            await self._roll_back(job, inst, manifest, restart=windows,
                                  reason="the apps it talks to are too old: " + "; ".join(job.needs["problems"]))
            return
        job.note("preflight", True)

        # 6. restart. Itself: hand the restart to what started it and stop here.
        if job.self_upgrade and manifests.service_type(manifest) == "windows_service":
            check = await asyncio.to_thread(nssm_handoff_check, manifest)
            if not check["ok"]:
                job.note("restart", False, error=check["error"])
                await self._roll_back(job, inst, manifest, restart=False,
                                      reason="NSSM would not restart the Observatory: " + check["error"])
                return
            job.state = "restarting"
            job.note("restart", True, handed_to="nssm", **{k: check[k] for k in ("service", "exit_policy")})
            save(job)
            # The job is on disk; give the response and the log their moment,
            # then leave. NSSM starts the new version, which closes the job.
            asyncio.get_running_loop().call_later(EXIT_GRACE_S, _exit_process)
            return
        if job.self_upgrade:
            job.state = "restarting"
            job.note("restart", True, handed_to="systemd")
            save(job)
            res = await lifecycle.restart(manifest)   # ends this process; the next one closes the job
            # Still here: systemd would not take the restart (no sudoers grant,
            # no unit). The old code is still running, so put the old packages
            # back rather than leave a new install nothing has started.
            if not res.get("ok", False):
                job.note("restart", False, error=res.get("error") or res.get("stderr") or "restart refused")
                await self._roll_back(job, inst, manifest, restart=False,
                                      reason="systemd would not restart the Observatory")
            return
        res = await (lifecycle.start(manifest) if windows else lifecycle.restart(manifest))
        healthy = res.get("ok", False) and await wait_healthy(manifest, wait_seconds)
        job.note("restart", healthy, **({} if healthy else {"error": res.get("error") or "no health within "
                                                            f"{wait_seconds:.0f}s"}))
        if not healthy:
            await self._roll_back(job, inst, manifest, restart=True, reason="it did not come back healthy")
            return

        # 7. put the config back if anything changed it
        changed = put_back(job, keep_changed=True)
        if changed:
            job.note("put_back", True, files=changed)
            res = await lifecycle.restart(manifest)
            if not (res.get("ok", False) and await wait_healthy(manifest, wait_seconds)):
                job.note("restart", False, error="no health after the config was put back")
                await self._roll_back(job, inst, manifest, restart=True,
                                      reason="it did not come back healthy on its own config")
                return
        else:
            job.note("put_back", True, files=[])

        # 8. the ledger
        update_ledger(inst, job.after.get(inst.package))
        job.state = "done"
        job.note("done", True)

    async def _roll_back(self, job: UpgradeJob, inst: Install, manifest: dict[str, Any],
                         restart: bool, reason: str) -> None:
        job.error = reason
        ok = True
        now = await asyncio.to_thread(installed_versions, inst.python, list(job.before))
        if now != job.before and any(job.before.values()):
            r = await asyncio.to_thread(_run, rollback_argv(inst, job.before), PIP_TIMEOUT_S)
            ok = r["ok"]
            job.note("rollback_install", ok, **({} if ok else {"pip": _tail(r.get("out", ""))}))
        put_back(job, keep_changed=False)
        if restart:
            res = await (lifecycle.start(manifest) if os.name == "nt" else lifecycle.restart(manifest))
            healthy = res.get("ok", False) and await wait_healthy(manifest, HEALTH_WAIT_S)
            job.note("rollback_restart", healthy)
            ok = ok and healthy
        job.state = "rolled_back" if ok else "rollback_failed"


# ── pieces ───────────────────────────────────────────────────────────────────

async def take_snapshot(name: str, reason: str) -> dict[str, Any]:
    """The service's own snapshot, through its store routes. A service that
    keeps none is a plain skip; one that keeps some and will not snapshot is a
    failure, and the upgrade does not go ahead."""
    from fastapi import HTTPException
    from .stores_routes import _forward, TIMEOUT
    try:
        r = await _forward(name, "POST", "/stores/snapshot", body={"reason": reason}, timeout=TIMEOUT)
    except HTTPException as e:
        return {"ok": False, "error": str(e.detail)}
    if r.status_code == 404:
        return {"ok": True, "skipped": "keeps no snapshots"}
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or not isinstance(body, dict):
        return {"ok": False, "error": (body or {}).get("error") if isinstance(body, dict) else f"HTTP {r.status_code}"}
    snap = body.get("snapshot") or {}
    return {"ok": True, "id": snap.get("id"), "snapshot": snap}


async def wait_healthy(manifest: dict[str, Any], wait_seconds: float) -> bool:
    if not manifests.service_has_port(manifest):
        return True
    deadline = time.monotonic() + max(1.0, wait_seconds)
    while True:
        h = await lifecycle.probe_port(manifest)
        if h.get("ok"):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(HEALTH_POLL_S)


def put_back(job: UpgradeJob, keep_changed: bool) -> list[str]:
    """Copy each held config file back where it differs. With keep_changed,
    what was there is kept as <copy>.from-upgrade first. Returns the paths put
    back."""
    back = []
    for c in job.config:
        p, copy = Path(c["path"]), Path(c["copy"])
        if _sha(p) == c.get("sha256") or not copy.is_file():
            continue
        if keep_changed and p.is_file():
            shutil.copy2(p, copy.with_name(copy.name + ".from-upgrade"))
        shutil.copy2(copy, p)
        back.append(str(p))
    return back


def update_ledger(inst: Install, version: Optional[str]) -> None:
    """The ledger names the version installed; keep it true. Best effort: the
    record may belong to another user."""
    if not (inst.ledger and version):
        return
    p = Path(inst.ledger)
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["version"] = version
        rec["upgraded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except (OSError, ValueError) as e:
        log.info("could not update the ledger %s: %s", p, e)


def finish_self_upgrades() -> list[str]:
    """At startup: close any job that was restarting THIS Observatory. The
    version that came up is the answer."""
    closed = []
    for job in recent(limit=KEEP_JOBS):
        if job.state != "restarting" or not job.self_upgrade:
            continue
        try:
            from importlib.metadata import version as dist_version
            now = dist_version(job.package)
        except Exception:  # noqa: BLE001
            now = None
        job.after = {**job.after, job.package: now}
        want = job.version or (job.after or {}).get(job.package)
        if now and (not job.version or now == want):
            job.state = "done"
            job.note("done", True, came_up=now)
        else:
            job.state, job.error = "failed", f"came back as {now}, not {want}"
            job.note("done", False, came_up=now)
        job.finished_at = time.time()
        save(job)
        closed.append(job.id)
    return closed
