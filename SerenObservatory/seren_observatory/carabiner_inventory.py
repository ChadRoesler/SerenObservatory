"""
Which carabiners are clipped on this box, whether or not the Rack can reach
them (24 Oct 2026: "Observatory should be aware of what carabiners are
installed on the box").

WHY. Before this the Observatory knew a carabiner only through the Rack's
GET /api/v1/carabiners (seren_observatory.carabiner_routes), which is shut
until carabiners.enabled and a token, and looks only in carabiners.folder -
which nothing writes: the Starwright card puts the clip wherever --into said
(<root>/kbh, or ~/seren-kbh) and records it in the install ledger. So a box
with claude.kbh clipped on read as having none, in its own glance and in
Lodestar's.

Reading one here runs nothing: the version and sha256 come out of the zip
(seren_sinew.carabiners.kbh_identity, the same read the Rack's list makes).
So this is a read like /system/services, open on the same terms, and not
behind the Rack's gate - that gate is for the routes that start a program in
someone's account.

WHERE IT LOOKS, all of them, one entry per .kbh however many found it:
    folder   carabiners.folder, the Rack's own (rack: true while the Rack is on)
    ledger   each seren-carabiner record in the install ledger
             (~/.seren/installed, $SEREN_INSTALLED_DIR moves it, as it does for
             Starwright): the folder its config sits in, and when and from
             where the card put it there
    default  ~/seren-kbh, the card's --into when it is given neither --into
             nor --root: a clip from before the ledger
A ledger record whose .kbh is gone is listed too, with present false: the
ledger says it was put there, and it is not there now. Nothing here reads a
token; the ledger never holds one.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Optional

from seren_sinew.carabiners import NAME, KbhRunner

from . import manifests

LEDGER_ENV = "SEREN_INSTALLED_DIR"
DEFAULT_LEDGER = "~/.seren/installed"
DEFAULT_FOLDER = "~/seren-kbh"
#: What the carabiner card records itself as (seren-carabiner-setup.sh/.ps1).
LEDGER_SERVICE = "seren-carabiner"


def ledger_dir() -> Path:
    """$SEREN_INSTALLED_DIR -> ~/.seren/installed, expanded per call like the
    roster. An empty env value counts as unset."""
    return Path(os.path.expanduser(os.getenv(LEDGER_ENV) or DEFAULT_LEDGER))


def _key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _install(rec: dict[str, Any], record: Path) -> dict[str, Any]:
    """The part of a ledger record that says how this clip got here. Picked,
    not copied: `options` is every flag the card was run with, and only the
    project folder means anything to someone looking at the box."""
    options = rec.get("options") if isinstance(rec.get("options"), dict) else {}
    return {"instance": str(rec.get("instance") or ""), "installed_at": str(rec.get("installed_at") or ""),
            "source": str(rec.get("source") or ""), "source_ref": str(rec.get("source_ref") or ""),
            "project": str(options.get("project") or ""), "record": str(record)}


def ledger_records() -> list[tuple[str, str, dict[str, Any]]]:
    """(carabiner, folder, install) for each seren-carabiner record in the
    ledger. The carabiner is the config's stem (<into>/<carabiner>.yaml is
    what both cards record), the folder the one it sits in; a record that
    says neither is skipped, and so is one that does not parse."""
    d = ledger_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob(f"{LEDGER_SERVICE}*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("service") != LEDGER_SERVICE:
            continue
        config = str(rec.get("config") or "")
        folder = str(rec.get("app_dir") or "") or (os.path.dirname(config) if config else "")
        options = rec.get("options") if isinstance(rec.get("options"), dict) else {}
        # Both cards' --into/-Into default the yaml to <into>/<carabiner>.yaml.
        name = Path(config).stem if config.lower().endswith(".yaml") else str(options.get("carabiner") or "")
        if not folder or not NAME.fullmatch(name or ""):
            continue
        out.append((name, folder, _install(rec, p)))
    return out


def _node() -> str:
    return str((manifests.load_node() or {}).get("hostname") or socket.gethostname())


def installed(carabiners_cfg: Any = None, node: Optional[str] = None) -> dict[str, Any]:
    """Every carabiner on this box: {"carabiners": [entry], "looked_in": [folder]}.

    An entry is the Rack's CarabinerEntry (carabiner, node, kbh, configured,
    version, sha256) and, beside it: present (the .kbh is there), rack (the
    Rack can reach it: it is in carabiners.folder and carabiners are on),
    found_by (which of the places above it sits in: folder / ledger /
    default) and install (the ledger record that names this clip, or null).
    Never raises: a folder that cannot be read is skipped, and the glance
    must not lose the node's vitals over a clip."""
    node = node or _node()
    rack_folder = str(getattr(carabiners_cfg, "folder", "") or "")
    rack_on = bool(getattr(carabiners_cfg, "enabled", False)) and bool(rack_folder)
    records = ledger_records()

    places: list[tuple[str, str]] = []
    if rack_folder:
        places.append(("folder", rack_folder))
    places += [("ledger", folder) for _, folder, _ in records]
    places.append(("default", DEFAULT_FOLDER))

    # Each folder read once, however many places name it.
    folders: dict[str, tuple[str, list[str]]] = {}
    for how, folder in places:
        folder = os.path.abspath(os.path.expanduser(folder))
        _, hows = folders.setdefault(_key(folder), (folder, []))
        if how not in hows:
            hows.append(how)

    entries: dict[str, dict[str, Any]] = {}
    rack_key = _key(os.path.expanduser(rack_folder)) if rack_folder else ""
    for folder, hows in folders.values():
        try:
            found = KbhRunner(folder=folder, python="", node=node).list()
        except OSError:
            found = []
        for e in found:
            k = _key(e.kbh)
            entry = entries.get(k)
            if entry is None:
                entry = entries[k] = {**e.to_dict(), "present": True,
                                      "rack": rack_on and _key(os.path.dirname(e.kbh)) == rack_key,
                                      "found_by": [], "install": None}
            entry["found_by"] += [h for h in hows if h not in entry["found_by"]]

    for name, folder, install in records:
        kbh = os.path.join(os.path.abspath(os.path.expanduser(folder)), f"{name}.kbh")
        k = _key(kbh)
        entry = entries.get(k)
        if entry is None:
            # The ledger says it was put here; it is not here now.
            entry = entries[k] = {"carabiner": name, "node": node, "kbh": kbh,
                                  "configured": os.path.isfile(os.path.join(os.path.dirname(kbh), f"{name}.yaml")),
                                  "version": "", "sha256": "", "present": False, "rack": False,
                                  "found_by": ["ledger"], "install": None}
        # The newest record wins when two name one clip (a reinstall under
        # another instance name leaves the old record behind).
        if entry["install"] is None or install["installed_at"] > entry["install"]["installed_at"]:
            entry["install"] = install
        if "ledger" not in entry["found_by"]:
            entry["found_by"].append("ledger")

    rows = sorted(entries.values(), key=lambda e: (e["carabiner"], e["kbh"]))
    return {"carabiners": rows, "looked_in": [folder for folder, _ in folders.values()]}
