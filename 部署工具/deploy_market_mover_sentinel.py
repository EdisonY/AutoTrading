"""Deploy 120s scanner interval and lightweight market-mover sentinel."""

from __future__ import annotations

import os
import sys
import time
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
    (ROOT / "策略文件" / "scanner.py", f"{REMOTE_DIR}/scanner.py"),
    (ROOT / "策略文件" / "scanner_v14.py", f"{REMOTE_DIR}/scanner_v14.py"),
    (ROOT / "策略文件" / "scanner_v16.py", f"{REMOTE_DIR}/scanner_v16.py"),
    (ROOT / "策略文件" / "market_mover_sentinel.py", f"{REMOTE_DIR}/market_mover_sentinel.py"),
    (ROOT / "core" / "market_watchlist.py", f"{REMOTE_DIR}/core/market_watchlist.py"),
    (ROOT / "core" / "models.py", f"{REMOTE_DIR}/core/models.py"),
    (ROOT / "core" / "review_analytics.py", f"{REMOTE_DIR}/core/review_analytics.py"),
    (ROOT / "部署工具" / "signal_quality_review.py", f"{REMOTE_DIR}/signal_quality_review.py"),
]

SCANNER_SERVICES = ["crypto-scanner", "crypto-scanner-v14", "crypto-scanner-v16"]
SENTINEL_SERVICE = "crypto-market-mover-sentinel"


def ssh() -> paramiko.SSHClient:
    last_error: Exception | None = None
    for attempt in range(1, 6):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            print(f"SSH connect attempt {attempt}/5")
            client.connect(
                HOST,
                22,
                USER,
                password=PASS or None,
                timeout=30,
                banner_timeout=60,
                auth_timeout=30,
                look_for_keys=True,
                allow_agent=True,
            )
            return client
        except Exception as e:
            last_error = e
            try:
                client.close()
            except Exception:
                pass
            wait = min(30, attempt * 5)
            print(f"SSH connect failed: {type(e).__name__}: {e}; retry in {wait}s")
            time.sleep(wait)
    raise last_error or RuntimeError("SSH connect failed")


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[str, str, int]:
    print(f">> {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out.strip():
        print(out.strip()[-2400:])
    if rc != 0 and err.strip():
        print(err.strip()[-2400:])
    return out, err, rc


def upload(client: paramiko.SSHClient) -> None:
    dirs = sorted({os.path.dirname(remote) for _, remote in UPLOADS})
    run(client, "mkdir -p " + " ".join(dirs) + f" {REMOTE_DIR}/runtime {REMOTE_DIR}/logs")
    sftp = client.open_sftp()
    try:
        for local, remote in UPLOADS:
            if not local.exists():
                raise FileNotFoundError(local)
            sftp.put(str(local), remote)
            print(f"OK upload {local} -> {remote}")
    finally:
        sftp.close()


def install_sentinel_service(client: paramiko.SSHClient) -> int:
    service = f"""[Unit]
Description=Crypto Market Mover Sentinel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={REMOTE_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart={PYTHON} -u {REMOTE_DIR}/market_mover_sentinel.py --root {REMOTE_DIR} --interval 15
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    remote_tmp = f"{REMOTE_DIR}/{SENTINEL_SERVICE}.service"
    sftp = client.open_sftp()
    try:
        with sftp.open(remote_tmp, "w") as f:
            f.write(service)
    finally:
        sftp.close()
    _, _, rc = run(client, f"sudo mv {remote_tmp} /etc/systemd/system/{SENTINEL_SERVICE}.service", timeout=30)
    return rc


def update_scanner_services(client: paramiko.SSHClient) -> int:
    commands = [
        "sudo sed -i 's/--interval 300/--interval 120/g; s/--interval 420/--interval 120/g' /opt/crypto-auto-trader/run_scanner.sh /opt/crypto-auto-trader/run_scanner_v14.sh 2>/dev/null || true",
        "sudo grep -R \"--interval\" /opt/crypto-auto-trader/run_scanner*.sh 2>/dev/null || true",
    ]
    for cmd in commands:
        _, _, rc = run(client, cmd, timeout=30)
        if rc != 0:
            return rc
    return 0


def main() -> int:
    client = ssh()
    try:
        upload(client)
        files = " ".join(remote for _, remote in UPLOADS)
        _, _, rc = run(client, f"cd {REMOTE_DIR} && {PYTHON} -m py_compile {files}", timeout=120)
        if rc != 0:
            return rc
        rc = install_sentinel_service(client)
        if rc != 0:
            return rc
        rc = update_scanner_services(client)
        if rc != 0:
            return rc

        run(client, "sudo systemctl daemon-reload", timeout=30)
        run(client, f"sudo systemctl enable {SENTINEL_SERVICE}", timeout=30)
        _, _, rc = run(client, f"sudo systemctl restart {SENTINEL_SERVICE}", timeout=60)
        if rc != 0:
            return rc
        for svc in SCANNER_SERVICES:
            _, _, rc = run(client, f"sudo systemctl restart {svc}", timeout=90)
            if rc != 0:
                return rc
            time.sleep(2)
            _, _, rc = run(client, f"systemctl is-active {svc}", timeout=20)
            if rc != 0:
                return rc
        time.sleep(8)
        run(
            client,
            f"systemctl is-active {SENTINEL_SERVICE} {' '.join(SCANNER_SERVICES)}; "
            f"cd {REMOTE_DIR} && ls -lh runtime/market_mover_watchlist.json logs/market_mover_sentinel.jsonl 2>/dev/null && "
            f"tail -3 logs/market_mover_sentinel.jsonl 2>/dev/null",
            timeout=60,
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
