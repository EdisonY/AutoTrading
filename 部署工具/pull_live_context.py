"""Pull a compact live decision context from Tencent without copying databases."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import paramiko

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
TENCENT_HOST = os.environ.get("TENCENT_HOST", "129.226.151.144")
TENCENT_USER = os.environ.get("TENCENT_USER", "ubuntu")
TENCENT_PASS = os.environ.get("TENCENT_SSH_PASSWORD")
TENCENT_KEY = os.environ.get("TENCENT_SSH_KEY")
LIVE_ROOT = "/opt/crypto-auto-trader"
POLYMARKET_ROOT = "/opt/polymarket-lab"
CST = timezone(timedelta(hours=8))

MAIN_FILES = [
    ("reports/index.html", "reports/index.html", True),
    ("reports/portal_latest.html", "reports/portal_latest.html", False),
    ("reports/decision_attention_latest.html", "reports/decision_attention_latest.html", False),
    ("reports/decision_attention_latest.md", "reports/decision_attention_latest.md", False),
    ("reports/strategy_evolution_latest.html", "reports/strategy_evolution_latest.html", False),
    ("reports/strategy_evolution_latest.json", "reports/strategy_evolution_latest.json", False),
    ("reports/strategy_evolution_latest.md", "reports/strategy_evolution_latest.md", False),
    ("reports/counterfactual_open_skips_latest.html", "reports/counterfactual_open_skips_latest.html", False),
    ("reports/counterfactual_open_skips_latest.json", "reports/counterfactual_open_skips_latest.json", False),
    ("reports/counterfactual_open_skips_latest.md", "reports/counterfactual_open_skips_latest.md", False),
    ("reports/alerts_latest.md", "reports/alerts_latest.md", False),
    ("复盘报告/account_snapshot_latest.html", "复盘报告/account_snapshot_latest.html", False),
    ("runtime/account_snapshot_latest.json", "runtime/account_snapshot_latest.json", True),
    ("runtime/alerts_latest.json", "runtime/alerts_latest.json", True),
    ("runtime/market_data_cache.json", "runtime/market_data_cache.json", False),
    ("runtime/strategy_evolution_latest.json", "runtime/strategy_evolution_latest.json", True),
    ("research_memory/attention/open_items.json", "runtime/live_attention_latest.json", True),
]

POLYMARKET_FILES = [
    ("reports/polymarket_probe_latest.json", "polymarket_lab/reports/polymarket_probe_latest.json", True),
    ("reports/polymarket_probe_latest.html", "polymarket_lab/reports/polymarket_probe_latest.html", False),
    ("reports/polymarket_probe_latest.md", "polymarket_lab/reports/polymarket_probe_latest.md", False),
    ("reports/polymarket_monitor_summary.jsonl", "polymarket_lab/reports/polymarket_monitor_summary.jsonl", False),
]

REMOTE_SUMMARY_SCRIPT = r"""
import json
import pathlib
import sqlite3
import subprocess
from datetime import datetime

root = pathlib.Path("/opt/crypto-auto-trader")
poly_root = pathlib.Path("/opt/polymarket-lab")

def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}

def unit_state(name):
    result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
    return (result.stdout.strip() or result.stderr.strip() or "unknown")

strategies = {}
db = root / "runtime" / "event_store.sqlite3"
if db.exists():
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    for name in ("A/v11", "B/v16", "C/v14"):
        opened = con.execute(
            "select ts,symbol,side from events where strategy=? and source like ? "
            "and (event_type=? or category=?) order by id desc limit 1",
            (name, "%/decisions", "OPEN", "opened"),
        ).fetchone()
        activity = con.execute(
            "select ts,event_type,category from events where strategy=? order by id desc limit 1",
            (name,),
        ).fetchone()
        strategies[name] = {
            "latest_open": dict(opened) if opened else {},
            "latest_activity": dict(activity) if activity else {},
        }
    con.close()

account = read_json(root / "runtime" / "account_snapshot_latest.json")
alerts = read_json(root / "runtime" / "alerts_latest.json")
attention = read_json(root / "research_memory" / "attention" / "open_items.json")
evolution = read_json(root / "runtime" / "strategy_evolution_latest.json")
poly = read_json(poly_root / "reports" / "polymarket_probe_latest.json")

services = {
    name: unit_state(name)
    for name in (
        "crypto-scanner.service",
        "crypto-scanner-v16.service",
        "crypto-scanner-v14.service",
        "crypto-market-mover-sentinel.service",
        "crypto-account-snapshot.service",
        "crypto-portal-refresh.service",
        "crypto-system-alerts.service",
        "polymarket-monitor.service",
    )
}

print(json.dumps({
    "server_time": datetime.now().astimezone().isoformat(),
    "services": services,
    "strategies": strategies,
    "account_summary": account.get("summary", {}),
    "alert_summary": {
        "ts": alerts.get("ts"),
        "status": alerts.get("status"),
        "alert_count": alerts.get("alert_count"),
        "alerts": alerts.get("alerts", [])[:5],
    },
    "attention_summary": attention.get("summary", {}),
    "attention_items": attention.get("items", [])[:8],
    "evolution_summary": evolution.get("summary", {}),
    "polymarket_summary": {
        "generated_at": poly.get("generated_at"),
        "health": (poly.get("health") or {}).get("ok"),
        "markets_checked": poly.get("markets_checked"),
        "opportunity_count": poly.get("opportunity_count"),
        "book_errors": poly.get("book_errors"),
        "near_misses": (poly.get("near_misses") or [])[:3],
    },
}, ensure_ascii=False))
"""


def connect(timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict[str, Any] = {}
    if TENCENT_KEY:
        kwargs["key_filename"] = str(Path(TENCENT_KEY).expanduser())
    client.connect(
        TENCENT_HOST,
        22,
        TENCENT_USER,
        password=TENCENT_PASS or None,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=True,
        allow_agent=True,
        **kwargs,
    )
    return client


def remote_command(client: paramiko.SSHClient, command: str, timeout: int) -> str:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if rc != 0:
        raise RuntimeError(f"remote command rc={rc}: {err[-400:] or out[-400:]}")
    return out.strip()


def pull_file(
    sftp: paramiko.SFTPClient,
    remote_root: str,
    remote_relative: str,
    local_relative: str,
    required: bool,
) -> dict[str, Any]:
    remote_path = f"{remote_root}/{remote_relative}"
    local_path = ROOT / Path(local_relative)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = sftp.stat(remote_path)
        sftp.get(remote_path, str(local_path))
        return {"remote": remote_path, "local": str(local_path), "bytes": int(info.st_size), "status": "ok"}
    except IOError as exc:
        if required:
            raise FileNotFoundError(f"required live file missing: {remote_path}") from exc
        return {"remote": remote_path, "local": str(local_path), "bytes": 0, "status": "missing"}


def localize_html_links(paths: list[Path]) -> None:
    replacements = [
        ("file:///opt/crypto-auto-trader/reports/", (ROOT / "reports").resolve().as_uri() + "/"),
        ("file:///opt/crypto-auto-trader/%E5%A4%8D%E7%9B%98%E6%8A%A5%E5%91%8A/", (ROOT / "复盘报告").resolve().as_uri() + "/"),
        ("file:///opt/polymarket-lab/reports/", (ROOT / "polymarket_lab" / "reports").resolve().as_uri() + "/"),
    ]
    for path in paths:
        if not path.exists() or path.suffix.lower() != ".html":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for remote, local in replacements:
            text = text.replace(remote, local)
        path.write_text(text, encoding="utf-8")


def pull_context(timeout: int) -> dict[str, Any]:
    pulled: list[dict[str, Any]] = []
    client = connect(timeout)
    try:
        sftp = client.open_sftp()
        try:
            for remote, local, required in MAIN_FILES:
                pulled.append(pull_file(sftp, LIVE_ROOT, remote, local, required))
            for remote, local, required in POLYMARKET_FILES:
                pulled.append(pull_file(sftp, POLYMARKET_ROOT, remote, local, required))
        finally:
            sftp.close()
        raw_summary = remote_command(
            client,
            f"/opt/crypto-auto-trader/.venv/bin/python - <<'PY'\n{REMOTE_SUMMARY_SCRIPT}\nPY",
            timeout=max(20, timeout),
        )
    finally:
        client.close()
    summary = json.loads(raw_summary)
    localize_html_links([ROOT / item["local"] if not Path(item["local"]).is_absolute() else Path(item["local"]) for item in pulled])
    payload = {
        "pulled_at": datetime.now(CST).isoformat(),
        "host": TENCENT_HOST,
        "remote_roots": {"live": LIVE_ROOT, "polymarket": POLYMARKET_ROOT},
        "files": pulled,
        "live_summary": summary,
    }
    out = ROOT / "runtime" / "live_context_summary_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def sync_logs(days: int, log_tail: int, timeout: int) -> None:
    command = [
        sys.executable,
        str(SCRIPT_DIR / "sync_tencent_logs.py"),
        "--days",
        str(days),
        "--log-tail",
        str(log_tail),
    ]
    subprocess.run(command, cwd=str(ROOT), check=True, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull compact live context for a fresh workspace")
    parser.add_argument("--timeout", type=int, default=20, help="SSH/summary timeout seconds")
    parser.add_argument("--logs-days", type=int, default=0, help="Also pull compact strategy log mirror for N recent days")
    parser.add_argument("--log-tail", type=int, default=800, help="Text log lines to keep when --logs-days is used")
    parser.add_argument("--logs-timeout", type=int, default=300, help="Maximum seconds for optional log mirror pull")
    args = parser.parse_args(argv)
    payload = pull_context(max(5, args.timeout))
    if args.logs_days > 0:
        sync_logs(args.logs_days, args.log_tail, args.logs_timeout)
    live = payload["live_summary"]
    accounts = live.get("account_summary") or {}
    print(
        json.dumps(
            {
                "pulled_at": payload["pulled_at"],
                "files_ok": sum(1 for item in payload["files"] if item["status"] == "ok"),
                "services": live.get("services"),
                "account_upnl": accounts.get("unrealized_pnl_usdt"),
                "positions": accounts.get("open_positions"),
                "attention": live.get("attention_summary"),
                "output": str(ROOT / "runtime" / "live_context_summary_latest.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
