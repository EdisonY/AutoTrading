"""Sync generated reports from Aliyun analysis node back to Tencent live node.

This script runs on Aliyun after analysis tasks complete.
It uploads key reports to Tencent's /opt/crypto-auto-trader/reports/ directory
so the command-center portal shows the latest analysis results.
"""

from __future__ import annotations

import os
import sys
import base64
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
    "portal_latest.html",
    "index.html",
    "strategy_evolution_latest.json",
    "strategy_evolution_latest.md",
    "strategy_evolution_latest.html",
    "strategy_truth_latest.json",
    "strategy_truth_latest.md",
    "sentinel_quality_latest.json",
    "sentinel_quality_latest.md",
    "counterfactual_open_skips_latest.json",
    "counterfactual_open_skips_latest.md",
    "counterfactual_open_skips_latest.html",
    "research_store_summary_latest.md",
    "alerts_latest.md",
    "market_review_latest.md",
    "market_review_latest.html",
]

RUNTIME_FILES = [
    "strategy_evolution_latest.json",
    "strategy_truth_latest.json",
    "sentinel_quality_latest.json",
    "research_store_summary_latest.json",
]

RESEARCH_FILES = [
    "open_items.json",
]


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


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
    client: paramiko.SSHClient,
    local_dir: Path,
    remote_dir: str,
    filenames: list[str],
    label: str,
    max_bytes: int,
    file_timeout: int,
    method: str,
) -> int:
    """Upload files from local_dir to remote_dir. Returns count of files uploaded."""
    sftp = client.open_sftp() if method == "sftp" else None
    if sftp:
        sftp.get_channel().settimeout(file_timeout)
    uploaded = 0
    try:
        # Ensure remote directory exists
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
                if method == "sftp":
                    assert sftp is not None
                    sftp.put(str(local_path), remote_path)
                else:
                    data = base64.b64encode(local_path.read_bytes())
                    cmd = f"base64 -d > {shell_quote(remote_path)}"
                    stdin, stdout, stderr = client.exec_command(cmd, timeout=file_timeout)
                    stdin.write(data.decode("ascii"))
                    stdin.channel.shutdown_write()
                    rc = stdout.channel.recv_exit_status()
                    if rc != 0:
                        err = stderr.read().decode("utf-8", errors="replace")
                        raise RuntimeError(err or f"remote base64 upload failed rc={rc}")
                print(f"  [OK]   {label}/{name} ({size} bytes) -> {remote_path}")
                uploaded += 1
            except Exception as exc:
                print(f"  [ERR]  {label}/{name} - {exc}")
    finally:
        if sftp:
            sftp.close()
    return uploaded


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sync Aliyun reports to Tencent")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced without uploading")
    parser.add_argument("--max-file-mb", type=float, default=8.0, help="Skip individual files larger than this")
    parser.add_argument("--file-timeout", type=int, default=30, help="Per-file upload timeout in seconds")
    parser.add_argument("--method", choices=["stream", "sftp"], default="stream", help="Upload method; stream uses SSH/base64 and avoids SFTP stalls")
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
        for name in RUNTIME_FILES:
            local = ALIYUN_RUNTIME / name
            status = "EXISTS" if local.exists() else "MISSING"
            print(f"  [{status}] runtime/{name}")
        for name in RESEARCH_FILES:
            local = ALIYUN_RESEARCH / name
            status = "EXISTS" if local.exists() else "MISSING"
            print(f"  [{status}] research_memory/attention/{name}")
        return 0

    client = ssh_client()
    try:
        total = 0
        print("--- Syncing runtime ---")
        total += sync_files(client, ALIYUN_RUNTIME, TENCENT_RUNTIME, RUNTIME_FILES, "runtime", max_bytes, args.file_timeout, args.method)
        print()
        print("--- Syncing research attention ---")
        total += sync_files(client, ALIYUN_RESEARCH, TENCENT_RESEARCH, RESEARCH_FILES, "research", max_bytes, args.file_timeout, args.method)
        print()
        print("--- Syncing reports ---")
        total += sync_files(client, ALIYUN_REPORTS, TENCENT_REPORTS, REPORT_FILES, "reports", max_bytes, args.file_timeout, args.method)
        print()
        print(f"Total: {total} files uploaded to Tencent")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
