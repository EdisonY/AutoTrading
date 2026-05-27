"""Sync generated reports from Aliyun analysis node back to Tencent live node.

This script runs on Aliyun after analysis tasks complete.
It uploads key reports to Tencent's /opt/crypto-auto-trader/reports/ directory
so the command-center portal shows the latest analysis results.
"""

from __future__ import annotations

import os
import sys
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
ALIYUN_RESEARCH = Path("/opt/crypto-shadow-lab/research_memory")

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
    "market_review_latest.md",
    "market_review_latest.html",
    "alerts_latest.md",
]

RUNTIME_FILES = [
    "strategy_evolution_latest.json",
]

RESEARCH_FILES = [
    "open_items.json",
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


def sync_files(
    client: paramiko.SSHClient,
    local_dir: Path,
    remote_dir: str,
    filenames: list[str],
    label: str,
) -> int:
    """Upload files from local_dir to remote_dir. Returns count of files uploaded."""
    sftp = client.open_sftp()
    uploaded = 0
    try:
        # Ensure remote directory exists
        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            stdin, stdout, stderr = client.exec_command(f"mkdir -p {remote_dir}", timeout=10)
            stdout.channel.recv_exit_status()

        for name in filenames:
            local_path = local_dir / name
            if not local_path.exists():
                print(f"  [SKIP] {label}/{name} - not found locally")
                continue
            remote_path = f"{remote_dir}/{name}"
            try:
                sftp.put(str(local_path), remote_path)
                print(f"  [OK]   {label}/{name} -> {remote_path}")
                uploaded += 1
            except Exception as exc:
                print(f"  [ERR]  {label}/{name} - {exc}")
    finally:
        sftp.close()
    return uploaded


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Sync Aliyun reports to Tencent")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be synced without uploading")
    args = parser.parse_args(argv)

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
        print("--- Syncing reports ---")
        total += sync_files(client, ALIYUN_REPORTS, TENCENT_REPORTS, REPORT_FILES, "reports")
        print()
        print("--- Syncing runtime ---")
        total += sync_files(client, ALIYUN_RUNTIME, TENCENT_RUNTIME, RUNTIME_FILES, "runtime")
        print()
        print("--- Syncing research attention ---")
        total += sync_files(client, ALIYUN_RESEARCH, TENCENT_RESEARCH, RESEARCH_FILES, "research")
        print()
        print(f"Total: {total} files uploaded to Tencent")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
