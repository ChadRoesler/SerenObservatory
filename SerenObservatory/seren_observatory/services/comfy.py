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
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import manifests

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

    # TODO Tier 2: POST /generate that proxies to ComfyUI's /prompt endpoint
    # with the observatory-side queueing + image cache logic the C# RuntimeHost
    # currently does. Until then, callers go directly to ComfyUI on its port.
