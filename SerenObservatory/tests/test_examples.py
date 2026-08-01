"""
The shipped sample manifests in examples/seren/.

Those files are the reference an operator copies when Observatory reports an
empty node — which is the single most common thing to go wrong, because
Observatory reads ~/.seren/services/*.json and nothing else. Documentation
that doesn't load is worse than none: it costs somebody an evening before
they stop believing it.

So the samples are executable. Every one is loaded through the real loader
and asserted to resolve to the service_type it's meant to demonstrate.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from seren_observatory import manifests

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "seren"


def test_the_examples_directory_exists_where_the_readme_says():
    assert EXAMPLES.is_dir(), f"missing {EXAMPLES}"
    assert (EXAMPLES / "README.md").is_file()
    assert (EXAMPLES / "node.json").is_file()
    assert (EXAMPLES / "services").is_dir()


def test_every_sample_is_valid_json():
    """The loader skips unparseable files SILENTLY so one bad manifest can't
    take down the whole report. Great behaviour, terrible for a sample —
    a broken one would just never appear and never say why."""
    for p in sorted(EXAMPLES.rglob("*.json")):
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as ex:
            pytest.fail(f"{p.name} is not valid JSON: {ex}")


@pytest.fixture
def home_from_examples(fake_home: Path) -> Path:
    """Drop the shipped samples into a fake ~/.seren and load from there."""
    shutil.copy(EXAMPLES / "node.json", fake_home / ".seren" / "node.json")
    for p in (EXAMPLES / "services").glob("*.json"):
        shutil.copy(p, fake_home / ".seren" / "services" / p.name)
    return fake_home


def test_all_samples_load(home_from_examples):
    svcs = manifests.load_services()
    assert set(svcs) == {"seren-memory", "llama", "coral", "searxng"}, sorted(svcs)


def test_each_sample_demonstrates_the_type_it_claims(home_from_examples):
    """The README sends people to a specific file per service_type. If one
    resolved differently the doc would be teaching the wrong shape."""
    svcs = manifests.load_services()
    expected = {
        "seren-memory": "systemd",         # what setup-seren-service.sh writes
        "llama": "pid_file",               # what node prep writes
        "coral": "library",                # no daemon, port 0
        "searxng": "docker_compose",
    }
    for name, want in expected.items():
        got = manifests.service_type(svcs[name])
        assert got == want, f"{name}: README says {want}, loader says {got}"


def test_the_pid_file_sample_relies_on_INFERENCE_not_a_declaration(home_from_examples):
    """llama.json deliberately omits service_type, because that's what every
    manifest written before the field existed looks like. If the backcompat
    default ever changes, this is the test that notices."""
    raw = json.loads((EXAMPLES / "services" / "llama.json").read_text())
    assert "service_type" not in raw
    assert manifests.service_type(raw) == "pid_file"


def test_the_library_sample_declares_port_zero(home_from_examples):
    """port 0 IS the statement 'this doesn't listen'. A library service with
    a nonzero port would get health-checked against nothing forever."""
    coral = manifests.load_services()["coral"]
    assert coral["port"] == 0


def test_each_type_carries_the_fields_its_handler_requires(home_from_examples):
    """A sample missing a required field parses fine and then fails at
    dispatch — the worst kind of example, since it looks correct."""
    svcs = manifests.load_services()
    required = {
        "seren-memory": ["systemd_unit", "port"],
        "llama": ["start_script", "stop_script", "pid_path"],
        "searxng": ["compose_file", "compose_service"],
    }
    for name, fields in required.items():
        for f in fields:
            assert svcs[name].get(f), f"{name}.json is missing {f}"


def test_node_sample_loads(home_from_examples):
    node = manifests.load_node()
    assert node is not None
    assert node["hostname"] and node["schema_version"] <= manifests.SCHEMA_VERSION


def test_no_sample_claims_a_future_schema():
    """The loader silently SKIPS a manifest whose schema_version exceeds what
    it understands. A sample that did would be invisible and inexplicable."""
    for p in sorted(EXAMPLES.rglob("*.json")):
        v = json.loads(p.read_text(encoding="utf-8")).get("schema_version", 0)
        assert v <= manifests.SCHEMA_VERSION, f"{p.name} declares schema {v}"
