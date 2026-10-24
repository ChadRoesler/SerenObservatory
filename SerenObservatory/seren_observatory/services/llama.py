"""
llama.cpp service-specific endpoints.

Mounted under /api/v1/service/llama.

Endpoints:
    GET    /models           - list .gguf files in the configured models dir,
                               and which one it serves
    POST   /models/fetch     - {url, name}: download a .gguf into the models
                               dir, in the background (a Lyceum link, say)
    GET    /models/fetch     - how that download is going
    DELETE /models/{name}    - remove a .gguf it is not serving
    GET    /model            - what it serves, and what it served before
    PUT    /model            - {name}: serve this one from its next start
    POST   /completion       - proxy to llama-server /completion (TODO)

WHY THE NODE FETCHES. A model trained in the Lyceum lands on the Spark; the
small model the hippocampus uses runs here. Three gigabytes should not travel
through Lodestar or the hippocampus to get from one box to the other, so the
asker hands this node a link (short-lived, made by the Lyceum) and the node
pulls the file itself, into the folder it already serves from.

WHAT IT SERVES is LLAMA_MODEL in the env file start_llama.sh reads
(serviceSpecific.config_path, written by seren-prepare-node). PUT /model
rewrites that one line and keeps the line it replaced as LLAMA_MODEL_PREVIOUS,
so going back is one more PUT. It does not restart anything: a running server
keeps its model until it next starts, and whoever asked decides when that is.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Body, HTTPException

from .. import manifests

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.gguf$")
_MODEL_LINE = re.compile(r"^(\s*)LLAMA_MODEL=(.*)$")
_PREV_LINE = re.compile(r"^\s*LLAMA_MODEL_PREVIOUS=")


def _manifest() -> dict[str, Any]:
    m = manifests.load_service("llama")
    if m is None:
        raise HTTPException(404, "llama not installed on this node")
    return m


def _models_dir(m: dict[str, Any]) -> Path:
    d = (m.get("serviceSpecific") or {}).get("models_dir", "")
    if not d:
        raise HTTPException(500, "llama manifest missing serviceSpecific.models_dir")
    return Path(os.path.expanduser(d))


def _env_file(m: dict[str, Any]) -> Optional[Path]:
    p = (m.get("serviceSpecific") or {}).get("config_path", "")
    return Path(os.path.expanduser(p)) if p else None


def _clean(v: str) -> str:
    v = v.strip()
    if not v.startswith(("'", '"')):
        v = v.split("#", 1)[0].strip()
    return v.strip("'\"")


def read_env(path: Optional[Path]) -> dict[str, str]:
    """LLAMA_MODEL and LLAMA_MODEL_PREVIOUS, as the file says them."""
    out = {"current": "", "previous": ""}
    if path is None or not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if (mm := _MODEL_LINE.match(line)):
            out["current"] = _clean(mm.group(2))
        elif _PREV_LINE.match(line):
            out["previous"] = _clean(line.split("=", 1)[1])
    return out


def set_env_model(path: Path, model_path: str) -> dict[str, str]:
    """Point LLAMA_MODEL at model_path, keeping the old value as
    LLAMA_MODEL_PREVIOUS; every other line stays as it was."""
    was = read_env(path)
    prev = was["current"] if was["current"] and was["current"] != model_path else was["previous"]
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    out, put = [], False
    for line in lines:
        if _PREV_LINE.match(line):
            continue
        if (mm := _MODEL_LINE.match(line)) and not put:
            out.append(f"{mm.group(1)}LLAMA_MODEL={model_path}")
            if prev:
                out.append(f"{mm.group(1)}LLAMA_MODEL_PREVIOUS={prev}")
            put = True
            continue
        out.append(line)
    if not put:
        out.append(f"LLAMA_MODEL={model_path}")
        if prev:
            out.append(f"LLAMA_MODEL_PREVIOUS={prev}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return read_env(path)


class Fetch:
    """One download at a time: a node pulling two models at once is a node
    with a full disk, not a faster one."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"state": "idle"}
        self._lock = threading.Lock()
        self.transport: Optional[httpx.BaseTransport] = None       # tests hand one in

    def start(self, url: str, dest: Path) -> bool:
        with self._lock:
            if self.state.get("state") == "fetching":
                return False
            self.state = {"state": "fetching", "name": dest.name, "bytes": 0, "total": None,
                          "started_at": time.time(), "from": httpx.URL(url).host}
        threading.Thread(target=self.run, args=(url, dest), daemon=True, name="llama-fetch").start()
        return True

    def run(self, url: str, dest: Path) -> None:
        part = dest.with_name(f".{dest.name}.part")
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=True,
                              transport=self.transport) as client, client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"the link answered {r.status_code}")
                total = int(r.headers.get("content-length") or 0) or None
                self.state["total"] = total
                if total:
                    free = shutil.disk_usage(dest.parent).free
                    if free < total + 512 * 1024 * 1024:
                        raise RuntimeError(f"not enough room: {total // 2**20} MB to fetch, {free // 2**20} MB free")
                with open(part, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        self.state["bytes"] += len(chunk)
            if total and self.state["bytes"] != total:
                raise RuntimeError(f"the download stopped short ({self.state['bytes']} of {total} bytes)")
            with open(part, "rb") as fh:
                if fh.read(4) != b"GGUF":
                    raise RuntimeError("what came down is not a GGUF file")
            os.replace(part, dest)
            self.state.update(state="done", ended_at=time.time())
        except Exception as e:  # noqa: BLE001 - every failure is the status, never a crash
            self.state.update(state="failed", error=str(e)[:300], ended_at=time.time())
            try:
                part.unlink()
            except OSError:
                pass


FETCH = Fetch()


def register(router: APIRouter) -> None:
    @router.get("/models")
    async def list_models():
        manifest = _manifest()
        p = _models_dir(manifest)
        serving = read_env(_env_file(manifest))
        if not p.is_dir():
            return {
                "models": [],
                "models_dir": str(p),
                "serving": serving,
                "note": "models directory does not exist yet",
            }

        models = sorted(f.name for f in p.glob("*.gguf"))
        # File sizes are useful for the dashboard ("how much disk is each eating")
        with_sizes = []
        for name in models:
            f = p / name
            try:
                size_mb = f.stat().st_size // (1024 * 1024)
            except OSError:
                size_mb = None
            with_sizes.append({"name": name, "size_mb": size_mb,
                               "serving": Path(serving["current"]).name == name})

        return {"models": with_sizes, "models_dir": str(p), "serving": serving}

    @router.post("/models/fetch")
    async def fetch_model(body: dict = Body(...)):
        """{url, name, overwrite?}. Starts the download and answers at once;
        GET /models/fetch says how it is going."""
        manifest = _manifest()
        url, name = str(body.get("url") or ""), str(body.get("name") or "")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "url is an http(s) link")
        if not _NAME.match(name):
            raise HTTPException(400, "name is a plain file name ending in .gguf")
        d = _models_dir(manifest)
        d.mkdir(parents=True, exist_ok=True)
        dest = d / name
        if dest.exists() and not body.get("overwrite"):
            raise HTTPException(409, f"{name} is already here")
        if not FETCH.start(url, dest):
            raise HTTPException(409, f"already fetching {FETCH.state.get('name')}")
        return {"ok": True, "fetch": FETCH.state}

    @router.get("/models/fetch")
    async def fetch_status():
        _manifest()
        return FETCH.state

    @router.delete("/models/{name}")
    async def delete_model(name: str):
        manifest = _manifest()
        if not _NAME.match(name):
            raise HTTPException(400, "name is a plain file name ending in .gguf")
        f = _models_dir(manifest) / name
        if not f.is_file():
            raise HTTPException(404, f"no {name} here")
        if Path(read_env(_env_file(manifest))["current"]).name == name:
            raise HTTPException(409, f"{name} is the one it serves; switch first")
        f.unlink()
        return {"ok": True, "removed": name}

    @router.get("/model")
    async def current_model():
        manifest = _manifest()
        return {"serving": read_env(_env_file(manifest)), "models_dir": str(_models_dir(manifest))}

    @router.put("/model")
    async def set_model(body: dict = Body(...)):
        """{name}: a .gguf in the models dir. Served from llama's next start;
        nothing is restarted here."""
        manifest = _manifest()
        name = str(body.get("name") or "")
        if not _NAME.match(name):
            raise HTTPException(400, "name is a plain file name ending in .gguf")
        f = _models_dir(manifest) / name
        if not f.is_file():
            raise HTTPException(404, f"no {name} in {f.parent}")
        env = _env_file(manifest)
        if env is None:
            raise HTTPException(500, "llama manifest names no env file (serviceSpecific.config_path)")
        return {"ok": True, "serving": set_env_model(env, str(f)),
                "note": "served from llama's next start; restart it to switch now"}

    # TODO Tier 2: POST /completion - proxy to llama-server's /completion
    # endpoint with token streaming. The chat app currently goes direct.
