"""Run scheduled strategies without Studio open: one OS task.

Installs a single recurring task that runs `msts-trader strategy run-due`
every minute. That command does exactly what Studio's in-process scheduler
does (same `due()` rules, once per rebalance period, New York clock) under a
shared lock, so the OS task and an open Studio can never double-trade.

  Windows       Task Scheduler (schtasks), current user, runs while logged on;
                uses pythonw.exe when present so no console window flashes.
  macOS/Linux   a crontab line tagged `# msts-trader-strategies`.

Nothing is installed unless the user asks (Studio's Install button or
`msts-trader strategy schedule install`).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

TASK_NAME = "msts-trader-strategies"
MARKER = f"# {TASK_NAME}"
# Every minute: a strategy's window is schedule time -> close (10 min at the
# default 15:50 ET), so a 1-minute check runs it on time and can't miss a
# short window. `run-due` exits in well under a second when nothing is due.
EVERY_MINUTES = 1


class ScheduleError(RuntimeError):
    pass


def _windows() -> bool:
    """One seam for the platform choice (tests patch this, never os.name)."""
    return os.name == "nt"


def _python() -> str:
    exe = Path(sys.executable)
    if _windows():
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def command() -> list[str]:
    return [_python(), "-m", "msts_trader", "strategy", "run-due"]


def _run(args: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, input=input)


# ── Windows ────────────────────────────────────────────────────────────────
def _win_tr() -> str:
    return " ".join(f'"{a}"' if " " in a or a.endswith(".exe") else a for a in command())


def _win_status() -> dict:
    r = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
    out = {"installed": r.returncode == 0, "detail": (r.stdout or r.stderr).strip()[:400], "installed_every": None}
    if out["installed"]:
        # The XML export is locale-independent (the LIST view is translated).
        x = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
        m = re.search(r"<Interval>PT(?:(\d+)H)?(?:(\d+)M)?</Interval>", x.stdout or "")
        if m:
            out["installed_every"] = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return out


def _win_install() -> None:
    tr = _win_tr()
    if len(tr) > 261:
        raise ScheduleError("the command line is too long for Task Scheduler (install msts-trader to a shorter path)")
    r = _run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(EVERY_MINUTES), "/TN", TASK_NAME, "/TR", tr])
    if r.returncode != 0:
        raise ScheduleError((r.stderr or r.stdout).strip() or "schtasks /Create failed")


def _win_uninstall() -> None:
    r = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    if r.returncode != 0 and "cannot find" not in (r.stderr + r.stdout).lower():
        raise ScheduleError((r.stderr or r.stdout).strip() or "schtasks /Delete failed")


# ── cron (macOS / Linux) ───────────────────────────────────────────────────
def _cron_line() -> str:
    minute = "*" if EVERY_MINUTES == 1 else f"*/{EVERY_MINUTES}"
    return f"{minute} * * * * {shlex.join(command())} >/dev/null 2>&1 {MARKER}"


def _crontab() -> str:
    r = _run(["crontab", "-l"])
    return r.stdout if r.returncode == 0 else ""


def _cron_write(text: str) -> None:
    r = _run(["crontab", "-"], input=text)
    if r.returncode != 0:
        raise ScheduleError((r.stderr or r.stdout).strip() or "crontab update failed")


def _cron_status() -> dict:
    lines = [ln for ln in _crontab().splitlines() if ln.rstrip().endswith(MARKER)]
    every = None
    if lines:
        field = lines[0].split()[0]
        every = 1 if field == "*" else int(field[2:]) if re.fullmatch(r"\*/\d+", field) else None
    return {"installed": bool(lines), "detail": lines[0] if lines else "", "installed_every": every}


def _cron_install() -> None:
    keep = [ln for ln in _crontab().splitlines() if not ln.rstrip().endswith(MARKER)]
    _cron_write("\n".join([*keep, _cron_line()]) + "\n")


def _cron_uninstall() -> None:
    keep = [ln for ln in _crontab().splitlines() if not ln.rstrip().endswith(MARKER)]
    _cron_write(("\n".join(keep) + "\n") if keep else "")


# ── public ─────────────────────────────────────────────────────────────────
def status() -> dict:
    try:
        st = _win_status() if _windows() else _cron_status()
    except FileNotFoundError:
        return {"supported": False, "installed": False, "platform": sys.platform, "detail": "no task scheduler found"}
    return {
        "supported": True,
        "installed": st["installed"],
        "platform": "windows" if _windows() else "cron",
        "every_minutes": EVERY_MINUTES,
        "installed_every_minutes": st.get("installed_every"),
        # an install from an older version (e.g. every 5 min): reinstall to update
        "outdated": bool(st["installed"] and st.get("installed_every") not in (None, EVERY_MINUTES)),
        "command": " ".join(command()),
        "detail": st["detail"],
    }


def install() -> dict:
    (_win_install if _windows() else _cron_install)()
    return status()


def uninstall() -> dict:
    (_win_uninstall if _windows() else _cron_uninstall)()
    return status()
