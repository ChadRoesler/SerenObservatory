"""
ComfyUI on a node: the catalog (what each model file IS, from its header)
and /generate (run a workflow here, hand back its images).

The model files are tiny fakes with real safetensors headers - the tensor
names and metadata the families' real files carry (taken from a real
ComfyUI models folder, 14 Oct 2026) and no weights to speak of.
"""
from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory import comfy_catalog as cc
from seren_observatory.app import create_app
from seren_observatory.services import comfy


def fake_model(path: Path, tensors: dict, meta: dict | None = None) -> Path:
    """A .safetensors file whose header says `tensors` (name -> shape) and
    `meta`; every tensor is one byte, so the file stays tiny."""
    header, off = {}, 0
    for name, shape in tensors.items():
        header[name] = {"dtype": "U8", "shape": shape, "data_offsets": [off, off + 1]}
        off += 1
    if meta:
        header["__metadata__"] = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in meta.items()}
    raw = json.dumps(header).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * off)
    return path


SDXL_LORA = {"lora_te1_text_model_encoder_layers_0_mlp_fc1.alpha": [], "lora_te2_text_model_encoder_layers_0_mlp_fc1.alpha": [],
             "lora_unet_input_blocks_4_1_proj_in.lora_down.weight": [8, 640]}
SD15_LORA = {"lora_te_text_model_encoder_layers_0_mlp_fc1.alpha": [], "lora_unet_down_blocks_0_attentions_0_proj_in.alpha": []}
KREA2_LORA = {"diffusion_model.blocks.0.attn.gate.lora_A.weight": [16, 3072], "diffusion_model.blocks.0.attn.gate.lora_B.weight": [3072, 16]}
WAN_LORA = {"diffusion_model.blocks.0.cross_attn.k.lora_A.weight": [128, 5120]}


@pytest.fixture()
def models(tmp_path: Path) -> Path:
    """A ComfyUI models folder with one of each kind, all in the flat
    folders everyone actually uses."""
    m = tmp_path / "ComfyUI" / "models"
    fake_model(m / "checkpoints" / "waiIllustriousSDXL_v160.safetensors",
               {"conditioner.embedders.0.transformer.text_model.embeddings.position_ids": [1, 77],
                "conditioner.embedders.1.model.ln_final.weight": [1280],
                "model.diffusion_model.input_blocks.0.0.weight": [320, 4, 3, 3]},
               {"modelspec.architecture": "stable-diffusion-xl-v1-base"})
    fake_model(m / "checkpoints" / "krea2TurboOfficialComfy_krea2TurboBf16.safetensors",
               {"blocks.0.attn.qknorm.knorm.scale": [128], "blocks.0.attn.gate.weight": [3072, 3072]})
    (m / "checkpoints" / "juggernautXL_v9.safetensors").write_bytes(b"\xff" * 64)          # not a safetensors at all
    fake_model(m / "diffusion_models" / "wan2.2_i2v_high_noise_14B_fp16.safetensors",
               {"patch_embedding.weight": [5120, 36, 1, 2, 2], "blocks.0.cross_attn.k.weight": [5120, 5120],
                "img_emb.proj.0.weight": [1280]})
    fake_model(m / "diffusion_models" / "realDream_animaV4.safetensors",
               {"model.diffusion_model.blocks.0.adaln_modulation_cross_attn.1.weight": [256, 2048]})
    fake_model(m / "diffusion_models" / "z_image_turbo_bf16.safetensors", {"cap_embedder.0.weight": [2560]})
    fake_model(m / "loras" / "Style_Asanagi_XL_ff.safetensors", SDXL_LORA,
               {"ss_base_model_version": "sdxl_base_v1-0", "ss_sd_model_name": "ponyDiffusionV6XL_v6StartWithThisOne.safetensors",
                "ss_tag_frequency": {"img": {"asanagi": 40, "1girl": 30, "solo": 12}}})
    fake_model(m / "loras" / "mystery_style.safetensors", SDXL_LORA, {"ss_base_model_version": "sdxl_base_v1-0"})
    fake_model(m / "loras" / "AsanagiStyleV3.safetensors", SD15_LORA)
    fake_model(m / "loras" / "Storybook_Folk_Art_Krea2.safetensors", KREA2_LORA, {"modelspec.architecture": "krea2"})
    fake_model(m / "loras" / "Wan22_A14B_T2V_HIGH_Lightning_4steps.safetensors", WAN_LORA)
    fake_model(m / "vae" / "wan_2.1_vae.safetensors", {"conv1.weight": [96, 3, 3, 3, 3], "conv1.bias": [96]})
    fake_model(m / "vae" / "ae.safetensors", {"decoder.conv_in.weight": [512, 16, 3, 3]}, {"modelspec.architecture": "Flux.1-AE"})
    fake_model(m / "vae" / "sdxl_vae.safetensors", {"decoder.conv_in.weight": [512, 4, 3, 3]})
    fake_model(m / "text_encoders" / "qwen_3_4b.safetensors", {"model.embed_tokens.weight": [151936, 2560]})
    return m


# ── what each file is ────────────────────────────────────────────────────

def test_each_family_is_told_from_its_header(models):
    f = cc.scan(models, hash_what="none")["files"]
    assert (f["checkpoints/waiIllustriousSDXL_v160.safetensors"]["arch"], f["checkpoints/waiIllustriousSDXL_v160.safetensors"]["base"]) \
        == ("sdxl", "Illustrious")
    assert f["checkpoints/krea2TurboOfficialComfy_krea2TurboBf16.safetensors"]["arch"] == "krea2"
    wan = f["diffusion_models/wan2.2_i2v_high_noise_14B_fp16.safetensors"]
    assert (wan["arch"], wan["kind"], wan["mode"], wan["expert"], wan["size"]) == \
        ("wan", "video", "image-to-video", "high noise", "14B")
    assert f["diffusion_models/realDream_animaV4.safetensors"]["arch"] == "cosmos"
    assert f["diffusion_models/z_image_turbo_bf16.safetensors"]["arch"] == "zimage"
    assert f["loras/AsanagiStyleV3.safetensors"]["arch"] == "sd15"
    assert f["loras/Storybook_Folk_Art_Krea2.safetensors"]["arch"] == "krea2"
    assert f["loras/Wan22_A14B_T2V_HIGH_Lightning_4steps.safetensors"]["kind"] == "video"
    assert f["vae/wan_2.1_vae.safetensors"]["decodes"] == ["wan", "krea2", "qwen-image"]
    assert f["vae/ae.safetensors"]["arch"] == "flux-ae" and f["vae/sdxl_vae.safetensors"]["arch"] == "sd-vae"
    assert f["text_encoders/qwen_3_4b.safetensors"]["arch"] == "qwen3"


def test_pony_is_told_from_the_training_metadata_and_tags_come_along(models):
    lora = cc.scan(models, hash_what="none")["files"]["loras/Style_Asanagi_XL_ff.safetensors"]
    assert (lora["arch"], lora["base"], lora["base_from"]) == ("sdxl", "Pony", "metadata")
    assert lora["tags"][:2] == ["asanagi", "1girl"]
    assert any("ss_sd_model_name" in w for w in lora["why"])


def test_a_file_that_is_not_safetensors_says_so_and_nothing_breaks(models):
    j = cc.scan(models, hash_what="none")["files"]["checkpoints/juggernautXL_v9.safetensors"]
    assert "not a safetensors file" in j["error"] and j["kind"] == "unknown" and "arch" not in j


def test_civitai_names_the_base_the_tensors_cannot(models, monkeypatch):
    asked = []

    def lookup(digest, key="", host="", timeout=20):
        asked.append((digest, key))
        if digest == cc.sha256(models / "loras" / "mystery_style.safetensors"):
            return {"name": "Mystery Style", "base": "Illustrious", "triggers": ["myst3ry"], "type": "LORA"}
        return None
    monkeypatch.setattr(cc, "civitai_lookup", lookup)
    f = cc.scan(models, hash_what="loras", civitai=True, civitai_key="k")["files"]
    m = f["loras/mystery_style.safetensors"]
    assert (m["base"], m["base_from"], m["triggers"]) == ("Illustrious", "civitai", ["myst3ry"])
    assert f["loras/AsanagiStyleV3.safetensors"]["civitai"] == {"unknown": True}
    assert len(asked) == 5 and all(k == "k" for _, k in asked), "the loras, and only them"
    assert "sha256" not in f["checkpoints/waiIllustriousSDXL_v160.safetensors"]


def test_a_rescan_keeps_what_it_learned_and_a_changed_file_is_looked_at_again(models, monkeypatch):
    calls = []
    monkeypatch.setattr(cc, "civitai_lookup", lambda d, *a, **k: calls.append(d) or None)
    first = cc.scan(models, hash_what="loras", civitai=True)
    n = len(calls)
    again = cc.scan(models, previous=first, hash_what="loras", civitai=True)
    assert len(calls) == n, "nothing hashed or asked twice"
    assert again["files"]["loras/AsanagiStyleV3.safetensors"]["sha256"] == first["files"]["loras/AsanagiStyleV3.safetensors"]["sha256"]
    fake_model(models / "loras" / "AsanagiStyleV3.safetensors", SDXL_LORA)                 # replaced by an sdxl one
    third = cc.scan(models, previous=again, hash_what="loras", civitai=True)
    assert third["files"]["loras/AsanagiStyleV3.safetensors"]["arch"] == "sdxl" and len(calls) == n + 1


def test_a_hand_correction_wins(models):
    f = cc.scan(models, hash_what="none", overrides={"loras/mystery_style.safetensors": {"base": "NoobAI"}})["files"]
    assert f["loras/mystery_style.safetensors"]["base"] == "NoobAI" and f["loras/mystery_style.safetensors"]["corrected"]


def test_only_the_same_arch_is_offered_and_another_base_may_work(models):
    cat = cc.scan(models, hash_what="none", overrides={"loras/mystery_style.safetensors": {"base": "Illustrious"}})
    fits = {f["name"]: f["fit"] for f in cc.compatible(cat, "checkpoints/waiIllustriousSDXL_v160.safetensors")}
    assert fits == {"mystery_style.safetensors": "good", "Style_Asanagi_XL_ff.safetensors": "may work"}
    krea = cc.compatible(cat, "checkpoints/krea2TurboOfficialComfy_krea2TurboBf16.safetensors")
    assert [f["name"] for f in krea] == ["Storybook_Folk_Art_Krea2.safetensors"], "no sdxl, sd15 or video lora on krea2"


def test_the_script_writes_a_catalog_and_never_takes_the_key_as_an_argument(models, tmp_path, monkeypatch):
    out = tmp_path / "catalog.json"
    assert cc.main([str(models), "--out", str(out), "--hash", "none"]) == 0
    assert json.loads(out.read_text())["files"]
    with pytest.raises(SystemExit):
        cc.main([str(models), "--civitai-key", "x"])


# ── the routes ───────────────────────────────────────────────────────────

@pytest.fixture()
def comfy_node(fake_home, models):
    (fake_home / ".seren" / "services" / "comfy.json").write_text(json.dumps({
        "schema_version": 2, "service": "comfy", "service_type": "pid_file", "port": 8188,
        "serviceSpecific": {"checkpoints_dir": str(models / "checkpoints"), "loras_dir": str(models / "loras"),
                            "vae_dir": str(models / "vae")}}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "tok", "civitai_key": "ck"}))
    return fake_home


@pytest.fixture()
async def client(comfy_node):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test",
                           headers={"Authorization": "Bearer tok"}) as c:
        yield c


async def test_the_first_look_makes_a_catalog_and_the_rest_follows(client, comfy_node, monkeypatch):
    started = []
    monkeypatch.setattr(comfy.SCANNER, "start", lambda *a: started.append(a) or True)
    r = await client.get("/api/v1/service/comfy/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["files"]["loras/Storybook_Folk_Art_Krea2.safetensors"]["arch"] == "krea2"
    assert body["summary"]["lora"]["Krea 2"] == 1
    assert (comfy_node / ".seren" / "comfy" / "catalog.json").is_file()
    (_, hash_what, civitai, key, _host), = started
    assert (hash_what, civitai, key) == ("loras", True, "ck"), "hashing and civitai behind it, with the node's key"


async def test_a_scan_is_one_at_a_time(client, monkeypatch):
    r = await client.post("/api/v1/service/comfy/catalog/scan", json={"hash": "everything"})
    assert r.status_code == 400
    assert comfy.SCANNER.lock.acquire(blocking=False)
    try:
        r = await client.post("/api/v1/service/comfy/catalog/scan", json={"hash": "none"})
        assert r.json()["started"] is False
    finally:
        comfy.SCANNER.lock.release()


async def test_generate_wants_a_workflow(client):
    assert (await client.post("/api/v1/service/comfy/generate", json={})).status_code == 400
    assert (await client.post("/api/v1/service/comfy/generate",
                              json={"workflow": {"1": {}}, "timeout_seconds": "soon"})).status_code == 400


# ── generating, against a pretend ComfyUI ───────────────────────────────

class FakeComfy:
    def __init__(self, fail: str = ""):
        self.fail, self.polls, self.freed, self.queued = fail, 0, False, None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            self.queued = json.loads(request.content)
            if self.fail == "refuse":
                return httpx.Response(400, json={"error": {"message": "bad node"}, "node_errors": {"3": "no such lora"}})
            return httpx.Response(200, json={"prompt_id": "p1"})
        if request.url.path == "/history/p1":
            self.polls += 1
            if self.polls < 2:
                return httpx.Response(200, json={})
            status = ({"status_str": "error", "messages": [["execution_error", {"exception_message": "out of memory"}]]}
                      if self.fail == "run" else {"status_str": "success"})
            return httpx.Response(200, json={"p1": {"status": status, "outputs": {
                "9": {"images": [{"filename": "wren_0001.png", "subfolder": "wren", "type": "output"}]}}}})
        if request.url.path == "/view":
            assert request.url.params["subfolder"] == "wren"
            return httpx.Response(200, content=b"\x89PNG fake", headers={"content-type": "image/png"})
        if request.url.path == "/free":
            self.freed = True
            return httpx.Response(200)
        return httpx.Response(404)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://comfy")


async def test_a_workflow_runs_and_its_images_come_back():
    fake = FakeComfy()
    out = await comfy.generate(8188, {"1": {"class_type": "KSampler"}}, 30, True, client=fake.client(), poll=0)
    assert out["ok"] and out["prompt_id"] == "p1" and out["freed"] and fake.freed
    (img,) = out["images"]
    assert (img["filename"], base64.b64decode(img["data"])) == ("wren_0001.png", b"\x89PNG fake")
    assert fake.queued["prompt"] == {"1": {"class_type": "KSampler"}}


async def test_comfy_saying_no_or_failing_is_reported_not_raised():
    out = await comfy.generate(8188, {"1": {}}, 30, False, client=FakeComfy("refuse").client(), poll=0)
    assert not out["ok"] and out["node_errors"] == {"3": "no such lora"}
    out = await comfy.generate(8188, {"1": {}}, 30, False, client=FakeComfy("run").client(), poll=0)
    assert not out["ok"] and out["detail"] == "out of memory"


async def test_a_workflow_that_never_finishes_times_out():
    class Never(FakeComfy):
        def handler(self, request):
            return httpx.Response(200, json={}) if "/history/" in request.url.path else super().handler(request)
    out = await comfy.generate(8188, {"1": {}}, 0.05, False, client=Never().client(), poll=0.01)
    assert not out["ok"] and "did not finish" in out["error"]


async def test_a_profile_is_kept_beside_the_catalog_and_can_be_taken_away(client, comfy_node, monkeypatch):
    monkeypatch.setattr(comfy.SCANNER, "start", lambda *a: True)
    r = await client.put("/api/v1/service/comfy/profiles", json={
        "model": "checkpoints/waiIllustriousSDXL_v160.safetensors",
        "loras": [{"name": "mystery_style.safetensors", "weight": 0.6}], "negative": "sepia", "colour": "ignored"})
    assert r.status_code == 200
    kept = json.loads((comfy_node / ".seren" / "comfy" / "profiles.json").read_text())
    assert kept == {"checkpoints/waiIllustriousSDXL_v160.safetensors": {
        "loras": [{"name": "mystery_style.safetensors", "weight": 0.6}], "negative": "sepia"}}
    assert (await client.get("/api/v1/service/comfy/catalog")).json()["profiles"] == kept
    for bad in ({"loras": [{"weight": 1}]}, {"loras": [{"name": "x", "weight": 9}]}, {"negative": 3}, {}):
        body = {"model": "checkpoints/waiIllustriousSDXL_v160.safetensors", **bad} if bad else {"loras": []}
        assert (await client.put("/api/v1/service/comfy/profiles", json=body)).status_code == 400, bad
    r = await client.put("/api/v1/service/comfy/profiles", json={"model": "checkpoints/waiIllustriousSDXL_v160.safetensors"})
    assert r.json()["profiles"] == {}


def test_a_node_says_which_families_its_comfyui_draws():
    assert comfy.families({"serviceSpecific": {"families": "sdxl, sd15"}}) == ["sdxl", "sd15"]
    assert comfy.families({"serviceSpecific": {"families": ["krea2"]}}) == ["krea2"]
    assert comfy.families({"serviceSpecific": {}}) == [] and comfy.families({}) == [], "none said: no limit"
