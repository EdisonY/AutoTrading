"""Sync three-strategy logs from the Tencent Cloud server to the local mirror."""

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

HOST = os.environ.get("TENCENT_HOST", "129.226.151.144")
USER = os.environ.get("TENCENT_USER", "ubuntu")
PASSWORD = os.environ.get("TENCENT_SSH_PASSWORD")
REMOTE_DIR = "/opt/crypto-auto-trader"

ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = ROOT / "server_logs_tencent"

FILES = [
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
    "logs/scanner_stdout.log",
    "logs_v14/decisions.jsonl",
    "logs_v14/signals.jsonl",
    "logs_v14/system.jsonl",
    "logs_v14/scanner_stdout.log",
    "logs_v16/decisions.jsonl",
    "logs_v16/signals.jsonl",
    "logs_v16/system.jsonl",
    "logs_v16/stdout.log",
    "reports/market_snapshot_latest.json",
]

JSONL_FILES = [rel for rel in FILES if rel.endswith(".jsonl")]
TEXT_FILES = [
    "logs/scanner_stdout.log",
    "logs_v14/scanner_stdout.log",
    "logs_v16/stdout.log",
]
REPORT_FILES = [
    "reports/market_snapshot_latest.json",
]

CST = timezone(timedelta(hours=8))


def ssh_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, password=PASSWORD or None, timeout=15, look_for_keys=True, allow_agent=True)
    return client


def remote_tail_filtered(client: paramiko.SSHClient, rel: str, days: list[str], output: Path) -> int:
    cmd = f"cd {REMOTE_DIR} && {remote_jsonl_filter_command(rel, days)}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=180)
    data = stdout.read()
    stderr.read()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    if not data:
        return 0
    return data.count(b"\n")


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
        fallback.append(f": > {shell_quote(output)};")
    for day in days:
        shard = f"{jsonl_shard_dir(rel)}/{day}.jsonl"
        if output:
            fallback.append(f"if [ -f {shell_quote(shard)} ]; then cat {shell_quote(shard)} >> {shell_quote(output)}; fi")
        else:
            fallback.append(f"if [ -f {shell_quote(shard)} ]; then cat {shell_quote(shard)}; fi")
    return (
        f"if [ -f {shell_quote(rel)} ]; then grep {patterns} {shell_quote(rel)}{target} || true; "
        f"else {' '.join(fallback)}; fi"
    )


def sync_bundle(days_back: int, log_tail: int = 800) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(CST).date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_back)]
    stamp = int(time.time())
    tmp_dir = f"/tmp/autotrading_sync_{stamp}"
    archive = f"{tmp_dir}.tgz"

    commands = [
        "set -e",
        f"rm -rf {shell_quote(tmp_dir)} {shell_quote(archive)}",
        f"mkdir -p {shell_quote(tmp_dir)}",
        f"cd {shell_quote(REMOTE_DIR)}",
    ]
    for rel in JSONL_FILES:
        quoted_rel = shell_quote(rel)
        commands.append(
            f"mkdir -p {shell_quote(tmp_dir + '/' + str(Path(rel).parent).replace(chr(92), '/'))}"
        )
        commands.append(remote_jsonl_filter_command(rel, days, tmp_dir + "/" + rel))
    for rel in TEXT_FILES:
        commands.append(
            f"mkdir -p {shell_quote(tmp_dir + '/' + str(Path(rel).parent).replace(chr(92), '/'))}"
        )
        commands.append(
            f"if [ -f {shell_quote(rel)} ]; then tail -n {int(log_tail)} {shell_quote(rel)} > {shell_quote(tmp_dir + '/' + rel)}; fi"
        )
    for rel in REPORT_FILES:
        commands.append(
            f"mkdir -p {shell_quote(tmp_dir + '/' + str(Path(rel).parent).replace(chr(92), '/'))}"
        )
        commands.append(
            f"if [ -f {shell_quote(rel)} ]; then cp {shell_quote(rel)} {shell_quote(tmp_dir + '/' + rel)}; fi"
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
        print(f"Connected to {HOST}")
        print(f"Fast bundle mode: {', '.join(reversed(days))}; log_tail={log_tail}")
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
            print(f"[SKIP] {rel} not found")
            continue
        if rel.endswith(".jsonl"):
            try:
                with path.open("rb") as f:
                    rows = sum(1 for _ in f)
                print(f"[OK] {rel} rows={rows}")
            except Exception:
                print(f"[OK] {rel}")
        else:
            print(f"[OK] {rel} bytes={path.stat().st_size}")


def sync_all(today_only: bool = False, days_back: int | None = None) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    client = ssh_client()
    sftp = client.open_sftp()
    try:
        print(f"Connected to {HOST}")
        days: list[str] | None = None
        if days_back:
            today = datetime.now(CST).date()
            days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_back)]
            print(f"Incremental mode: {', '.join(reversed(days))}")

        for rel in FILES:
            remote = f"{REMOTE_DIR}/{rel}"
            local = LOCAL_DIR / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            if days and rel.endswith(".jsonl"):
                try:
                    rows = remote_tail_filtered(client, rel, days, local)
                    print(f"[OK] {rel} filtered rows={rows}")
                except Exception as exc:
                    print(f"[ERR] {rel}: {exc}")
                continue

            try:
                sftp.stat(remote)
                sftp.get(remote, str(local))
                print(f"[OK] {rel}")
            except FileNotFoundError:
                print(f"[SKIP] {rel} not found")
                continue
            except Exception as exc:
                print(f"[ERR] {rel}: {exc}")
                continue

            if today_only and local.suffix == ".jsonl":
                import json

                today = datetime.now().strftime("%Y-%m-%d")
                filtered: list[str] = []
                with local.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        ts = str(row.get("ts") or row.get("time") or row.get("exit_time") or "")
                        if ts.startswith(today):
                            filtered.append(line)
                local.write_text("".join(filtered), encoding="utf-8")
                print(f"    filtered today rows: {len(filtered)}")
    finally:
        sftp.close()
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Tencent Cloud logs to local mirror")
    parser.add_argument("--today", action="store_true", help="Keep only today's JSONL rows")
    parser.add_argument("--days", type=int, default=None, help="Pull only JSONL rows matching recent N dates")
    parser.add_argument("--full", action="store_true", help="Use the older file-by-file SFTP mode")
    parser.add_argument("--log-tail", type=int, default=800, help="Lines to keep from large text logs in fast mode")
    args = parser.parse_args(argv)
    if args.days and not args.full:
        sync_bundle(args.days, log_tail=args.log_tail)
    else:
        sync_all(today_only=args.today, days_back=args.days)
    print(f"Local mirror updated at {LOCAL_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
