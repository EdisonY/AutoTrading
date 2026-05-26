"""Deploy read-only Polymarket monitor to Tencent node as fallback."""

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
REMOTE_DIR = "/opt/polymarket-lab"
PYTHON = "/opt/crypto-auto-trader/.venv/bin/python"
ROOT = Path(__file__).resolve().parents[1]

UPLOADS = [
    (ROOT / "polymarket_lab" / "probe.py", f"{REMOTE_DIR}/probe.py"),
    (ROOT / "polymarket_lab" / "monitor.py", f"{REMOTE_DIR}/monitor.py"),
    (ROOT / "polymarket_lab" / "config.example.json", f"{REMOTE_DIR}/config.example.json"),
    (ROOT / "polymarket_lab" / "README.md", f"{REMOTE_DIR}/README.md"),
]


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
        except Exception as exc:
            last_error = exc
            try:
                client.close()
            except Exception:
                pass
            time.sleep(min(30, attempt * 5))
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
    run(client, f"sudo mkdir -p {REMOTE_DIR}/reports && sudo chown -R {USER}:{USER} {REMOTE_DIR}")
    dirs = sorted({os.path.dirname(remote) for _, remote in UPLOADS})
    run(client, "mkdir -p " + " ".join(dirs + [f"{REMOTE_DIR}/reports"]))
    sftp = client.open_sftp()
    try:
        for local, remote in UPLOADS:
            if not local.exists():
                raise FileNotFoundError(local)
            sftp.put(str(local), remote)
            print(f"OK upload {local} -> {remote}")
    finally:
        sftp.close()


def install_service(client: paramiko.SSHClient) -> int:
    service = f"""[Unit]
Description=Read-only Polymarket Continuous Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={USER}
WorkingDirectory={REMOTE_DIR}
Environment=PYTHONIOENCODING=utf-8
ExecStart={PYTHON} {REMOTE_DIR}/monitor.py --config {REMOTE_DIR}/config.example.json --all-markets --max-orderbooks 80 --interval-seconds 300 --run-timeout-seconds 420
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
"""
    remote_tmp = f"{REMOTE_DIR}/polymarket-monitor.service"
    sftp = client.open_sftp()
    try:
        with sftp.open(remote_tmp, "w") as f:
            f.write(service)
    finally:
        sftp.close()
    _, _, rc = run(client, f"sudo mv {remote_tmp} /etc/systemd/system/polymarket-monitor.service", timeout=30)
    if rc != 0:
        return rc
    run(client, "sudo systemctl daemon-reload", timeout=30)
    run(client, "sudo systemctl enable polymarket-monitor.service", timeout=30)
    _, _, rc = run(client, "sudo systemctl restart polymarket-monitor.service", timeout=60)
    return rc


def main() -> int:
    client = ssh()
    try:
        upload(client)
        files = " ".join(remote for _, remote in UPLOADS if remote.endswith(".py"))
        _, _, rc = run(client, f"cd {REMOTE_DIR} && {PYTHON} -m py_compile {files}", timeout=120)
        if rc != 0:
            return rc
        _, _, rc = run(
            client,
            f"cd {REMOTE_DIR} && {PYTHON} monitor.py --config {REMOTE_DIR}/config.example.json --all-markets --max-orderbooks 20 --once --run-timeout-seconds 180",
            timeout=240,
        )
        if rc != 0:
            return rc
        rc = install_service(client)
        if rc != 0:
            return rc
        run(client, "systemctl --no-pager --full status polymarket-monitor.service | sed -n '1,18p'", timeout=30)
        run(client, f"ls -la {REMOTE_DIR}/reports | tail -n 20", timeout=30)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
