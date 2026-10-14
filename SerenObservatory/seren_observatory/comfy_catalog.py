"""
seren_observatory.comfy_catalog
════════════════════════════════════════════════════════════════════════

What is in a ComfyUI models folder, and what goes with what - worked out
from the files, not from where someone happened to drop them.

    python -m seren_observatory.comfy_catalog <ComfyUI/models> [--out catalog.json]
                                              [--hash loras|all|none] [--civitai]

A pile of checkpoints and LoRAs in two flat folders says nothing about which
LoRA a model can wear. Every .safetensors file says a great deal, though,
and says it in its first few kilobytes: the JSON header holds the tensor
names (and shapes), and usually a block of training metadata. Reading it
never touches the weights, so a 28 GB video model costs the same as a 20 MB
LoRA. From that, each file gets:

    role      checkpoint | diffusion | lora | vae | text_encoder | other
    arch      the network it is (or plugs into): sdxl, sd15, krea2, flux,
              wan, cosmos, zimage, qwen-image ... - a HARD fit: an sdxl LoRA
              cannot load on a krea2 model, so it is never offered there
    base      within an arch, what it was trained on (Pony, Illustrious,
              NoobAI, SDXL 1.0 ...) - a SOFT fit: a Pony LoRA on an
              Illustrious model is "may work", not "won't load". The tensors
              cannot tell these apart (they are all sdxl); training metadata,
              CivitAI (looked up by the file's hash) and, last, the name can
    kind      image | video - so an image request never drags in a video
              LoRA, and a video one never sees image LoRAs
    triggers  the words a LoRA wants in the prompt (CivitAI's trained words,
              else its most frequent training tags)

Why each guess was made rides along (`why`), and a file can be corrected by
hand in an overrides file beside the catalog - a correction always wins.

Nothing is moved or renamed: ComfyUI workflows name files by path, and a
tidy-up would break every saved one.

Standard library only, on purpose: it runs inside the Observatory on a
Comfy node, and as a plain script on a box that has no Observatory yet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

SCHEMA = 1
MAX_HEADER = 100 * 1024 * 1024           # a real header is KBs to a few MB
CIVITAI_HOST = "https://civitai.com"

# ComfyUI's model folders -> the role a file there plays.
ROLES = {
    "checkpoints": "checkpoint", "diffusion_models": "diffusion", "unet": "diffusion",
    "loras": "lora", "vae": "vae", "text_encoders": "text_encoder", "clip": "text_encoder",
}

# Display names for the archs, and which are video.
ARCHS = {
    "sd15": "SD 1.5", "sdxl": "SDXL", "flux": "Flux", "krea2": "Krea 2", "wan": "Wan (video)",
    "cosmos": "Cosmos (Anima)", "zimage": "Z-Image", "qwen-image": "Qwen-Image",
    "hunyuan-video": "HunyuanVideo", "ltx-video": "LTX-Video",
}
VIDEO_ARCHS = {"wan", "hunyuan-video", "ltx-video"}

# What a training-metadata string says about the arch.
META_ARCH = [
    (re.compile(r"krea[-_ ]?2", re.I), "krea2"),
    (re.compile(r"stable-diffusion-xl|sdxl", re.I), "sdxl"),
    (re.compile(r"stable-diffusion-v1|sd_v1|sd-v1|^sd1", re.I), "sd15"),
    (re.compile(r"flux", re.I), "flux"),
    (re.compile(r"\bwan", re.I), "wan"),
    (re.compile(r"hunyuan[-_ ]?video", re.I), "hunyuan-video"),
    (re.compile(r"\bltx", re.I), "ltx-video"),
    (re.compile(r"qwen[-_ ]?image", re.I), "qwen-image"),
    (re.compile(r"z[-_ ]?image", re.I), "zimage"),
]

# Within sdxl, what a name or a model string says about the base.
BASE_HINTS = [
    (re.compile(r"pony", re.I), "Pony"),
    (re.compile(r"noob", re.I), "NoobAI"),
    (re.compile(r"illustrious|(?:^|[^a-z])il(?:[^a-z]|$)|_il|ill_|wai", re.I), "Illustrious"),
]


# ── reading a file ─────────────────────────────────────────────────────────

def read_header(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(tensors, metadata) from a .safetensors header. Raises ValueError for
    a file that is not one (or whose header cannot be read)."""
    with open(path, "rb") as f:
        raw = f.read(8)
        if len(raw) != 8:
            raise ValueError("too short to be a safetensors file")
        n = struct.unpack("<Q", raw)[0]
        if n <= 1 or n > MAX_HEADER:
            raise ValueError("not a safetensors file (its header length makes no sense)")
        try:
            header = json.loads(f.read(n))
        except ValueError as ex:
            raise ValueError(f"unreadable header ({ex})") from None
    if not isinstance(header, dict):
        raise ValueError("unreadable header")
    meta = header.pop("__metadata__", None) or {}
    return header, meta if isinstance(meta, dict) else {}


def sha256(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


# ── what a file is ─────────────────────────────────────────────────────────

_PREFIXES = ("model.diffusion_model.", "diffusion_model.", "transformer.", "base_model.model.",
             "model.", "lora_unet_", "unet.")


def _strip(key: str) -> str:
    for p in _PREFIXES:
        if key.startswith(p):
            return key[len(p):]
    return key


def _arch_from_meta(meta: dict[str, Any]) -> Optional[tuple[str, str]]:
    for field in ("modelspec.architecture", "ss_base_model_version", "modelspec.title", "ss_sd_model_name"):
        v = str(meta.get(field) or "")
        if not v:
            continue
        for pat, arch in META_ARCH:
            if pat.search(v):
                return arch, f"{field}: {v[:60]}"
    return None


def _arch_from_tensors(role: str, keys: list[str]) -> Optional[tuple[str, str]]:
    joined = "\n".join(keys)
    flat = "\n".join(_strip(k) for k in keys)
    if role == "lora":
        if "lora_te2_" in joined or ("lora_te1_" in joined and "lora_te_text" not in joined):
            return "sdxl", "two text encoders (lora_te1/te2)"
        if "lora_te_" in joined:
            return "sd15", "one text encoder (lora_te_)"
        if "double_blocks" in flat:
            return "flux", "double_blocks"
        if re.search(r"(^|\n)blocks[._]\d+[._]cross_attn", flat):
            return "wan", "blocks.N.cross_attn"
        if re.search(r"(^|\n)blocks[._]\d+[._]attn[._](gate|qknorm|wk|wq)", flat) or "text_fusion.layerwise_blocks" in flat:
            return "krea2", "blocks.N.attn.gate"
        if re.search(r"down_blocks[._]\d+[._]attentions", flat):
            return "sd15", "diffusers down_blocks (unet)"
        return None
    if any(k.startswith("conditioner.embedders.1") for k in keys):
        return "sdxl", "conditioner.embedders.1 (a second text encoder)"
    if "double_blocks." in flat:
        return "flux", "double_blocks"
    if "patch_embedding" in flat and re.search(r"(^|\n)blocks\.\d+\.cross_attn", flat):
        return "wan", "patch_embedding + blocks.N.cross_attn"
    if "adaln_modulation_cross_attn" in flat:
        return "cosmos", "adaln_modulation_cross_attn"
    if "cap_embedder" in flat:
        return "zimage", "cap_embedder"
    if "img_mod" in flat and "transformer_blocks" in flat:
        return "qwen-image", "transformer_blocks.N.img_mod"
    if re.search(r"(^|\n)blocks\.\d+\.attn\.(qknorm|gate|wk)", flat):
        return "krea2", "blocks.N.attn (qknorm/gate)"
    if "model.diffusion_model.input_blocks" in joined and "cond_stage_model" in joined:
        return "sd15", "input_blocks + one text encoder"
    return None


def _vae_arch(tensors: dict[str, Any], meta: dict[str, Any]) -> tuple[Optional[str], str, list[str]]:
    """A VAE's family, and the archs it decodes for (by its latent space)."""
    spec = str(meta.get("modelspec.architecture") or meta.get("modelspec.title") or "")
    shapes = {k: v.get("shape") for k, v in tensors.items() if isinstance(v, dict)}
    five_d = any(isinstance(s, list) and len(s) == 5 for s in shapes.values())
    if five_d:
        return "wan-vae", "3-D convolutions (a video VAE)", ["wan", "krea2", "qwen-image"]
    if re.search(r"flux", spec, re.I):
        return "flux-ae", f"modelspec: {spec}", ["flux", "zimage"]
    conv_in = shapes.get("decoder.conv_in.weight")
    if isinstance(conv_in, list) and len(conv_in) == 4:
        if conv_in[1] == 16:
            return "flux-ae", "16-channel latents", ["flux", "zimage"]
        if conv_in[1] == 4:
            return "sd-vae", "4-channel latents", ["sd15", "sdxl"]
    return None, "", []


def _base(arch: str, name: str, meta: dict[str, Any]) -> tuple[Optional[str], str, str]:
    """(base, how sure, why) - only sdxl has bases worth telling apart."""
    if arch != "sdxl":
        return None, "", ""
    for field in ("ss_sd_model_name", "modelspec.title", "ss_output_name"):
        v = str(meta.get(field) or "")
        for pat, base in BASE_HINTS:
            if v and pat.search(v):
                return base, "metadata", f"{field}: {v[:60]}"
    for pat, base in BASE_HINTS:
        if pat.search(name):
            return base, "name", f"the name says {base}"
    return None, "", ""


def _video_detail(name: str, tensors: dict[str, Any], flat_keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if any("img_emb" in k or "k_img" in k for k in flat_keys) or re.search(r"i2v", name, re.I):
        out["mode"] = "image-to-video"
    elif re.search(r"t2v", name, re.I):
        out["mode"] = "text-to-video"
    if re.search(r"high", name, re.I):
        out["expert"] = "high noise"
    elif re.search(r"low", name, re.I):
        out["expert"] = "low noise"
    pe = next((v.get("shape") for k, v in tensors.items() if _strip(k) == "patch_embedding.weight" and isinstance(v, dict)), None)
    if isinstance(pe, list) and pe:
        out["size"] = {5120: "14B", 3072: "5B", 1536: "1.3B"}.get(pe[0], f"dim {pe[0]}")
    elif re.search(r"14b|a14b", name, re.I):
        out["size"] = "14B"
    return out


def _top_tags(meta: dict[str, Any], n: int = 8) -> list[str]:
    raw = meta.get("ss_tag_frequency")
    try:
        freq = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return []
    counts: dict[str, int] = {}
    for bucket in freq.values() if isinstance(freq, dict) else []:
        if isinstance(bucket, dict):
            for tag, c in bucket.items():
                counts[tag.strip()] = counts.get(tag.strip(), 0) + int(c or 0)
    return [t for t, _ in sorted(counts.items(), key=lambda x: -x[1])[:n] if t]


def describe(path: Path, role: str) -> dict[str, Any]:
    """Everything the header says about one file (no hashing, no network)."""
    entry: dict[str, Any] = {"role": role, "bytes": path.stat().st_size}
    try:
        tensors, meta = read_header(path)
    except (OSError, ValueError) as ex:
        entry.update(error=str(ex), kind="unknown")
        return entry
    keys = list(tensors)
    name = path.name
    why: list[str] = []
    if role == "vae":
        fam, how, decodes = _vae_arch(tensors, meta)
        if fam:
            entry.update(arch=fam, decodes=decodes)
            why.append(how)
    elif role == "text_encoder":
        n = name.lower()
        fam = ("qwen3-vl" if "qwen3-vl" in n or "qwen3_vl" in n else "umt5" if "umt5" in n
               else "qwen3" if "qwen_3" in n or "qwen3" in n else "clip-l" if "clip_l" in n
               else "clip-g" if "clip_g" in n else "t5xxl" if "t5xxl" in n else None)
        if fam:
            entry["arch"] = fam
            why.append("the name")
    elif role in ("checkpoint", "diffusion", "lora"):
        found = _arch_from_meta(meta) if role == "lora" else None
        found = found or _arch_from_tensors(role, keys) or _arch_from_meta(meta)
        if found:
            entry["arch"] = found[0]
            why.append(found[1])
            base, sure, bwhy = _base(found[0], name, meta)
            if base:
                entry.update(base=base, base_from=sure)
                why.append(bwhy)
            if found[0] in VIDEO_ARCHS:
                entry.update(_video_detail(name, tensors, [_strip(k) for k in keys]))
            if role == "lora":
                tags = _top_tags(meta)
                if tags:
                    entry["tags"] = tags
                if meta.get("ss_network_module"):
                    entry["network"] = str(meta["ss_network_module"]).rsplit(".", 1)[-1]
                elif any(".lokr_" in k for k in keys):
                    entry["network"] = "lokr"
    arch = entry.get("arch")
    entry["kind"] = "video" if arch in VIDEO_ARCHS else ("image" if arch else "unknown")
    entry["why"] = why
    return entry


# ── CivitAI ────────────────────────────────────────────────────────────────

def civitai_lookup(digest: str, key: str = "", host: str = CIVITAI_HOST, timeout: float = 20) -> Optional[dict[str, Any]]:
    """The model version with this file hash, or None when CivitAI does not
    know it. The key, when there is one, goes only in a header to `host`."""
    req = urllib.request.Request(f"{host.rstrip('/')}/api/v1/model-versions/by-hash/{digest}",
                                 headers={"Accept": "application/json", "User-Agent": "SerenObservatory comfy catalog",
                                          **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            v = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return None
        raise
    model = v.get("model") or {}
    return {"version_id": v.get("id"), "model_id": v.get("modelId"), "name": model.get("name"),
            "version": v.get("name"), "type": model.get("type"), "base": v.get("baseModel"),
            "triggers": [w for w in v.get("trainedWords") or [] if isinstance(w, str)][:20],
            "nsfw": model.get("nsfw")}


# CivitAI's base names -> our arch, so a file the tensors could not place
# still gets one, and a disagreement is visible.
def arch_of_civitai_base(base: str) -> Optional[str]:
    b = (base or "").lower()
    if any(x in b for x in ("pony", "illustrious", "noob", "sdxl")):
        return "sdxl"
    if b.startswith("sd 1") or b == "sd1.5":
        return "sd15"
    for pat, arch in META_ARCH:
        if pat.search(b):
            return arch
    return None


def civitai_base(base: str) -> Optional[str]:
    """The sdxl base CivitAI names, in our words."""
    b = (base or "").lower()
    for pat, name in BASE_HINTS:
        if pat.search(b):
            return name
    return "SDXL" if b.startswith("sdxl") else None


# ── the catalog ────────────────────────────────────────────────────────────

def _walk(models: Path):
    for folder, role in ROLES.items():
        d = models / folder
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.safetensors")):
            if p.is_file():
                yield folder, role, p


def scan(models: Path, previous: Optional[dict[str, Any]] = None, hash_what: str = "loras",
         civitai: bool = False, civitai_key: str = "", civitai_host: str = CIVITAI_HOST,
         overrides: Optional[dict[str, Any]] = None, log=lambda m: None) -> dict[str, Any]:
    """The catalog of `models` (a ComfyUI models folder). A file unchanged
    since `previous` (same size and mtime) keeps what was learned about it -
    its hash and CivitAI answer above all - so a re-scan is cheap."""
    prev = (previous or {}).get("files", {})
    files: dict[str, Any] = {}
    for folder, role, p in _walk(models):
        rel = f"{folder}/{p.relative_to(models / folder).as_posix()}"
        st = p.stat()
        stamp = {"bytes": st.st_size, "mtime": int(st.st_mtime)}
        old = prev.get(rel)
        if old and old.get("bytes") == stamp["bytes"] and old.get("mtime") == stamp["mtime"]:
            entry = dict(old)
        else:
            entry = describe(p, role)
            entry.update(stamp)
        entry["name"] = p.relative_to(models / folder).as_posix()        # what ComfyUI calls it
        entry["folder"] = folder
        want_hash = hash_what == "all" or (hash_what == "loras" and role == "lora")
        if want_hash and "sha256" not in entry and "error" not in entry:
            log(f"hashing {rel} ({entry['bytes'] / 1e9:.2f} GB)")
            entry["sha256"] = sha256(p)
        if civitai and entry.get("sha256") and "civitai" not in entry:
            try:
                found = civitai_lookup(entry["sha256"], civitai_key, civitai_host)
                entry["civitai"] = found or {"unknown": True}
                log(f"civitai {rel}: {(found or {}).get('name') or 'not known'}")
            except (urllib.error.URLError, OSError, ValueError) as ex:
                log(f"civitai {rel}: lookup failed ({ex}) - will try again next scan")
        _fold_civitai(entry)
        files[rel] = entry
    for rel, fix in (overrides or {}).items():
        if rel in files and isinstance(fix, dict):
            files[rel].update(fix)
            files[rel]["corrected"] = True
            if "arch" in fix:
                files[rel]["kind"] = "video" if fix["arch"] in VIDEO_ARCHS else "image"
    return {"schema": SCHEMA, "scanned_at": int(time.time()), "models": str(models), "files": files}


def _fold_civitai(entry: dict[str, Any]) -> None:
    """What CivitAI said, folded into the entry: its base is better than a
    name guess, its trained words are the triggers."""
    c = entry.get("civitai") or {}
    if not c or c.get("unknown"):
        return
    arch = arch_of_civitai_base(c.get("base") or "")
    if arch and not entry.get("arch"):
        entry["arch"] = arch
        entry["kind"] = "video" if arch in VIDEO_ARCHS else "image"
        entry.setdefault("why", []).append(f"civitai base: {c.get('base')}")
    base = civitai_base(c.get("base") or "") if entry.get("arch") == "sdxl" else None
    if base and entry.get("base_from") != "metadata":
        entry.update(base=base, base_from="civitai")
    if c.get("triggers"):
        entry["triggers"] = c["triggers"]


def compatible(catalog: dict[str, Any], model: str, kind: str = "lora") -> list[dict[str, Any]]:
    """What can go with `model` (a catalog key): the same arch only - a
    different one would not load - and, within it, the same base first;
    another base comes back marked `fit: "may work"`."""
    files = catalog.get("files", {})
    m = files.get(model) or {}
    out = []
    for rel, f in files.items():
        if f.get("role") != kind or f.get("arch") != m.get("arch"):
            continue
        same = not m.get("base") or not f.get("base") or f["base"] == m["base"]
        out.append({**f, "key": rel, "fit": "good" if same else "may work"})
    return sorted(out, key=lambda f: (f["fit"] != "good", f["name"].lower()))


def summary(catalog: dict[str, Any]) -> dict[str, Any]:
    """Counts by role and arch - the shape of a models folder at a glance."""
    out: dict[str, dict[str, int]] = {}
    for f in catalog.get("files", {}).values():
        label = ARCHS.get(f.get("arch"), f.get("arch") or "unknown")
        if f.get("base"):
            label += f" / {f['base']}"
        out.setdefault(f.get("role", "?"), {}).setdefault(label, 0)
        out[f.get("role", "?")][label] += 1
    return out


# ── as a script ────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Catalog a ComfyUI models folder from the files themselves")
    ap.add_argument("models", help="the ComfyUI models folder")
    ap.add_argument("--out", default="", help="write the catalog here (and read the last one from here)")
    ap.add_argument("--overrides", default="", help="a JSON file of corrections: {relpath: {arch, base, ...}}")
    ap.add_argument("--hash", default="loras", choices=("none", "loras", "all"),
                    help="which files to hash for CivitAI (all: checkpoints too - slow, once)")
    ap.add_argument("--civitai", action="store_true", help="look each hashed file up on CivitAI")
    ap.add_argument("--civitai-host", default=os.environ.get("CIVITAI_HOST", CIVITAI_HOST))
    ap.add_argument("--summary", action="store_true", help="print counts by role and arch")
    a = ap.parse_args(argv)
    # The key is never an argument: a file named by CIVITAI_KEY_FILE, or CIVITAI_KEY.
    key = os.environ.get("CIVITAI_KEY", "")
    if not key and os.environ.get("CIVITAI_KEY_FILE"):
        key = Path(os.environ["CIVITAI_KEY_FILE"]).expanduser().read_text(encoding="utf-8").strip()
    out = Path(a.out).expanduser() if a.out else None
    previous = json.loads(out.read_text(encoding="utf-8")) if out and out.is_file() else None
    overrides = json.loads(Path(a.overrides).read_text(encoding="utf-8")) if a.overrides else None
    cat = scan(Path(a.models).expanduser(), previous, a.hash, a.civitai, key, a.civitai_host, overrides,
               log=lambda m: print(m, file=sys.stderr, flush=True))
    text = json.dumps(cat, indent=1)
    if out:
        tmp = out.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)
    if a.summary or not out:
        print(json.dumps(summary(cat), indent=1) if a.summary else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
