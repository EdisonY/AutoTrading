"""Pull a compact live decision context from Tencent without copying databases."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
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
    ("reports/research_store_summary_latest.md", "reports/research_store_summary_latest.md", False),
    ("reports/replay_feature_dataset_latest.md", "reports/replay_feature_dataset_latest.md", False),
    ("reports/replay_gate_audit_latest.md", "reports/replay_gate_audit_latest.md", False),
    ("reports/alerts_latest.md", "reports/alerts_latest.md", False),
    ("reports/market_review_latest.html", "reports/market_review_latest.html", False),
    ("reports/market_review_latest.md", "reports/market_review_latest.md", False),
    ("reports/market_snapshot_latest.json", "reports/market_snapshot_latest.json", False),
    ("复盘报告/account_snapshot_latest.html", "复盘报告/account_snapshot_latest.html", False),
    ("runtime/account_snapshot_latest.json", "runtime/account_snapshot_latest.json", True),
    ("runtime/alerts_latest.json", "runtime/alerts_latest.json", True),
    ("runtime/market_data_cache.json", "runtime/market_data_cache.json", False),
    ("runtime/strategy_evolution_latest.json", "runtime/strategy_evolution_latest.json", True),
    ("runtime/research_store_summary_latest.json", "runtime/research_store_summary_latest.json", False),
    ("runtime/replay_feature_dataset_latest.json", "runtime/replay_feature_dataset_latest.json", False),
    ("runtime/replay_gate_audit_latest.json", "runtime/replay_gate_audit_latest.json", False),
    ("research_memory/attention/open_items.json", "runtime/live_attention_latest.json", True),
]

LARGE_LOCAL_FILES = {
    "reports/market_review_latest.html",
    "reports/market_review_latest.md",
}

REMOTE_SUMMARY_SCRIPT = r"""
import json
import pathlib
import sqlite3
import subprocess
from datetime import datetime

root = pathlib.Path("/opt/crypto-auto-trader")

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
services = {
    name: unit_state(name)
    for name in (
        "crypto-scanner.service",
        "crypto-scanner-v16.service",
        "crypto-scanner-v14.service",
        "crypto-market-mover-sentinel.service",
        "crypto-account-snapshot.service",
        "crypto-system-alerts.service",
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
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(max(5, min(timeout, 15)))
        sock = getattr(transport, "sock", None)
        if sock is not None:
            sock.settimeout(timeout)
    return client


def remote_command(client: paramiko.SSHClient, command: str, timeout: int) -> str:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    channel = stdout.channel
    try:
        channel.settimeout(1.0)
    except Exception:
        pass
    deadline = time.monotonic() + max(1, timeout)
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    while True:
        while channel.recv_ready():
            out_chunks.append(channel.recv(65536))
        while channel.recv_stderr_ready():
            err_chunks.append(channel.recv_stderr(65536))
        if channel.exit_status_ready():
            rc = channel.recv_exit_status()
            break
        if time.monotonic() >= deadline:
            channel.close()
            raise TimeoutError(f"remote command timed out after {timeout}s: {command[:160]}")
        time.sleep(0.1)
    while channel.recv_ready():
        out_chunks.append(channel.recv(65536))
    while channel.recv_stderr_ready():
        err_chunks.append(channel.recv_stderr(65536))
    out = b"".join(out_chunks).decode("utf-8", errors="replace")
    err = b"".join(err_chunks).decode("utf-8", errors="replace")
    if rc != 0:
        raise RuntimeError(f"remote command rc={rc}: {err[-400:] or out[-400:]}")
    return out.strip()


def openssh_args(binary: str, timeout: int) -> list[str]:
    args = [
        binary,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
    ]
    if TENCENT_KEY:
        args.extend(["-i", str(Path(TENCENT_KEY).expanduser())])
    return args


def openssh_remote_command(command: str, timeout: int) -> str:
    target = f"{TENCENT_USER}@{TENCENT_HOST}"
    try:
        result = subprocess.run(
            [*openssh_args("ssh", timeout), target, command],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"openssh remote command timed out after {timeout}s: {command[:160]}") from exc
    out = result.stdout.decode("utf-8", errors="replace")
    err = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"openssh remote command rc={result.returncode}: {err[-400:] or out[-400:]}")
    return out.strip()


def pull_files_openssh(entries: list[tuple[str, str, str, bool]], timeout: int) -> list[dict[str, Any]]:
    encoded = base64.b64encode(json.dumps(entries, ensure_ascii=False).encode("utf-8")).decode("ascii")
    script = (
        "import base64, json, pathlib, sys, tarfile\n"
        f"entries=json.loads(base64.b64decode('{encoded}').decode('utf-8'))\n"
        "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:\n"
        "    for index, (remote_root, remote_relative, _local, _required) in enumerate(entries):\n"
        "        path=pathlib.Path(remote_root) / remote_relative\n"
        "        if path.exists() and path.is_file():\n"
        "            archive.add(str(path), arcname=str(index), recursive=False)\n"
    )
    command = f"python3 - <<'PY'\n{script}PY"
    target = f"{TENCENT_USER}@{TENCENT_HOST}"
    try:
        result = subprocess.run(
            [*openssh_args("ssh", timeout), target, command],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"openssh file pull timed out after {timeout}s for {len(entries)} files") from exc
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"openssh file pull rc={result.returncode}: {err[-400:]}")
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:gz") as archive:
        for member in archive.getmembers():
            stream = archive.extractfile(member)
            if stream is not None:
                members[member.name] = stream.read()
    pulled: list[dict[str, Any]] = []
    for index, (remote_root, remote_relative, local_relative, required) in enumerate(entries):
        remote_path = f"{remote_root}/{remote_relative}"
        content = members.get(str(index))
        if content is None:
            if required:
                raise FileNotFoundError(f"required live file missing: {remote_path}")
            pulled.append({"remote": remote_path, "local": str(ROOT / local_relative), "bytes": 0, "status": "missing"})
            continue
        local_path = ROOT / Path(local_relative)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        pulled.append({"remote": remote_path, "local": str(local_path), "bytes": len(content), "status": "ok"})
    return pulled


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
    ]
    for path in paths:
        if not path.exists() or path.suffix.lower() != ".html":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for remote, local in replacements:
            text = text.replace(remote, local)
        path.write_text(text, encoding="utf-8")


def pull_context(timeout: int) -> dict[str, Any]:
    entries = [
        *((LIVE_ROOT, remote, local, required) for remote, local, required in MAIN_FILES),
    ]
    use_openssh = TENCENT_PASS is None and shutil.which("ssh") is not None
    if use_openssh:
        small_entries = [entry for entry in entries if entry[2] not in LARGE_LOCAL_FILES]
        large_entries = [entry for entry in entries if entry[2] in LARGE_LOCAL_FILES]
        pulled = pull_files_openssh(small_entries, timeout=max(60, timeout))
        if large_entries:
            try:
                pulled.extend(pull_files_openssh(large_entries, timeout=max(90, timeout)))
            except TimeoutError:
                for remote_root, remote_relative, local_relative, required in large_entries:
                    if required:
                        raise
                    pulled.append(
                        {
                            "remote": f"{remote_root}/{remote_relative}",
                            "local": str(ROOT / local_relative),
                            "bytes": 0,
                            "status": "skipped_timeout",
                        }
                    )
        raw_summary = openssh_remote_command(
            f"/opt/crypto-auto-trader/.venv/bin/python - <<'PY'\n{REMOTE_SUMMARY_SCRIPT}\nPY",
            timeout=max(30, timeout),
        )
    else:
        pulled: list[dict[str, Any]] = []
        client = connect(timeout)
        try:
            sftp = client.open_sftp()
            try:
                sftp.get_channel().settimeout(timeout)
            except Exception:
                pass
            try:
                for remote, local, required in MAIN_FILES:
                    pulled.append(pull_file(sftp, LIVE_ROOT, remote, local, required))
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
        "remote_roots": {"live": LIVE_ROOT},
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
    try:
        payload = pull_context(max(5, args.timeout))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
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
