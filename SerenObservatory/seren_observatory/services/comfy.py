"""
ComfyUI service-specific endpoints.

Mounted under /api/v1/service/comfy.

Endpoints:
    GET    /checkpoints           - list .safetensors / .ckpt in checkpoints dir
    GET    /loras                 - list .safetensors in loras dir
    GET    /vae                   - list .safetensors / .pt in vae dir
    DELETE /checkpoints/{name}    - remove a checkpoint
    DELETE /loras/{name}          - remove a lora
    DELETE /vae/{name}            - remove a VAE
    GET    /catalog               - what every model file IS (seren_observatory.comfy_catalog), and the profiles
    POST   /catalog/scan          - look again: {hash: none|loras|all, civitai: bool}
    PUT    /profiles              - what someone LIKES with a model: {model, loras?, negative?, vae?}
    POST   /generate              - run a ComfyUI workflow here and hand back its images

/generate takes the workflow in ComfyUI's API format (what Lodestar builds
from a template) and does the waiting a caller would otherwise do against
ComfyUI's own port: queue it, follow its history, fetch every image. The
node's ComfyUI is reached on loopback; nobody else needs its port.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

from .. import comfy_catalog, manifests

# Each Comfy resource type maps to a serviceSpecific manifest field + a set
# of file extensions to enumerate.
RESOURCE_DIRS = {
    "checkpoints": ("checkpoints_dir", {".safetensors", ".ckpt"}),
    "loras":       ("loras_dir",       {".safetensors", ".pt"}),
    "vae":         ("vae_dir",         {".safetensors", ".pt"}),
}


def _list_dir(manifest: dict, resource: str) -> dict:
    if resource not in RESOURCE_DIRS:
        raise HTTPException(404, f"unknown comfy resource: {resource}")

    dir_key, extensions = RESOURCE_DIRS[resource]
    dir_path = manifest.get("serviceSpecific", {}).get(dir_key)
    if not dir_path:
        raise HTTPException(500, f"comfy manifest missing serviceSpecific.{dir_key}")

    p = Path(dir_path)
    if not p.is_dir():
        return {resource: [], "path": str(p), "note": "directory does not exist yet"}

    files = sorted(
        f.name for f in p.iterdir()
        if f.is_file() and f.suffix.lower() in extensions
    )
    return {resource: files, "path": str(p)}


def _safe_target(manifest: dict, resource: str, name: str) -> Path:
    """Resolve a resource file inside its directory, refusing path traversal."""
    if resource not in RESOURCE_DIRS:
        raise HTTPException(404, f"unknown comfy resource: {resource}")
    if "/" in name or ".." in name or name.startswith("."):
        raise HTTPException(400, "invalid file name")

    dir_key, extensions = RESOURCE_DIRS[resource]
    dir_path = manifest.get("serviceSpecific", {}).get(dir_key, "")
    target = Path(dir_path) / name
    # resolve() and check it's still within the parent - defense in depth
    try:
        target_resolved = target.resolve()
        parent_resolved = Path(dir_path).resolve()
        target_resolved.relative_to(parent_resolved)
    except (ValueError, OSError) as e:
        raise HTTPException(400, "invalid path") from e

    if target.suffix.lower() not in extensions:
        raise HTTPException(400, f"unexpected extension; expected one of {sorted(extensions)}")
    if not target.is_file():
        raise HTTPException(404, f"{resource}/{name} not found")
    return target


# ── the catalog ──────────────────────────────────────────────────────────
# Kept beside the box's manifests (~/.seren/comfy/), with a file of hand
# corrections next to it. A scan reads headers (seconds); hashing and the
# CivitAI lookups run in the background and are remembered per file.

def catalog_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".seren" / "comfy"


def models_root(manifest: dict) -> Path | None:
    """The ComfyUI models folder: serviceSpecific.models_dir, else the
    folder the checkpoints are in."""
    spec = manifest.get("serviceSpecific", {}) or {}
    if spec.get("models_dir"):
        return Path(os.path.expanduser(spec["models_dir"]))
    if spec.get("checkpoints_dir"):
        return Path(os.path.expanduser(spec["checkpoints_dir"])).parent
    return None


def families(manifest: dict) -> list[str]:
    """The model families this node's ComfyUI can draw (serviceSpecific.
    families, a list or "a,b"); [] means no limit. Having a model's files is
    not the same as being able to run it: the Xavier 32GB keeps every model
    but, on JetPack 5's torch 2.1, draws SDXL and SD 1.5 only."""
    raw = (manifest.get("serviceSpecific") or {}).get("families") or []
    items = raw.split(",") if isinstance(raw, str) else raw
    return [str(f).strip() for f in items if str(f).strip()]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _civitai_key(request: Request) -> str:
    """CivitAI's key, when the node has one: secrets.json `civitai_key`.
    Lookups by hash work without it; with it, they see what an account sees."""
    path = getattr(request.app.state, "secrets_path", None)
    data = _read_json(Path(path)) if path else None
    return str((data or {}).get("civitai_key") or "")


class _Scanner:
    """One scan at a time per node; the latest finished catalog on disk."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False}

    def run(self, root: Path, hash_what: str, civitai: bool, key: str, host: str) -> dict[str, Any]:
        d = catalog_dir()
        d.mkdir(parents=True, exist_ok=True)
        out, fixes = d / "catalog.json", d / "overrides.json"
        cat = comfy_catalog.scan(root, _read_json(out), hash_what, civitai, key, host,
                                 _read_json(fixes) if fixes.is_file() else None,
                                 log=lambda m: self.state.update(last=m))
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(cat, indent=1), encoding="utf-8")
        os.replace(tmp, out)
        return cat

    def start(self, root: Path, hash_what: str, civitai: bool, key: str, host: str) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        self.state = {"running": True, "started": int(time.time()), "hash": hash_what, "civitai": civitai}

        def work() -> None:
            try:
                self.run(root, hash_what, civitai, key, host)
                self.state.update(running=False, finished=int(time.time()))
            except Exception as ex:  # noqa: BLE001 - a failed scan is reported, never fatal
                self.state.update(running=False, error=f"{type(ex).__name__}: {ex}")
            finally:
                self.lock.release()
        threading.Thread(target=work, daemon=True, name="comfy-catalog").start()
        return True


SCANNER = _Scanner()


# ── profiles ─────────────────────────────────────────────────────────────
# The catalog says what CAN go with a model; a profile says what someone
# likes with it - the LoRAs and weights it wears when a request names none,
# a negative, a VAE in place of its own. The old SerenTools kept one yaml per
# checkpoint for this (workflows/checkpoint-profiles/); the model's type,
# which those also held by hand, is the catalog's to know now.

MAX_PROFILE_LORAS = 8


def profiles_path() -> Path:
    return catalog_dir() / "profiles.json"


def clean_profile(body: dict[str, Any]) -> dict[str, Any]:
    """A profile as kept: only what was given, checked. Raises ValueError."""
    out: dict[str, Any] = {}
    loras = body.get("loras")
    if loras:
        if not isinstance(loras, list) or len(loras) > MAX_PROFILE_LORAS:
            raise ValueError(f"loras is a list of up to {MAX_PROFILE_LORAS} {{name, weight}}")
        kept = []
        for l in loras:
            name = l.get("name") if isinstance(l, dict) else l
            if not isinstance(name, str) or not name.strip():
                raise ValueError("each lora needs a name")
            try:
                weight = float(l.get("weight", 1.0)) if isinstance(l, dict) else 1.0
            except (TypeError, ValueError):
                raise ValueError(f"the weight for {name} is a number") from None
            if not -2.0 <= weight <= 2.0:
                raise ValueError(f"the weight for {name} is between -2 and 2")
            kept.append({"name": name.strip(), "weight": weight})
        out["loras"] = kept
    for key in ("negative", "vae"):
        v = body.get(key)
        if v:
            if not isinstance(v, str):
                raise ValueError(f"{key} is text")
            out[key] = v.strip()[:2000]
    return out


# ── generating ───────────────────────────────────────────────────────────

GENERATE_DEFAULT_TIMEOUT = 600.0
GENERATE_MAX_TIMEOUT = 3600.0


async def generate(port: int, workflow: dict[str, Any], timeout: float, free_after: bool,
                   client: httpx.AsyncClient | None = None, poll: float = 1.0) -> dict[str, Any]:
    """Queue `workflow` on the ComfyUI at 127.0.0.1:`port`, wait for it, and
    return its images (base64), or why not."""
    own = client is None
    client = client or httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60)
    t0 = time.monotonic()
    try:
        r = await client.post("/prompt", json={"prompt": workflow, "client_id": f"seren-{uuid.uuid4().hex[:8]}"})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not body.get("prompt_id"):
            return {"ok": False, "error": "comfy refused the workflow",
                    "detail": body.get("error") or r.text[:2000], "node_errors": body.get("node_errors") or {}}
        pid = body["prompt_id"]
        entry: dict[str, Any] = {}
        while time.monotonic() - t0 < timeout:
            h = (await client.get(f"/history/{pid}")).json()
            if pid in h:
                entry = h[pid]
                break
            await asyncio.sleep(poll)
        if not entry:
            return {"ok": False, "error": f"comfy did not finish in {timeout:.0f}s", "prompt_id": pid}
        status = entry.get("status", {}) or {}
        if status.get("status_str") != "success":
            msgs = [m[1] for m in status.get("messages", []) if m and m[0] == "execution_error"]
            return {"ok": False, "error": "comfy failed running the workflow", "prompt_id": pid,
                    "detail": (msgs[0].get("exception_message") if msgs else None) or status.get("status_str")}
        images = []
        for node in (entry.get("outputs") or {}).values():
            for im in node.get("images", []) or []:
                v = await client.get("/view", params={"filename": im["filename"], "subfolder": im.get("subfolder", ""),
                                                      "type": im.get("type", "output")})
                if v.status_code == 200:
                    images.append({"filename": im["filename"], "mime": v.headers.get("content-type", "image/png"),
                                   "data": base64.b64encode(v.content).decode("ascii")})
        out = {"ok": True, "prompt_id": pid, "images": images, "seconds": round(time.monotonic() - t0, 1)}
        if free_after:
            # Hand the memory back: on a box with one pool for CPU and GPU,
            # a loaded image model is memory nothing else can have.
            try:
                await client.post("/free", json={"unload_models": True, "free_memory": True})
                out["freed"] = True
            except httpx.HTTPError:
                out["freed"] = False
        return out
    except httpx.HTTPError as ex:
        return {"ok": False, "error": "comfy did not answer", "detail": f"{type(ex).__name__}: {ex}"}
    finally:
        if own:
            await client.aclose()


def register(router: APIRouter) -> None:
    @router.get("/checkpoints")
    async def list_checkpoints():
        m = manifests.load_service("comfy")
        if m is None:
            raise HTTPException(404, "comfy not installed on this node")
        return _list_dir(m, "checkpoints")

    @router.get("/loras")
    async def list_loras():
        m = manifests.load_service("comfy")
        if m is None:
            raise HTTPException(404, "comfy not installed on this node")
        return _list_dir(m, "loras")

    @router.get("/vae")
    async def list_vae():
        m = manifests.load_service("comfy")
        if m is None:
            raise HTTPException(404, "comfy not installed on this node")
        return _list_dir(m, "vae")

    @router.delete("/{resource}/{name}")
    async def delete_resource(resource: str, name: str):
        m = manifests.load_service("comfy")
        if m is None:
            raise HTTPException(404, "comfy not installed on this node")

        target = _safe_target(m, resource, name)
        target.unlink()
        return {"ok": True, "removed": name, "resource": resource}

    @router.get("/catalog")
    async def catalog(request: Request):
        """The catalog, and how the latest scan went. With none on disk yet,
        a quick one (headers only) is made now and the hashing and CivitAI
        lookups start behind it."""
        m = manifests.load_service("comfy")
        if m is None:
            raise HTTPException(404, "comfy not installed on this node")
        root = models_root(m)
        if root is None or not root.is_dir():
            raise HTTPException(500, "comfy manifest names no models folder (serviceSpecific.models_dir)")
        cat = _read_json(catalog_dir() / "catalog.json")
        if cat is None:
            cat = await asyncio.to_thread(SCANNER.run, root, "none", False, "", comfy_catalog.CIVITAI_HOST)
            SCANNER.start(root, "loras", True, _civitai_key(request),
                          (m.get("serviceSpecific") or {}).get("civitai_host") or comfy_catalog.CIVITAI_HOST)
        return {**cat, "scan": SCANNER.state, "summary": comfy_catalog.summary(cat),
                "profiles": _read_json(profiles_path()) or {}, "families": families(m)}

    @router.put("/profiles")
    async def profiles_put(body: dict = Body(...)):
        """{model, loras?: [{name, weight}], negative?, vae?} - an empty one
        (nothing but the model) takes the model's profile away."""
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            raise HTTPException(400, "name the model the profile is for")
        try:
            prof = clean_profile(body)
        except ValueError as ex:
            raise HTTPException(400, str(ex)) from None
        path = profiles_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        all_ = _read_json(path) or {}
        if prof:
            all_[model.strip()] = prof
        else:
            all_.pop(model.strip(), None)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        return {"ok": True, "profiles": all_}

    @router.post("/catalog/scan")
    async def catalog_scan(request: Request, body: dict = Body(default={})):
        """{hash: none|loras|all (default loras), civitai: bool (default true)}"""
        m = manifests.load_service("comfy")
        if m is None:
            raise HTTPException(404, "comfy not installed on this node")
        root = models_root(m)
        if root is None or not root.is_dir():
            raise HTTPException(500, "comfy manifest names no models folder (serviceSpecific.models_dir)")
        what = body.get("hash", "loras")
        if what not in ("none", "loras", "all"):
            raise HTTPException(400, "hash is none, loras or all")
        started = SCANNER.start(root, what, bool(body.get("civitai", True)), _civitai_key(request),
                                (m.get("serviceSpecific") or {}).get("civitai_host") or comfy_catalog.CIVITAI_HOST)
        return {"ok": True, "started": started, "scan": SCANNER.state}

    @router.post("/generate")
    async def generate_route(body: dict = Body(...)):
        """{workflow: <ComfyUI API-format graph>, timeout_seconds?, free_after?}
        -> {ok, images: [{filename, mime, data (base64)}], seconds} | {ok: false, error, detail}"""
        m = manifests.load_service("comfy")
        if m is None:
            raise HTTPException(404, "comfy not installed on this node")
        wf = body.get("workflow")
        if not isinstance(wf, dict) or not wf:
            raise HTTPException(400, "send {workflow: <ComfyUI API-format graph>}")
        try:
            timeout = min(float(body.get("timeout_seconds") or GENERATE_DEFAULT_TIMEOUT), GENERATE_MAX_TIMEOUT)
        except (TypeError, ValueError):
            raise HTTPException(400, "timeout_seconds is a number") from None
        return await generate(int(m.get("port") or 8188), wf, timeout, bool(body.get("free_after", False)))
