"""Refresh the command-center HTML on a short interval."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
CST = timezone(timedelta(hours=8))
STATUS_PATH = ROOT / "runtime" / "portal_refresh_latest.json"


def refresh_once(timeout: int = 50) -> dict:
    started = time.time()
    attention_proc = subprocess.run(
        [sys.executable, str(ROOT / "decision_attention.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=20,
    )
    proc = subprocess.run(
        [sys.executable, str(ROOT / "portal_dashboard.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    payload = {
        "ts": datetime.now(CST).isoformat(),
        "status": "ok" if proc.returncode == 0 and attention_proc.returncode == 0 else "error",
        "attention_returncode": attention_proc.returncode,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.time() - started, 3),
        "attention_stdout_tail": attention_proc.stdout[-1000:],
        "attention_stderr_tail": attention_proc.stderr[-1000:],
        "stdout_tail": proc.stdout[-1000:],
        "stderr_tail": proc.stderr[-1000:],
    }
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("ts", "status", "elapsed_seconds")}, ensure_ascii=False), flush=True)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="入口页定时刷新服务")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            refresh_once()
        except Exception as exc:
            payload = {"ts": datetime.now(CST).isoformat(), "status": "error", "error": str(exc)}
            STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(payload, ensure_ascii=False), flush=True)
        if args.once:
            return 0
        time.sleep(max(20, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
