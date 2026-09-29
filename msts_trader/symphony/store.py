"""Strategies on disk (one JSON per symphony) and the run log.

~/.msts-trader/strategies/<id>.json   (override: MSTS_STRATEGIES_DIR)
~/.msts-trader/runs.jsonl             (append-only; override: MSTS_RUNS_LOG)
~/.msts-trader/sleeve_snapshots.jsonl (append-only; override: MSTS_SNAPSHOTS_LOG)

Writes are temp-file + atomic replace with one `.bak` of the previous
version, the same pattern the sleeve ledger uses.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .model import ID_RE, Symphony


class StoreError(ValueError):
    pass


def strategies_dir() -> Path:
    return Path(os.environ.get("MSTS_STRATEGIES_DIR") or os.path.expanduser("~/.msts-trader/strategies"))


def runs_log() -> Path:
    return Path(os.environ.get("MSTS_RUNS_LOG") or os.path.expanduser("~/.msts-trader/runs.jsonl"))


def snapshots_log() -> Path:
    return Path(os.environ.get("MSTS_SNAPSHOTS_LOG") or os.path.expanduser("~/.msts-trader/sleeve_snapshots.jsonl"))


def _path(sid: str) -> Path:
    if not ID_RE.fullmatch(sid or ""):
        raise StoreError(f"invalid strategy id {sid!r}")
    return strategies_dir() / f"{sid}.json"


def list_all() -> list[Symphony]:
    d = strategies_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(Symphony.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception:
            continue  # a hand-broken file must not hide the rest
    return out


def get(sid: str) -> Symphony:
    p = _path(sid)
    if not p.exists():
        raise StoreError(f"no strategy {sid!r} (looked in {p.parent})")
    try:
        return Symphony.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise StoreError(f"strategy file {p} is invalid: {e}") from e


def exists(sid: str) -> bool:
    return _path(sid).exists()


def save(sym: Symphony) -> Path:
    p = _path(sym.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(sym.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
    if p.exists():
        os.replace(p, p.with_suffix(".json.bak"))
    os.replace(tmp, p)
    return p


def delete(sid: str) -> None:
    p = _path(sid)
    if not p.exists():
        raise StoreError(f"no strategy {sid!r}")
    os.replace(p, p.with_suffix(".json.bak"))


def unique_id(base: str) -> str:
    sid, k = base[:40], 2
    while exists(sid):
        suffix = f"-{k}"
        sid = base[: 40 - len(suffix)] + suffix
        k += 1
    return sid


def _append(p: Path, entry: dict) -> dict:
    entry = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **entry}
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def log_run(entry: dict) -> dict:
    return _append(runs_log(), entry)


def read_runs(sid: str | None = None, limit: int = 100) -> list[dict]:
    p = runs_log()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if sid is None or e.get("strategy") == sid:
            out.append(e)
    return out[-limit:][::-1]


def log_snapshot(entry: dict) -> dict:
    """Sleeve state {cash, holdings, contributed} after a live run / capital change."""
    return _append(snapshots_log(), entry)


def read_snapshots(sid: str) -> list[dict]:
    """Oldest first."""
    p = snapshots_log()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("strategy") == sid:
            out.append(e)
    return out
