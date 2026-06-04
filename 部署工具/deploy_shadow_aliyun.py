"""Deploy read-only shadow review / experiment system to Aliyun."""

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

ALIYUN_HOST = os.environ.get("ALIYUN_HOST", "39.105.156.210")
ALIYUN_USER = os.environ.get("ALIYUN_USER", "root")
ALIYUN_PASS = os.environ.get("ALIYUN_SSH_PASSWORD")

REMOTE_DIR = "/opt/crypto-shadow-lab"
PYTHON = "/root/miniconda3/bin/python3"
ROOT = Path(__file__).resolve().parents[1]

UPLOADS = [
    (ROOT / "core" / "__init__.py", f"{REMOTE_DIR}/core/__init__.py"),
    (ROOT / "core" / "models.py", f"{REMOTE_DIR}/core/models.py"),
    (ROOT / "core" / "binance_api_queue.py", f"{REMOTE_DIR}/core/binance_api_queue.py"),
    (ROOT / "core" / "binance_api_guard.py", f"{REMOTE_DIR}/core/binance_api_guard.py"),
    (ROOT / "core" / "binance_api_queue_client.py", f"{REMOTE_DIR}/core/binance_api_queue_client.py"),
    (ROOT / "core" / "position_utils.py", f"{REMOTE_DIR}/core/position_utils.py"),
    (ROOT / "core" / "replay.py", f"{REMOTE_DIR}/core/replay.py"),
    (ROOT / "core" / "replay_depth_cache.py", f"{REMOTE_DIR}/core/replay_depth_cache.py"),
    (ROOT / "core" / "replay_fill.py", f"{REMOTE_DIR}/core/replay_fill.py"),
    (ROOT / "core" / "replay_kline_source.py", f"{REMOTE_DIR}/core/replay_kline_source.py"),
    (ROOT / "core" / "review_analytics.py", f"{REMOTE_DIR}/core/review_analytics.py"),
    (ROOT / "core" / "experiment.py", f"{REMOTE_DIR}/core/experiment.py"),
    (ROOT / "core" / "research_memory.py", f"{REMOTE_DIR}/core/research_memory.py"),
    (ROOT / "部署工具" / "daily_market_review.py", f"{REMOTE_DIR}/daily_market_review.py"),
    (ROOT / "部署工具" / "shadow_sync_from_tencent.py", f"{REMOTE_DIR}/shadow_sync_from_tencent.py"),
    (ROOT / "部署工具" / "signal_quality_review.py", f"{REMOTE_DIR}/signal_quality_review.py"),
    (ROOT / "部署工具" / "experiment_runner.py", f"{REMOTE_DIR}/experiment_runner.py"),
    (ROOT / "部署工具" / "experiment_report.py", f"{REMOTE_DIR}/experiment_report.py"),
    (ROOT / "部署工具" / "strategy_evolution_gate.py", f"{REMOTE_DIR}/strategy_evolution_gate.py"),
    (ROOT / "部署工具" / "decision_attention.py", f"{REMOTE_DIR}/decision_attention.py"),
    (ROOT / "部署工具" / "research_memory_builder.py", f"{REMOTE_DIR}/research_memory_builder.py"),
    (ROOT / "部署工具" / "research_kline_features.py", f"{REMOTE_DIR}/research_kline_features.py"),
    (ROOT / "部署工具" / "research_kline_backfill.py", f"{REMOTE_DIR}/research_kline_backfill.py"),
    (ROOT / "部署工具" / "research_depth_backfill.py", f"{REMOTE_DIR}/research_depth_backfill.py"),
    (ROOT / "部署工具" / "research_store_retention.py", f"{REMOTE_DIR}/research_store_retention.py"),
    (ROOT / "部署工具" / "research_store_compaction.py", f"{REMOTE_DIR}/research_store_compaction.py"),
    (ROOT / "部署工具" / "replay_feature_dataset.py", f"{REMOTE_DIR}/replay_feature_dataset.py"),
    (ROOT / "部署工具" / "replay_readiness_review.py", f"{REMOTE_DIR}/replay_readiness_review.py"),
    (ROOT / "部署工具" / "long_term_skeleton_review.py", f"{REMOTE_DIR}/long_term_skeleton_review.py"),
    (ROOT / "部署工具" / "rollback_watch_review.py", f"{REMOTE_DIR}/rollback_watch_review.py"),
    (ROOT / "部署工具" / "rollback_execution_plan.py", f"{REMOTE_DIR}/rollback_execution_plan.py"),
    (ROOT / "部署工具" / "rollback_automation_guard.py", f"{REMOTE_DIR}/rollback_automation_guard.py"),
    (ROOT / "部署工具" / "research_store_export.py", f"{REMOTE_DIR}/research_store_export.py"),
    (ROOT / "部署工具" / "research_store_query.py", f"{REMOTE_DIR}/research_store_query.py"),
    (ROOT / "部署工具" / "research_review_dashboard.py", f"{REMOTE_DIR}/research_review_dashboard.py"),
    (ROOT / "部署工具" / "portal_dashboard.py", f"{REMOTE_DIR}/portal_dashboard.py"),
    (ROOT / "部署工具" / "decision_portal.py", f"{REMOTE_DIR}/decision_portal.py"),
    (ROOT / "部署工具" / "apply_research_approval.py", f"{REMOTE_DIR}/apply_research_approval.py"),
    (ROOT / "部署工具" / "strategy_truth_ledger.py", f"{REMOTE_DIR}/strategy_truth_ledger.py"),
    (ROOT / "部署工具" / "sentinel_quality_review.py", f"{REMOTE_DIR}/sentinel_quality_review.py"),
    (ROOT / "部署工具" / "counterfactual_open_skips.py", f"{REMOTE_DIR}/counterfactual_open_skips.py"),
    (ROOT / "部署工具" / "sync_aliyun_reports_to_tencent.py", f"{REMOTE_DIR}/sync_aliyun_reports_to_tencent.py"),
    (ROOT / "部署工具" / "attention_api_server.py", f"{REMOTE_DIR}/attention_api_server.py"),
    (ROOT / "部署工具" / "aliyun_analysis_refresh.sh", f"{REMOTE_DIR}/aliyun_analysis_refresh.sh"),
    (ROOT / "部署工具" / "aliyun_decision_portal_refresh.sh", f"{REMOTE_DIR}/aliyun_decision_portal_refresh.sh"),
    (ROOT / "部署工具" / "aliyun_shadow_review.sh", f"{REMOTE_DIR}/run_shadow_review.sh"),
    (ROOT / "部署工具" / "systemd" / "crypto-decision-portal-refresh.service", f"{REMOTE_DIR}/systemd/crypto-decision-portal-refresh.service"),
    (ROOT / "部署工具" / "systemd" / "crypto-decision-portal-refresh.timer", f"{REMOTE_DIR}/systemd/crypto-decision-portal-refresh.timer"),
]


def ssh() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ALIYUN_HOST, 22, ALIYUN_USER, password=ALIYUN_PASS or None, timeout=20, look_for_keys=True, allow_agent=True)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> tuple[str, str, int]:
    print(f">> {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out.strip():
        print(out.strip()[-1800:])
    if rc != 0 and err.strip():
        print(err.strip()[-1800:])
    return out, err, rc


def upload(client: paramiko.SSHClient) -> None:
    dirs = sorted({os.path.dirname(remote) for _, remote in UPLOADS})
    run(client, "mkdir -p " + " ".join(dirs + [f"{REMOTE_DIR}/server_logs_tencent", f"{REMOTE_DIR}/experiments/results", f"{REMOTE_DIR}/reports"]))
    sftp = client.open_sftp()
    try:
        for local, remote in UPLOADS:
            if not local.exists():
                raise FileNotFoundError(local)
            sftp.put(str(local), remote)
            print(f"OK upload {local} -> {remote}")
    finally:
        sftp.close()


def install_service(client: paramiko.SSHClient) -> None:
    service = f"""[Unit]
Description=Crypto Shadow Review and Experiment Run
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={REMOTE_DIR}
ExecStart=/bin/bash {REMOTE_DIR}/run_shadow_review.sh
"""
    timer = """[Unit]
Description=Daily Crypto Shadow Review Timer

[Timer]
OnCalendar=*-*-* 02:20:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    sftp = client.open_sftp()
    try:
        with sftp.open("/etc/systemd/system/crypto-shadow-review.service", "w") as f:
            f.write(service)
        with sftp.open("/etc/systemd/system/crypto-shadow-review.timer", "w") as f:
            f.write(timer)
    finally:
        sftp.close()
    run(client, f"chmod +x {REMOTE_DIR}/run_shadow_review.sh")
    run(client, f"chmod +x {REMOTE_DIR}/aliyun_decision_portal_refresh.sh")
    run(
        client,
        f"cp {REMOTE_DIR}/systemd/crypto-decision-portal-refresh.service /etc/systemd/system/crypto-decision-portal-refresh.service "
        f"&& cp {REMOTE_DIR}/systemd/crypto-decision-portal-refresh.timer /etc/systemd/system/crypto-decision-portal-refresh.timer",
    )
    run(client, "systemctl daemon-reload")
    run(client, "systemctl enable crypto-shadow-review.timer")
    run(client, "systemctl restart crypto-shadow-review.timer")
    run(client, "systemctl enable --now crypto-decision-portal-refresh.timer")


def main() -> int:
    client = ssh()
    try:
        upload(client)
        py_files = " ".join(remote for _, remote in UPLOADS if remote.endswith(".py"))
        _, _, rc = run(client, f"cd {REMOTE_DIR} && {PYTHON} -m py_compile {py_files}", timeout=120)
        if rc != 0:
            return rc
        _, _, rc = run(client, f"{PYTHON} -c 'import paramiko; print(\"paramiko ok\")'", timeout=30)
        if rc != 0:
            _, _, rc = run(client, f"{PYTHON} -m pip install paramiko", timeout=180)
        if rc != 0:
            return rc
        install_service(client)
        run(
            client,
            f"cd {REMOTE_DIR} && {PYTHON} research_review_dashboard.py "
            f"--memory-dir {REMOTE_DIR}/research_memory "
            f"--experiment-results {REMOTE_DIR}/experiments/results/latest.jsonl "
            f"--out-dir {REMOTE_DIR}/reports",
            timeout=120,
        )
        run(
            client,
            f"cd {REMOTE_DIR} && {PYTHON} strategy_evolution_gate.py "
            f"--memory-dir {REMOTE_DIR}/research_memory "
            f"--experiments-dir {REMOTE_DIR}/experiments "
            f"--reports-dir {REMOTE_DIR}/reports "
            f"--runtime-dir {REMOTE_DIR}/runtime",
            timeout=120,
        )
        run(client, f"cd {REMOTE_DIR} && {PYTHON} decision_attention.py || true", timeout=60)
        run(client, f"cd {REMOTE_DIR} && {PYTHON} portal_dashboard.py --out-dir {REMOTE_DIR}/reports", timeout=120)
        run(client, "systemctl list-timers --all | grep crypto-shadow || true", timeout=30)
        run(client, f"ls -la {REMOTE_DIR}", timeout=30)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
