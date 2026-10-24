"""
llama on a node: fetching a model from a link into the folder it serves
from, and changing which one it serves - the env file's one line, with the
line it replaced kept for going back.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from seren_observatory.app import create_app
from seren_observatory.services import llama

ENV = """# ~/seren-llama.env - what ~/start_llama.sh serves.
LLAMA_MODEL=/models/Qwen3-4B-Instruct-2507-Q5_K_M.gguf
LLAMA_CTX=8192
LLAMA_EXTRA_ARGS=""
"""


@pytest.fixture()
def llama_node(fake_home: Path) -> Path:
    models = fake_home / "models"
    models.mkdir()
    (models / "Qwen3-4B-Instruct-2507-Q5_K_M.gguf").write_bytes(b"GGUF" + b"\0" * 64)
    env = fake_home / "seren-llama.env"
    env.write_text(ENV.replace("/models/Qwen3-4B-Instruct-2507-Q5_K_M.gguf", str(models / "Qwen3-4B-Instruct-2507-Q5_K_M.gguf")))
    (fake_home / ".seren" / "services" / "llama.json").write_text(json.dumps({
        "schema_version": 1, "service": "llama", "service_type": "pid_file", "port": 8090,
        "serviceSpecific": {"models_dir": str(models), "config_path": str(env)}}))
    (fake_home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "tok"}))
    return fake_home


@pytest.fixture()
async def client(llama_node):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test",
                           headers={"Authorization": "Bearer tok"}) as c:
        yield c


@pytest.fixture()
def lyceum(monkeypatch):
    """A Lyceum link that hands out a small GGUF (or, at /junk, not one)."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/junk.gguf"):
            return httpx.Response(200, content=b"<html>nope</html>")
        if req.url.path.endswith("/gone.gguf"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"GGUF" + b"\1" * 4096)
    monkeypatch.setattr(llama, "FETCH", llama.Fetch())
    llama.FETCH.transport = httpx.MockTransport(handler)
    return llama.FETCH


async def _wait(client):
    for _ in range(100):
        st = (await client.get("/api/v1/service/llama/models/fetch")).json()
        if st["state"] != "fetching":
            return st
        time.sleep(0.05)
    raise AssertionError("fetch never finished")


async def test_a_model_comes_down_from_a_link_and_is_served_next(client, llama_node, lyceum):
    r = await client.post("/api/v1/service/llama/models/fetch",
                          json={"url": "http://spark:7258/viewer/dl/abc/consolidator-v1-Q5_K_M.gguf",
                                "name": "consolidator-v1-Q5_K_M.gguf"})
    assert r.status_code == 200, r.text
    st = await _wait(client)
    assert st["state"] == "done" and st["bytes"] == 4100
    listed = (await client.get("/api/v1/service/llama/models")).json()
    names = {m["name"]: m["serving"] for m in listed["models"]}
    assert names == {"Qwen3-4B-Instruct-2507-Q5_K_M.gguf": True, "consolidator-v1-Q5_K_M.gguf": False}

    r = await client.put("/api/v1/service/llama/model", json={"name": "consolidator-v1-Q5_K_M.gguf"})
    assert r.status_code == 200, r.text
    env = (llama_node / "seren-llama.env").read_text()
    assert f"LLAMA_MODEL={llama_node / 'models' / 'consolidator-v1-Q5_K_M.gguf'}" in env
    assert f"LLAMA_MODEL_PREVIOUS={llama_node / 'models' / 'Qwen3-4B-Instruct-2507-Q5_K_M.gguf'}" in env
    assert "LLAMA_CTX=8192" in env and env.startswith("# ~/seren-llama.env"), "every other line kept"

    # going back is one more PUT, and the previous line follows
    back = (await client.put("/api/v1/service/llama/model", json={"name": "Qwen3-4B-Instruct-2507-Q5_K_M.gguf"})).json()
    assert back["serving"]["current"].endswith("Qwen3-4B-Instruct-2507-Q5_K_M.gguf")
    assert back["serving"]["previous"].endswith("consolidator-v1-Q5_K_M.gguf")
    assert (llama_node / "seren-llama.env").read_text().count("LLAMA_MODEL_PREVIOUS=") == 1

    # the one it serves can't be deleted; the other can
    assert (await client.delete("/api/v1/service/llama/models/Qwen3-4B-Instruct-2507-Q5_K_M.gguf")).status_code == 409
    assert (await client.delete("/api/v1/service/llama/models/consolidator-v1-Q5_K_M.gguf")).status_code == 200


async def test_what_is_not_a_model_does_not_land(client, llama_node, lyceum):
    for name in ("junk.gguf", "gone.gguf"):
        r = await client.post("/api/v1/service/llama/models/fetch", json={"url": f"http://spark/{name}", "name": name})
        assert r.status_code == 200
        st = await _wait(client)
        assert st["state"] == "failed", st
        assert not (llama_node / "models" / name).exists()
        assert not list((llama_node / "models").glob(".*.part"))


async def test_names_and_links_are_checked(client, llama_node, lyceum):
    bad = [{"url": "file:///etc/passwd", "name": "x.gguf"}, {"url": "http://a/x", "name": "../x.gguf"},
           {"url": "http://a/x", "name": "x.bin"}]
    for body in bad:
        assert (await client.post("/api/v1/service/llama/models/fetch", json=body)).status_code == 400
    r = await client.post("/api/v1/service/llama/models/fetch",
                          json={"url": "http://a/x", "name": "Qwen3-4B-Instruct-2507-Q5_K_M.gguf"})
    assert r.status_code == 409, "already here, and not overwritten unless asked"
    assert (await client.put("/api/v1/service/llama/model", json={"name": "nope.gguf"})).status_code == 404


async def test_changing_the_model_needs_the_token(llama_node):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        assert (await c.put("/api/v1/service/llama/model", json={"name": "x.gguf"})).status_code in (401, 403)
