"""
Process controller for the local skip-trace runner, driven by the /admin/skiptrace
control panel. Manages ONE background run at a time:

  start()      -> launch `python -m scrapers.skiptrace_run ...` as a child process
  stop_after() -> ask it to finish the in-flight lead, then halt cleanly ("Kill After")
  kill_now()   -> terminate the process tree immediately ("Kill ASAP")
  status()     -> read the runner's progress file + whether it's still alive

Safety by design: pace/break presets only ever make it SLOWER, never faster.
Runs locally only (headed browser appears on the user's desktop).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONTROL = DATA / ".skiptrace_control.json"
STATUS = DATA / ".skiptrace_status.json"
PIDFILE = DATA / ".skiptrace_pid"
LOG = DATA / "skiptrace_control.log"

# Pace presets: (gap_min, gap_max) seconds between DuckDuckGo searches. Slower =
# lower chance of tripping DDG's burst rate-limit. None faster than the safe default.
PACE_PRESETS = {
    "normal": (4, 9),
    "slower": (10, 20),
    "safest": (20, 40),
}
# Break presets: (break_every_leads, break_min_s, break_max_s).
BREAK_PRESETS = {
    "normal": (25, 180, 420),
    "frequent": (15, 300, 600),
}


def _read_pid() -> int | None:
    try:
        return int(PIDFILE.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10)
            return str(pid) in out.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_running() -> bool:
    pid = _read_pid()
    return bool(pid and _pid_alive(pid))


def start(county: str = "", limit: int = 0, pace: str = "normal", breaks: str = "normal",
          skip_traced: bool = True, write: bool = True, engine: str = "ddg") -> tuple[bool, str]:
    if is_running():
        return False, "A run is already in progress."
    if not county:
        return False, "Pick a county first."
    if engine not in ("ddg", "google", "combo"):
        engine = "ddg"

    gmin, gmax = PACE_PRESETS.get(pace, PACE_PRESETS["normal"])
    bev, bmin, bmax = BREAK_PRESETS.get(breaks, BREAK_PRESETS["normal"])

    DATA.mkdir(parents=True, exist_ok=True)
    CONTROL.write_text(json.dumps({"action": ""}), encoding="utf-8")  # clear any prior stop
    STATUS.write_text(json.dumps({"state": "starting", "county": county}), encoding="utf-8")

    cmd = [
        sys.executable, "-m", "scrapers.skiptrace_run",
        "--county", county, "--engine", engine,
        "--gap-min", str(gmin), "--gap-max", str(gmax),
        "--break-every", str(bev), "--break-min", str(bmin), "--break-max", str(bmax),
        "--control-file", str(CONTROL), "--status-file", str(STATUS),
    ]
    if limit:
        cmd += ["--limit", str(int(limit))]
    if not skip_traced:
        cmd += ["--all"]
    if not write:
        cmd += ["--no-write"]

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    logf = open(LOG, "w", encoding="utf-8")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
                            env=env, creationflags=creationflags)
    PIDFILE.write_text(str(proc.pid), encoding="utf-8")
    return True, f"Started {county} (engine={engine}, pace={pace}, breaks={breaks})."


def stop_after() -> tuple[bool, str]:
    """Kill After: let the current lead finish, then halt cleanly."""
    if not is_running():
        return False, "Nothing is running."
    CONTROL.write_text(json.dumps({"action": "stop_after"}), encoding="utf-8")
    return True, "Will stop after the current lead finishes."


def kill_now() -> tuple[bool, str]:
    """Kill ASAP: terminate the runner and its browser children immediately."""
    pid = _read_pid()
    if not (pid and _pid_alive(pid)):
        CONTROL.write_text(json.dumps({"action": ""}), encoding="utf-8")
        return False, "Nothing is running."
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception as exc:
        return False, f"Kill failed: {type(exc).__name__}: {exc}"
    CONTROL.write_text(json.dumps({"action": ""}), encoding="utf-8")
    return True, "Killed."


def status() -> dict:
    try:
        s = json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        s = {"state": "idle"}
    s["running"] = is_running()
    return s
