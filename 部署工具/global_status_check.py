"""Global operational status check for live and shadow servers."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import paramiko

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


@dataclass(frozen=True)
class Server:
    label: str
    host: str
    user: str
    password: str | None
    remote_dir: str
    python: str


TENCENT = Server(
    label="Tencent live",
    host="129.226.151.144",
    user="ubuntu",
    password=os.environ.get("TENCENT_SSH_PASSWORD"),
    remote_dir="/opt/crypto-auto-trader",
    python="/opt/crypto-auto-trader/.venv/bin/python",
)

ALIYUN = Server(
    label="Aliyun shadow",
    host="39.105.156.210",
    user="root",
    password=os.environ.get("ALIYUN_SSH_PASSWORD"),
    remote_dir="/opt/crypto-shadow-lab",
    python="/root/miniconda3/bin/python3",
)


def connect(server: Server) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(server.host, 22, server.user, password=server.password or None, timeout=20, look_for_keys=True, allow_agent=True)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return rc, out.strip(), err.strip()


def section(title: str) -> None:
    print(f"\n## {title}")


def print_cmd(client: paramiko.SSHClient, title: str, cmd: str, timeout: int = 120) -> None:
    rc, out, err = run(client, cmd, timeout=timeout)
    print(f"\n### {title} (rc={rc})")
    if out:
        print(out[-4000:])
    if err:
        print("[stderr]")
        print(err[-1200:])


def check_tencent(run_account_snapshot: bool) -> None:
    section("Tencent live server")
    client = connect(TENCENT)
    try:
        print_cmd(client, "host/resources", "hostname; uptime; free -h; df -h /; timedatectl | sed -n '1,6p'")
        print_cmd(
            client,
            "strategy services",
            "systemctl show crypto-scanner crypto-scanner-v14 crypto-scanner-v16 "
            "-p Id -p ActiveState -p SubState -p UnitFileState -p MainPID -p NRestarts --no-pager",
        )
        print_cmd(
            client,
            "persistence timers",
            "systemctl show crypto-market-review.timer -p Id -p ActiveState -p UnitFileState -p NextElapseUSecRealtime -p Persistent --no-pager; "
            "systemctl list-timers --all | grep -E 'crypto-market|crypto-shadow' || true",
        )
        print_cmd(
            client,
            "processes",
            "ps -eo pid,etimes,cmd | grep -E 'scanner|daily_market|account_snapshot|dashboard|uvicorn' | grep -v grep || true",
        )
        print_cmd(
            client,
            "all crypto services",
            "systemctl list-units --type=service --all | grep crypto || true; "
            "systemctl list-unit-files | grep crypto || true",
        )
        print_cmd(
            client,
            "dashboard services should stay off",
            "systemctl show crypto-dashboard crypto-dashboard-b crypto-dashboard-c crypto-dashboard-dual "
            "-p Id -p ActiveState -p SubState -p UnitFileState -p MainPID --no-pager 2>/dev/null || true",
        )
        print_cmd(
            client,
            "live script compile",
            "cd /opt/crypto-auto-trader && "
            ".venv/bin/python -m py_compile scanner.py scanner_v14.py scanner_v16.py "
            "account_snapshot_html.py daily_market_review.py "
            "core/strategy_engine.py core/risk_engine.py core/execution_engine.py core/review_analytics.py",
        )
        print_cmd(
            client,
            "log file mtimes",
            "cd /opt/crypto-auto-trader && "
            "ls -lh --time-style='+%F %T' logs/system.jsonl logs/scanner_stdout.log "
            "logs_v14/system.jsonl logs_v14/scanner_stdout.log logs_v16/system.jsonl logs_v16/stdout.log "
            "scanner_data/trades.jsonl scanner_data_v14/trades.jsonl scanner_data_v16/trades.jsonl 2>/dev/null || true",
        )
        print_cmd(
            client,
            "latest logs",
            "cd /opt/crypto-auto-trader && "
            "for f in logs/system.jsonl logs_v14/system.jsonl logs_v16/system.jsonl logs/scanner_stdout.log logs_v14/scanner_stdout.log logs_v16/stdout.log; do "
            "echo ==== $f; [ -f $f ] && tail -n 5 $f || echo missing; done",
        )
        print_cmd(
            client,
            "reports/snapshots",
            "cd /opt/crypto-auto-trader && "
            "ls -lh reports/market_snapshot_latest.json reports/market_review_latest.html 2>/dev/null || true; "
            "ls -lh 复盘报告/account_snapshot_latest.html 2>/dev/null || true",
        )
        if run_account_snapshot:
            print_cmd(
                client,
                "account snapshot refresh",
                "cd /opt/crypto-auto-trader && PYTHONIOENCODING=utf-8 "
                f"{TENCENT.python} account_snapshot_html.py && "
                "ls -lh 复盘报告/account_snapshot_latest.html",
                timeout=180,
            )
    finally:
        client.close()


def check_aliyun() -> None:
    section("Aliyun shadow server")
    client = connect(ALIYUN)
    try:
        print_cmd(client, "host/resources", "hostname; uptime; free -h; df -h /; timedatectl | sed -n '1,6p'")
        print_cmd(
            client,
            "shadow service/timer",
            "systemctl show crypto-shadow-review.service crypto-shadow-review.timer "
            "-p Id -p ActiveState -p SubState -p UnitFileState -p MainPID -p NRestarts -p NextElapseUSecRealtime -p Persistent --no-pager; "
            "systemctl list-timers --all | grep crypto-shadow || true",
        )
        print_cmd(
            client,
            "processes",
            "ps -eo pid,etimes,cmd | grep -E 'shadow|research|experiment|portal|python' | grep -v grep || true",
        )
        print_cmd(
            client,
            "latest artifacts",
            "cd /opt/crypto-shadow-lab && "
            "ls -lh reports/research_review_latest.html reports/portal_latest.html reports/shadow_experiments_*.html "
            "experiments/results/latest.jsonl experiments/families_latest.json server_logs_tencent/reports/market_snapshot_latest.json 2>/dev/null || true",
        )
        print_cmd(
            client,
            "script compile",
            "cd /opt/crypto-shadow-lab && "
            f"{ALIYUN.python} -m py_compile research_memory_builder.py experiment_runner.py experiment_report.py "
            "research_review_dashboard.py portal_dashboard.py apply_research_approval.py shadow_sync_from_tencent.py",
        )
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="全局检查实盘/影子服务器状态")
    parser.add_argument("--skip-account-snapshot", action="store_true", help="不刷新腾讯账号盈亏快照")
    args = parser.parse_args(argv)
    check_tencent(run_account_snapshot=not args.skip_account_snapshot)
    check_aliyun()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
