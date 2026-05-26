"""Deploy review/config/paper-broker optimization files to Tencent SG."""

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

HOST = os.environ.get("TENCENT_HOST", "129.226.151.144")
USER = os.environ.get("TENCENT_USER", "ubuntu")
PASS = os.environ.get("TENCENT_SSH_PASSWORD")
REMOTE_DIR = "/opt/crypto-auto-trader"
PYTHON = f"{REMOTE_DIR}/.venv/bin/python"

ROOT = Path(__file__).resolve().parents[1]

UPLOADS = [
    (ROOT / "core" / "__init__.py", f"{REMOTE_DIR}/core/__init__.py"),
    (ROOT / "core" / "models.py", f"{REMOTE_DIR}/core/models.py"),
    (ROOT / "core" / "strategy_config.py", f"{REMOTE_DIR}/core/strategy_config.py"),
    (ROOT / "core" / "review_analytics.py", f"{REMOTE_DIR}/core/review_analytics.py"),
    (ROOT / "core" / "paper_broker.py", f"{REMOTE_DIR}/core/paper_broker.py"),
    (ROOT / "config" / "README.md", f"{REMOTE_DIR}/config/README.md"),
    (ROOT / "config" / "v11.toml", f"{REMOTE_DIR}/config/v11.toml"),
    (ROOT / "config" / "v14.toml", f"{REMOTE_DIR}/config/v14.toml"),
    (ROOT / "config" / "v16.toml", f"{REMOTE_DIR}/config/v16.toml"),
    (ROOT / "部署工具" / "daily_market_review.py", f"{REMOTE_DIR}/daily_market_review.py"),
    (ROOT / "部署工具" / "account_snapshot_html.py", f"{REMOTE_DIR}/account_snapshot_html.py"),
    (ROOT / "部署工具" / "decision_attention.py", f"{REMOTE_DIR}/decision_attention.py"),
    (ROOT / "部署工具" / "portal_dashboard.py", f"{REMOTE_DIR}/portal_dashboard.py"),
    (ROOT / "部署工具" / "apply_research_approval.py", f"{REMOTE_DIR}/apply_research_approval.py"),
]


def ssh() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, password=PASS or None, timeout=20, look_for_keys=True, allow_agent=True)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[str, str, int]:
    print(f">> {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out.strip():
        print(out.strip()[-1200:])
    if rc != 0 and err.strip():
        print(err.strip()[-1200:])
    return out, err, rc


def ensure_remote_dirs(client: paramiko.SSHClient) -> None:
    dirs = sorted({os.path.dirname(remote) for _, remote in UPLOADS})
    run(client, "mkdir -p " + " ".join(dirs + [f"{REMOTE_DIR}/reports"]))


def upload_files(client: paramiko.SSHClient) -> None:
    sftp = client.open_sftp()
    try:
        for local, remote in UPLOADS:
            if not local.exists():
                raise FileNotFoundError(local)
            sftp.put(str(local), remote)
            print(f"OK upload {local} -> {remote}")
    finally:
        sftp.close()


def install_market_review_timer(client: paramiko.SSHClient) -> None:
    script = f"""#!/bin/bash
set -euo pipefail
cd {REMOTE_DIR}
export PYTHONIOENCODING=utf-8
{PYTHON} daily_market_review.py --top 30 --limit-symbols 220
"""
    service = f"""[Unit]
Description=Crypto Daily Market Review Snapshot
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={REMOTE_DIR}
ExecStart=/bin/bash {REMOTE_DIR}/run_daily_market_review.sh
"""
    timer = """[Unit]
Description=Daily Crypto Market Review Snapshot Timer

[Timer]
OnCalendar=*-*-* 02:05:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    sftp = client.open_sftp()
    try:
        with sftp.open(f"{REMOTE_DIR}/run_daily_market_review.sh", "w") as f:
            f.write(script)
        with sftp.open(f"{REMOTE_DIR}/crypto-market-review.service", "w") as f:
            f.write(service)
        with sftp.open(f"{REMOTE_DIR}/crypto-market-review.timer", "w") as f:
            f.write(timer)
    finally:
        sftp.close()
    run(client, f"chmod +x {REMOTE_DIR}/run_daily_market_review.sh")
    run(client, f"sudo mv {REMOTE_DIR}/crypto-market-review.service /etc/systemd/system/crypto-market-review.service")
    run(client, f"sudo mv {REMOTE_DIR}/crypto-market-review.timer /etc/systemd/system/crypto-market-review.timer")
    run(client, "sudo systemctl daemon-reload")
    run(client, "sudo systemctl enable crypto-market-review.timer")
    run(client, "sudo systemctl restart crypto-market-review.timer")


def main() -> int:
    client = ssh()
    try:
        ensure_remote_dirs(client)
        upload_files(client)
        files = " ".join(
            [
                f"{REMOTE_DIR}/core/__init__.py",
                f"{REMOTE_DIR}/core/models.py",
                f"{REMOTE_DIR}/core/strategy_config.py",
                f"{REMOTE_DIR}/core/review_analytics.py",
                f"{REMOTE_DIR}/core/paper_broker.py",
                f"{REMOTE_DIR}/daily_market_review.py",
                f"{REMOTE_DIR}/account_snapshot_html.py",
                f"{REMOTE_DIR}/decision_attention.py",
                f"{REMOTE_DIR}/portal_dashboard.py",
                f"{REMOTE_DIR}/apply_research_approval.py",
            ]
        )
        _, _, rc = run(client, f"cd {REMOTE_DIR} && {PYTHON} -m py_compile {files}", timeout=120)
        if rc != 0:
            return rc
        install_market_review_timer(client)
        _, _, rc = run(
            client,
            f"cd {REMOTE_DIR} && PYTHONIOENCODING=utf-8 {PYTHON} daily_market_review.py --top 30 --limit-symbols 220",
            timeout=600,
        )
        if rc != 0:
            return rc
        _, _, rc = run(
            client,
            f"ls -lh {REMOTE_DIR}/reports/market_snapshot_latest.json {REMOTE_DIR}/reports/market_review_latest.html",
            timeout=30,
        )
        run(client, f"cd {REMOTE_DIR} && {PYTHON} decision_attention.py && {PYTHON} portal_dashboard.py --out-dir {REMOTE_DIR}/reports", timeout=120)
        run(client, f"ls -lh {REMOTE_DIR}/reports/portal_latest.html {REMOTE_DIR}/reports/index.html", timeout=30)
        run(client, "systemctl list-timers --all | grep crypto-market-review || true", timeout=30)
        return rc
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
