"""Sync generated reports from Aliyun analysis node back to Tencent live node.

This script runs on Aliyun after analysis tasks complete.
It uploads key reports to Tencent's /opt/crypto-auto-trader/reports/ directory
so the command-center portal shows the latest analysis results.
"""

from __future__ import annotations

import os
import sys
import base64
import subprocess
from pathlib import Path

import paramiko

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ALIYUN_REPORTS = Path("/opt/crypto-shadow-lab/reports")
ALIYUN_RUNTIME = Path("/opt/crypto-shadow-lab/runtime")
ALIYUN_RESEARCH = Path("/opt/crypto-shadow-lab/research_memory/attention")

TENCENT_HOST = os.environ.get("TENCENT_HOST", "129.226.151.144")
TENCENT_USER = os.environ.get("TENCENT_USER", "ubuntu")
TENCENT_PASS = os.environ.get("TENCENT_SSH_PASSWORD")
TENCENT_REPORTS = "/opt/crypto-auto-trader/reports"
TENCENT_RUNTIME = "/opt/crypto-auto-trader/runtime"
TENCENT_RESEARCH = "/opt/crypto-auto-trader/research_memory/attention"

# Reports generated on Aliyun that should be synced to Tencent
REPORT_FILES = [
    "index.html",
    "counterfactual_open_skips_latest.json",
    "counterfactual_open_skips_latest.md",
    "counterfactual_open_skips_latest.html",
    "research_store_summary_latest.md",
    "research_kline_backfill_latest.md",
    "research_depth_backfill_latest.md",
    "replay_feature_dataset_latest.md",
    "replay_gate_audit_latest.md",
    "replay_live_parity_latest.md",
    "replay_readiness_latest.md",
    "rollback_watch_review_latest.md",
    "a_v11_rollout_review_latest.md",
    "b_v16_rollout_review_latest.md",
    "portal_latest.html",
    "strategy_evolution_latest.json",
    "strategy_evolution_latest.md",
    "strategy_evolution_latest.html",
    "strategy_truth_latest.json",
    "strategy_truth_latest.md",
    "sentinel_quality_latest.json",
    "sentinel_quality_latest.md",
    "alerts_latest.md",
]

OPTIONAL_REPORT_FILES = [
    "market_review_latest.md",
    "market_review_latest.html",
]

RUNTIME_FILES = [
    "strategy_evolution_latest.json",
    "strategy_truth_latest.json",
    "sentinel_quality_latest.json",
    "research_store_summary_latest.json",
    "research_kline_backfill_latest.json",
    "research_depth_backfill_latest.json",
    "replay_feature_dataset_latest.json",
    "replay_gate_audit_latest.json",
    "replay_live_parity_latest.json",
    "replay_readiness_latest.json",
    "rollback_watch_review_latest.json",
    "a_v11_rollout_review_latest.json",
    "b_v16_rollout_review_latest.json",
]

RESEARCH_FILES = [
    "open_items.json",
]


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def upload_with_system_ssh(local_path: Path, remote_path: str, file_timeout: int) -> None:
    data = base64.b64encode(local_path.read_bytes())
    remote_dir = remote_path.rsplit("/", 1)[0]
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        f"{TENCENT_USER}@{TENCENT_HOST}",
        f"mkdir -p {shell_quote(remote_dir)} && base64 -d > {shell_quote(remote_path)}",
    ]
    proc = subprocess.run(cmd, input=data, capture_output=True, timeout=file_timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).decode("utf-8", errors="replace")
        raise RuntimeError(err[-1200:] or f"ssh upload failed rc={proc.returncode}")


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


def sync_files(
    client: paramiko.SSHClient | None,
    local_dir: Path,
    remote_dir: str,
    filenames: list[str],
    label: str,
    max_bytes: int,
    file_timeout: int,
    method: str,
    max_errors: int,
    retries: int,
) -> int:
    """Upload files from local_dir to remote_dir. Returns count of files uploaded."""
    sftp = client.open_sftp() if client and method == "sftp" else None
    if sftp:
        sftp.get_channel().settimeout(file_timeout)
    uploaded = 0
    errors = 0
    try:
        # Ensure remote directory exists
        if client:
            stdin, stdout, stderr = client.exec_command(f"mkdir -p {shell_quote(remote_dir)}", timeout=10)
            stdout.channel.recv_exit_status()

        for name in filenames:
            local_path = local_dir / name
            if not local_path.exists():
                print(f"  [SKIP] {label}/{name} - not found locally")
                continue
            size = local_path.stat().st_size
            if size > max_bytes:
                print(f"  [SKIP] {label}/{name} - {size} bytes exceeds max {max_bytes}")
                continue
            remote_path = f"{remote_dir}/{name}"
            try:
                for attempt in range(1, retries + 2):
                    try:
                        if method == "sftp":
                            assert sftp is not None
                            sftp.put(str(local_path), remote_path)
                        elif method == "ssh":
                            upload_with_system_ssh(local_path, remote_path, file_timeout)
                        else:
                            assert client is not None
                            data = base64.b64encode(local_path.read_bytes())
                            cmd = f"base64 -d > {shell_quote(remote_path)}"
                            stdin, stdout, stderr = client.exec_command(cmd, timeout=file_timeout)
                            stdin.write(data.decode("ascii"))
                            stdin.channel.shutdown_write()
                            stdin.close()
                            rc = stdout.channel.recv_exit_status()
                            if rc != 0:
                                err = stderr.read().decode("utf-8", errors="replace")
                                raise RuntimeError(err or f"remote base64 upload failed rc={rc}")
                        break
                    except Exception:
                        if attempt > retries:
                            raise
                        print(f"  [RETRY] {label}/{name} attempt {attempt + 1}")
                print(f"  [OK]   {label}/{name} ({size} bytes) -> {remote_path}")
                uploaded += 1
            except Exception as exc:
                print(f"  [ERR]  {label}/{name} - {exc}")
                errors += 1
                if errors >= max_errors:
                    print(f"  [STOP] {label} reached {max_errors} upload errors")
                    break
    finally:
        if sftp:
            sftp.close()
    return uploaded


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sync Aliyun reports to Tencent")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced without uploading")
    parser.add_argument("--max-file-mb", type=float, default=8.0, help="Skip individual files larger than this")
    parser.add_argument("--file-timeout", type=int, default=12, help="Per-file upload timeout in seconds")
    parser.add_argument("--method", choices=["ssh", "stream", "sftp"], default="ssh", help="Upload method; ssh uses one OpenSSH/base64 upload per file")
    parser.add_argument("--max-errors", type=int, default=2, help="Stop a section after this many upload errors")
    parser.add_argument("--include-optional", action="store_true", help="Also sync bulky/detail reports such as market review")
    parser.add_argument("--retries", type=int, default=1, help="Retry each file this many times after a timeout/error")
    args = parser.parse_args(argv)
    max_bytes = int(args.max_file_mb * 1024 * 1024)

    print(f"Aliyun reports dir: {ALIYUN_REPORTS}")
    print(f"Tencent target: {TENCENT_HOST}:{TENCENT_REPORTS}")
    print()

    if args.dry_run:
        print("[DRY RUN] Would sync:")
        for name in REPORT_FILES:
            local = ALIYUN_REPORTS / name
            status = "EXISTS" if local.exists() else "MISSING"
            print(f"  [{status}] reports/{name}")
        if args.include_optional:
            for name in OPTIONAL_REPORT_FILES:
                local = ALIYUN_REPORTS / name
                status = "EXISTS" if local.exists() else "MISSING"
                print(f"  [{status}] reports/{name}")
        for name in RUNTIME_FILES:
            local = ALIYUN_RUNTIME / name
            status = "EXISTS" if local.exists() else "MISSING"
            print(f"  [{status}] runtime/{name}")
        for name in RESEARCH_FILES:
            local = ALIYUN_RESEARCH / name
            status = "EXISTS" if local.exists() else "MISSING"
            print(f"  [{status}] research_memory/attention/{name}")
        return 0

    client = None if args.method == "ssh" else ssh_client()
    try:
        total = 0
        print("--- Syncing runtime ---")
        total += sync_files(client, ALIYUN_RUNTIME, TENCENT_RUNTIME, RUNTIME_FILES, "runtime", max_bytes, args.file_timeout, args.method, args.max_errors, args.retries)
        print()
        print("--- Syncing reports ---")
        report_files = REPORT_FILES + (OPTIONAL_REPORT_FILES if args.include_optional else [])
        total += sync_files(client, ALIYUN_REPORTS, TENCENT_REPORTS, report_files, "reports", max_bytes, args.file_timeout, args.method, args.max_errors, args.retries)
        print()
        print("--- Syncing research attention ---")
        total += sync_files(client, ALIYUN_RESEARCH, TENCENT_RESEARCH, RESEARCH_FILES, "research", max_bytes, args.file_timeout, args.method, args.max_errors, args.retries)
        print()
        print(f"Total: {total} files uploaded to Tencent")
        return 0
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
