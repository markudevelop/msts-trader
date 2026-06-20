"""Idempotency guard: avoid double-trading the same targets in a window.

Records a fingerprint of (broker, account, targets, execution params) per UTC
day. A second unattended run with an IDENTICAL plan the same day is skipped
unless the user passes --force. Prevents a cron misfire or a manual + scheduled
overlap from rebalancing twice.

The fingerprint includes the execution params (allocation, scope, sweep,
threshold, threshold mode, whole-shares, min-weight) — a deliberate second run
with materially different sizing/scope produces a DIFFERENT plan, so it must NOT
be suppressed as a duplicate.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(os.path.expanduser("~/.msts-trader/runstate.json"))


def fingerprint(broker: str, account_id: str, targets, params: dict | None = None) -> str:
    items = sorted((t.ticker, str(t.weight)) for t in targets)
    payload = {"broker": broker, "account": account_id, "targets": items}
    if params:
        # str() every value so Decimals / bools / None hash deterministically.
        payload["params"] = {k: str(v) for k, v in sorted(params.items())}
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def already_done(fp: str) -> bool:
    if not STATE_PATH.exists():
        return False
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception:
        return False
    return data.get("date") == _today() and fp in (data.get("fingerprints") or [])


def record(fp: str) -> None:
    data = {"date": _today(), "fingerprints": []}
    if STATE_PATH.exists():
        try:
            existing = json.loads(STATE_PATH.read_text())
            if existing.get("date") == _today():
                data = existing
        except Exception:
            pass
    fps = set(data.get("fingerprints") or [])
    fps.add(fp)
    data["fingerprints"] = sorted(fps)
    data["date"] = _today()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, indent=2))
