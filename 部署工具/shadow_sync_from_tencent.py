"""Read-only log sync from Tencent live server to the shadow lab node."""

from __future__ import annotations

import argparse
import os
import tarfile
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paramiko

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TENCENT_HOST = os.environ.get("TENCENT_HOST", "129.226.151.144")
TENCENT_USER = os.environ.get("TENCENT_USER", "ubuntu")
TENCENT_PASS = os.environ.get("TENCENT_SSH_PASSWORD")
REMOTE_DIR = "/opt/crypto-auto-trader"
CST = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parent
LOCAL_DIR = ROOT / "server_logs_tencent"

JSONL_FILES = [
    "scanner_data/events.jsonl",
    "scanner_data/trades.jsonl",
    "scanner_data_v14/events.jsonl",
    "scanner_data_v14/trades.jsonl",
    "scanner_data_v16/events.jsonl",
    "scanner_data_v16/trades.jsonl",
    "logs/decisions.jsonl",
    "logs/signals.jsonl",
    "logs/operations.jsonl",
    "logs/system.jsonl",
    "logs_v14/decisions.jsonl",
    "logs_v14/signals.jsonl",
    "logs_v14/system.jsonl",
    "logs_v16/decisions.jsonl",
    "logs_v16/signals.jsonl",
    "logs_v16/system.jsonl",
]

TEXT_FILES = [
    "logs/scanner_stdout.log",
    "logs_v14/scanner_stdout.log",
    "logs_v16/stdout.log",
]

REPORT_FILES = [
    "reports/market_snapshot_latest.json",
]


def ssh_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    configured_key = os.environ.get("TENCENT_SSH_KEY")
    default_key = Path.home() / ".ssh" / "autotrading_tencent_sync"
    key_path = Path(configured_key).expanduser() if configured_key else default_key
    key_args = {"key_filename": str(key_path)} if key_path.exists() else {}
    client.connect(
        TENCENT_HOST,
        22,
        TENCENT_USER,
        password=TENCENT_PASS or None,
        timeout=20,
        look_for_keys=True,
        allow_agent=True,
        **key_args,
    )
    return client


def recent_days(days_back: int) -> list[str]:
    today = datetime.now(CST).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in reversed(range(days_back))]


def grep_recent(client: paramiko.SSHClient, rel: str, days: list[str]) -> bytes:
    cmd = f"cd {REMOTE_DIR} && {remote_jsonl_filter_command(rel, days)}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=240)
    data = stdout.read()
    stderr.read()
    return data


def fetch_tail(client: paramiko.SSHClient, rel: str, lines: int = 300) -> bytes:
    cmd = f"cd {REMOTE_DIR} && if [ -f {rel} ]; then tail -n {lines} {rel}; fi"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=60)
    data = stdout.read()
    stderr.read()
    return data


def run_remote(client: paramiko.SSHClient, cmd: str, timeout: int = 300) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return rc, out.strip(), err.strip()


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def jsonl_shard_dir(rel: str) -> str:
    return Path(rel).with_suffix("").as_posix()


def remote_jsonl_filter_command(rel: str, days: list[str], output: str | None = None) -> str:
    patterns = " ".join(f"-e {shell_quote(day)}" for day in days)
    target = f" > {shell_quote(output)}" if output else ""
    fallback = []
    if output:
        fallback.append(f": > {shell_quote(output)}")
    for day in days:
        shard = f"{jsonl_shard_dir(rel)}/{day}.jsonl"
        if output:
            fallback.append(f"if [ -f {shell_quote(shard)} ]; then cat {shell_quote(shard)} >> {shell_quote(output)}; fi")
        else:
            fallback.append(f"if [ -f {shell_quote(shard)} ]; then cat {shell_quote(shard)}; fi")
    fallback_script = "; ".join(fallback)
    return (
        f"if [ -f {shell_quote(rel)} ]; then grep {patterns} {shell_quote(rel)}{target} || true; "
        f"else {fallback_script}; fi"
    )


def sync_bundle(days_back: int, log_tail: int = 300) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    days = recent_days(days_back)
    stamp = int(time.time())
    tmp_dir = f"/tmp/autotrading_shadow_sync_{stamp}"
    archive = f"{tmp_dir}.tgz"

    commands = [
        "set -e",
        f"rm -rf {shell_quote(tmp_dir)} {shell_quote(archive)}",
        f"mkdir -p {shell_quote(tmp_dir)}",
        f"cd {shell_quote(REMOTE_DIR)}",
    ]
    for rel in JSONL_FILES:
        parent = Path(rel).parent.as_posix()
        commands.append(f"mkdir -p {shell_quote(f'{tmp_dir}/{parent}')} ")
        commands.append(remote_jsonl_filter_command(rel, days, f"{tmp_dir}/{rel}"))
    for rel in TEXT_FILES:
        parent = Path(rel).parent.as_posix()
        commands.append(f"mkdir -p {shell_quote(f'{tmp_dir}/{parent}')} ")
        commands.append(
            f"if [ -f {shell_quote(rel)} ]; then tail -n {int(log_tail)} {shell_quote(rel)} > {shell_quote(f'{tmp_dir}/{rel}')} ; fi"
        )
    for rel in REPORT_FILES:
        parent = Path(rel).parent.as_posix()
        commands.append(f"mkdir -p {shell_quote(f'{tmp_dir}/{parent}')} ")
        commands.append(
            f"if [ -f {shell_quote(rel)} ]; then cp {shell_quote(rel)} {shell_quote(f'{tmp_dir}/{rel}')} ; fi"
        )
    commands.extend(
        [
            f"tar -czf {shell_quote(archive)} -C {shell_quote(tmp_dir)} .",
            f"du -h {shell_quote(archive)} | awk '{{print $1}}'",
        ]
    )

    client = ssh_client()
    sftp = client.open_sftp()
    local_archive = LOCAL_DIR / f"_sync_{stamp}.tgz"
    try:
        print(f"Connected to Tencent {TENCENT_HOST}")
        print(f"Fast bundle mode: {', '.join(days)}; log_tail={log_tail}")
        rc, out, err = run_remote(client, " && ".join(commands), timeout=300)
        if rc != 0:
            raise RuntimeError(f"remote bundle failed rc={rc}: {err or out}")
        if out:
            print(f"Remote bundle size: {out.splitlines()[-1]}")
        sftp.get(archive, str(local_archive))
    finally:
        try:
            run_remote(client, f"rm -rf {shell_quote(tmp_dir)} {shell_quote(archive)}", timeout=30)
        except Exception:
            pass
        sftp.close()
        client.close()

    with tarfile.open(local_archive, "r:gz") as tar:
        tar.extractall(LOCAL_DIR)
    local_archive.unlink(missing_ok=True)

    for rel in JSONL_FILES + TEXT_FILES + REPORT_FILES:
        path = LOCAL_DIR / rel
        if not path.exists():
            print(f"[SKIP] {rel}")
            continue
        print(f"[OK] {rel}")


def sync(days_back: int) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    days = recent_days(days_back)
    client = ssh_client()
    try:
        print(f"Connected to Tencent {TENCENT_HOST}; days={','.join(days)}")
        for rel in JSONL_FILES:
            local = LOCAL_DIR / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            data = grep_recent(client, rel, days)
            local.write_bytes(data)
            print(f"[JSONL] {rel}: {data.count(b'\\n')} rows")
        for rel in TEXT_FILES:
            local = LOCAL_DIR / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            data = fetch_tail(client, rel)
            local.write_bytes(data)
            print(f"[LOG] {rel}: {len(data)} bytes")
        for rel in REPORT_FILES:
            local = LOCAL_DIR / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            cmd = f"cd {REMOTE_DIR} && if [ -f {rel} ]; then cat {rel}; fi"
            stdin, stdout, stderr = client.exec_command(cmd, timeout=60)
            data = stdout.read()
            stderr.read()
            local.write_bytes(data)
            print(f"[REPORT] {rel}: {len(data)} bytes")
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Tencent logs into shadow lab")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--full", action="store_true", help="Use the older file-by-file sync mode")
    parser.add_argument("--log-tail", type=int, default=300, help="Lines to keep from large text logs in fast mode")
    args = parser.parse_args(argv)
    if not args.full:
        sync_bundle(args.days, log_tail=args.log_tail)
    else:
        sync(args.days)
    print(f"Local shadow mirror updated at {LOCAL_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
