"""
GET /api/v1/system/node without a ~/.seren/node.json (20 Oct 2026). The ask: "noticed lodestar isnt reporting ips?" - no node ever had the file, so the
manifest came back null and every Lodestar card said "-" for host and ip.
Now the box says what it knows: hostname, addresses, platform, cores, with
`source: discovered`. The file still wins when it exists.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from seren_observatory import manifests
from seren_observatory.app import create_app
from seren_observatory.config import ObservatoryConfig


def _client(monkeypatch):
    monkeypatch.setattr("seren_observatory.auth.load_token", lambda *a, **k: "tok")
    monkeypatch.setattr("seren_observatory.app.load_token", lambda *a, **k: "tok")
    return TestClient(create_app(ObservatoryConfig()), headers={"Authorization": "Bearer tok"})


def test_without_the_file_the_node_is_discovered(fake_home, monkeypatch):
    assert not manifests.node_path().is_file()
    c = _client(monkeypatch)
    m = c.get("/api/v1/system/node").json()["manifest"]
    assert m["source"] == "discovered"
    assert m["hostname"] and isinstance(m["ip_addresses"], list)
    assert m["platform"] and m["cpu_cores"]


def test_the_file_still_wins(fake_home, monkeypatch):
    manifests.node_path().parent.mkdir(parents=True, exist_ok=True)
    manifests.node_path().write_text(json.dumps({"schema_version": 1, "hostname": "brain", "ip_addresses": ["192.0.2.200"]}))
    c = _client(monkeypatch)
    m = c.get("/api/v1/system/node").json()["manifest"]
    assert m["hostname"] == "brain" and m["ip_addresses"] == ["192.0.2.200"] and "source" not in m


def test_discovery_never_raises_when_the_box_is_shy(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostbyname_ex", lambda h: (_ for _ in ()).throw(OSError("no dns")))
    monkeypatch.setattr(manifests.os, "name", "nt", raising=False)

    class Dead:
        def __init__(self, *a, **k): pass
        def connect(self, *a): raise OSError("no route")
        def close(self): pass
    monkeypatch.setattr(socket, "socket", Dead)
    m = manifests.discover_node()
    assert m["hostname"] and m["ip_addresses"] == [] and m["source"] == "discovered"
