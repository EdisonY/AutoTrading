"""Unified deploy and rollback helper for AutoTrading servers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import paramiko

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
CST = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class RemoteTarget:
    name: str
    host_env: str
    default_host: str
    user_env: str
    default_user: str
    pass_env: str
    key_env: str
    root: str
    python: str


TARGETS = {
    "tencent": RemoteTarget(
        name="tencent",
        host_env="TENCENT_HOST",
        default_host="129.226.151.144",
        user_env="TENCENT_USER",
        default_user="ubuntu",
        pass_env="TENCENT_SSH_PASSWORD",
        key_env="TENCENT_SSH_KEY",
        root="/opt/crypto-auto-trader",
        python="/opt/crypto-auto-trader/.venv/bin/python",
    ),
    "aliyun": RemoteTarget(
        name="aliyun",
        host_env="ALIYUN_HOST",
        default_host="39.105.156.210",
        user_env="ALIYUN_USER",
        default_user="root",
        pass_env="ALIYUN_SSH_PASSWORD",
        key_env="ALIYUN_SSH_KEY",
        root="/opt/crypto-shadow-lab",
        python="/root/miniconda3/bin/python3",
    ),
}


def file_pair(local: str, remote: str | None = None) -> tuple[Path, str]:
    return ROOT / local, remote or local.replace("\\", "/")


CORE_FILES = [
    file_pair("core/__init__.py"),
    file_pair("core/account_state.py"),
    file_pair("core/account_state_cache.py"),
    file_pair("core/account_state_stream.py"),
    file_pair("core/audit_log.py"),
    file_pair("core/binance_api_queue.py"),
    file_pair("core/binance_api_queue_client.py"),
    file_pair("core/binance_api_executor.py"),
    file_pair("core/binance_api_guard.py"),
    file_pair("core/binance_user_stream.py"),
    file_pair("core/binance_user_stream_runtime.py"),
    file_pair("core/binance_order_rules.py"),
    file_pair("core/event_store.py"),
    file_pair("core/execution_engine.py"),
    file_pair("core/exchange_state.py"),
    file_pair("core/external_market_data.py"),
    file_pair("core/kline_cache.py"),
    file_pair("core/market_data_cache.py"),
    file_pair("core/market_microstructure.py"),
    file_pair("core/market_watchlist.py"),
    file_pair("core/paper_exchange.py"),
    file_pair("core/position_utils.py"),
    file_pair("core/replay.py"),
    file_pair("core/replay_depth_cache.py"),
    file_pair("core/replay_fill.py"),
    file_pair("core/risk_engine.py"),
    file_pair("core/sentinel_event_bus.py"),
    file_pair("core/sentinel_scanner.py"),
    file_pair("core/strategy_config.py"),
    file_pair("core/strategy_engine.py"),
    file_pair("core/strategy_gate_cases.py"),
    file_pair("core/strategy_gates.py"),
    file_pair("cloud/__init__.py"),
    file_pair("cloud/analyzer/__init__.py"),
    file_pair("cloud/analyzer/auxiliary.py"),
]

def scanner_account_state_recovery_dropin(service: str) -> tuple[Path, str]:
    return file_pair(
        "部署工具/systemd/crypto-scanner-account-state-recovery.conf",
        f"{service}.account-state-recovery.conf",
    )


def scanner_paper_full_run_dropin(service: str) -> tuple[Path, str]:
    return file_pair(
        "部署工具/systemd/crypto-scanner-paper-full-run.conf",
        f"{service}.paper-full-run.conf",
    )


def scanner_account_state_recovery_post(service: str) -> str:
    return (
        f"sudo mkdir -p /etc/systemd/system/{service}.d && "
        f"sudo cp {service}.account-state-recovery.conf "
        f"/etc/systemd/system/{service}.d/30-account-state-recovery.conf && "
        "sudo systemctl daemon-reload"
    )


def scanner_paper_full_run_post(service: str) -> str:
    return (
        f"sudo mkdir -p /etc/systemd/system/{service}.d && "
        f"sudo cp {service}.paper-full-run.conf "
        f"/etc/systemd/system/{service}.d/60-paper-full-run.conf && "
        "sudo systemctl daemon-reload"
    )


RESEARCH_CORE = [
    file_pair("core/__init__.py"),
    file_pair("core/account_state.py"),
    file_pair("core/account_state_stream.py"),
    file_pair("core/binance_api_queue.py"),
    file_pair("core/binance_api_queue_client.py"),
    file_pair("core/binance_api_executor.py"),
    file_pair("core/binance_api_guard.py"),
    file_pair("core/binance_user_stream.py"),
    file_pair("core/binance_user_stream_runtime.py"),
    file_pair("core/event_store.py"),
    file_pair("core/external_market_data.py"),
    file_pair("core/kline_cache.py"),
    file_pair("core/market_microstructure.py"),
    file_pair("core/models.py"),
    file_pair("core/position_utils.py"),
    file_pair("core/replay.py"),
    file_pair("core/replay_depth_cache.py"),
    file_pair("core/replay_fill.py"),
    file_pair("core/replay_kline_source.py"),
    file_pair("core/review_analytics.py"),
    file_pair("core/experiment.py"),
    file_pair("core/research_memory.py"),
    file_pair("core/paper_broker.py"),
    file_pair("core/paper_exchange.py"),
    file_pair("core/strategy_gate_cases.py"),
    file_pair("core/strategy_gates.py"),
]

TENCENT_COMPONENTS: dict[str, dict[str, Any]] = {
    "portal": {
        "files": [
            file_pair("部署工具/decision_attention.py", "decision_attention.py"),
            file_pair("部署工具/acknowledge_attention_items.py", "acknowledge_attention_items.py"),
            file_pair("部署工具/long_term_skeleton_review.py", "long_term_skeleton_review.py"),
            file_pair("部署工具/paper_exchange_runner.py", "paper_exchange_runner.py"),
            file_pair("部署工具/portal_dashboard.py", "portal_dashboard.py"),
            file_pair("部署工具/decision_portal.py", "decision_portal.py"),
            file_pair("部署工具/portal_refresh_service.py", "portal_refresh_service.py"),
            file_pair("部署工具/system_alerts.py", "system_alerts.py"),
            file_pair("部署工具/systemd/crypto-paper-exchange-refresh.service", "systemd/crypto-paper-exchange-refresh.service"),
            file_pair("部署工具/systemd/crypto-paper-exchange-refresh.timer", "systemd/crypto-paper-exchange-refresh.timer"),
        ],
        # Portal generation moved to Aliyun. Tencent keeps only the alert service
        # here so a portal deploy cannot restart an intentionally inactive unit.
        "services": ["crypto-system-alerts.service"],
        "post": [
            "{python} long_term_skeleton_review.py --runtime-dir {root}/runtime --reports-dir {root}/reports || true",
            "{python} system_alerts.py --once || true",
            "{python} decision_attention.py || true",
            "{python} portal_dashboard.py --out-dir {root}/reports",
            "sudo cp systemd/crypto-paper-exchange-refresh.service /etc/systemd/system/crypto-paper-exchange-refresh.service && sudo cp systemd/crypto-paper-exchange-refresh.timer /etc/systemd/system/crypto-paper-exchange-refresh.timer && sudo systemctl daemon-reload && sudo systemctl enable --now crypto-paper-exchange-refresh.timer",
        ],
    },
    "strategy-a": {
        "files": CORE_FILES
        + [
            scanner_account_state_recovery_dropin("crypto-scanner.service"),
            scanner_paper_full_run_dropin("crypto-scanner.service"),
            file_pair("策略文件/scanner.py", "scanner.py"),
            file_pair("策略文件/strategy_breakout.py", "strategy_breakout.py"),
            file_pair("交易客户端/binance_client.py", "binance_client.py"),
            file_pair("config/v11.toml", "config/v11.toml"),
        ],
        "services": ["crypto-scanner.service"],
        "post": [
            scanner_account_state_recovery_post("crypto-scanner.service"),
            scanner_paper_full_run_post("crypto-scanner.service"),
        ],
    },
    "strategy-b": {
        "files": CORE_FILES
        + [
            scanner_account_state_recovery_dropin("crypto-scanner-v16.service"),
            scanner_paper_full_run_dropin("crypto-scanner-v16.service"),
            file_pair("策略文件/scanner_v16.py", "scanner_v16.py"),
            file_pair("交易客户端/binance_client_v2.py", "binance_client_v2.py"),
            file_pair("config/v16.toml", "config/v16.toml"),
        ],
        "services": ["crypto-scanner-v16.service"],
        "post": [
            scanner_account_state_recovery_post("crypto-scanner-v16.service"),
            scanner_paper_full_run_post("crypto-scanner-v16.service"),
        ],
    },
    "strategy-c": {
        "files": CORE_FILES
        + [
            scanner_account_state_recovery_dropin("crypto-scanner-v14.service"),
            scanner_paper_full_run_dropin("crypto-scanner-v14.service"),
            file_pair("策略文件/scanner_v14.py", "scanner_v14.py"),
            file_pair("交易客户端/binance_client_v3.py", "binance_client_v3.py"),
            file_pair("config/v14.toml", "config/v14.toml"),
        ],
        "services": ["crypto-scanner-v14.service"],
        "post": [
            scanner_account_state_recovery_post("crypto-scanner-v14.service"),
            scanner_paper_full_run_post("crypto-scanner-v14.service"),
        ],
    },
    "sentinel": {
        "files": CORE_FILES
        + [
            file_pair("部署工具/binance_start_guard.py", "binance_start_guard.py"),
            file_pair("策略文件/market_data_service.py", "market_data_service.py"),
            file_pair("部署工具/systemd/crypto-market-data-cache.service", "systemd/crypto-market-data-cache.service"),
        ],
        "services": ["crypto-market-data-cache.service"],
        "post": [
            "sudo cp systemd/crypto-market-data-cache.service /etc/systemd/system/crypto-market-data-cache.service && sudo systemctl daemon-reload",
        ],
    },
    "market-data": {
        "files": CORE_FILES
        + [
            file_pair("策略文件/market_data_service.py", "market_data_service.py"),
            file_pair("部署工具/market_microstructure_service.py", "market_microstructure_service.py"),
            file_pair("部署工具/systemd/crypto-market-data-cache.service", "systemd/crypto-market-data-cache.service"),
            file_pair("部署工具/systemd/crypto-market-microstructure.service", "systemd/crypto-market-microstructure.service"),
        ],
        "services": ["crypto-market-data-cache.service", "crypto-market-microstructure.service"],
        "post": [
            "{python} -c \"import market_data_service; import market_microstructure_service; print('market data imports ok')\"",
            "sudo cp systemd/crypto-market-data-cache.service /etc/systemd/system/crypto-market-data-cache.service && sudo cp systemd/crypto-market-microstructure.service /etc/systemd/system/crypto-market-microstructure.service && sudo systemctl daemon-reload && sudo systemctl enable crypto-market-data-cache.service crypto-market-microstructure.service",
        ],
    },
    "account": {
        "files": CORE_FILES
        + [
            file_pair("部署工具/binance_start_guard.py", "binance_start_guard.py"),
            file_pair("部署工具/account_snapshot_service.py", "account_snapshot_service.py"),
            file_pair("部署工具/account_state_service.py", "account_state_service.py"),
            file_pair("部署工具/account_snapshot_html.py", "account_snapshot_html.py"),
            file_pair("部署工具/systemd/crypto-account-state.service", "systemd/crypto-account-state.service"),
            file_pair("部署工具/systemd/crypto-account-snapshot-api-throttle.conf", "systemd/crypto-account-snapshot-api-throttle.conf"),
            file_pair("交易客户端/binance_client.py", "binance_client.py"),
            file_pair("交易客户端/binance_client_v2.py", "binance_client_v2.py"),
            file_pair("交易客户端/binance_client_v3.py", "binance_client_v3.py"),
        ],
        "services": ["crypto-account-snapshot.service"],
        "post": [
            '{python} -c "import account_snapshot_service; print(\'account_snapshot_service import ok\')"',
            "sudo mkdir -p /etc/systemd/system/crypto-account-snapshot.service.d && sudo cp systemd/crypto-account-snapshot-api-throttle.conf /etc/systemd/system/crypto-account-snapshot.service.d/10-api-throttle.conf && sudo systemctl daemon-reload",
        ],
    },
    "account-state": {
        "files": CORE_FILES
        + [
            file_pair("部署工具/binance_start_guard.py", "binance_start_guard.py"),
            file_pair("部署工具/account_snapshot_service.py", "account_snapshot_service.py"),
            file_pair("部署工具/account_state_service.py", "account_state_service.py"),
            file_pair("部署工具/account_snapshot_html.py", "account_snapshot_html.py"),
            file_pair("部署工具/systemd/crypto-account-state.service", "systemd/crypto-account-state.service"),
            file_pair("交易客户端/binance_client.py", "binance_client.py"),
            file_pair("交易客户端/binance_client_v2.py", "binance_client_v2.py"),
            file_pair("交易客户端/binance_client_v3.py", "binance_client_v3.py"),
        ],
        "services": [],
        "post": [
            '{python} -c "import account_state_service; print(\'account_state_service import ok\')"',
            "sudo cp systemd/crypto-account-state.service /etc/systemd/system/crypto-account-state.service && sudo systemctl daemon-reload",
        ],
    },
    "api-queue": {
        "files": CORE_FILES
        + [
            file_pair("部署工具/binance_start_guard.py", "binance_start_guard.py"),
            file_pair("部署工具/binance_api_queue_service.py", "binance_api_queue_service.py"),
            file_pair("部署工具/systemd/crypto-binance-api-queue.service", "systemd/crypto-binance-api-queue.service"),
        ],
        "services": [],
        "post": [
            '{python} -c "import binance_api_queue_service; print(\'binance_api_queue_service import ok\')"',
            "sudo cp systemd/crypto-binance-api-queue.service /etc/systemd/system/crypto-binance-api-queue.service && sudo systemctl daemon-reload",
        ],
    },
    "user-stream": {
        "files": CORE_FILES
        + [
            file_pair("部署工具/binance_start_guard.py", "binance_start_guard.py"),
            file_pair("部署工具/binance_user_stream_service.py", "binance_user_stream_service.py"),
            file_pair("部署工具/systemd/crypto-binance-user-stream.service", "systemd/crypto-binance-user-stream.service"),
            file_pair("部署工具/systemd/crypto-binance-user-stream-v16.service", "systemd/crypto-binance-user-stream-v16.service"),
            file_pair("部署工具/systemd/crypto-binance-user-stream-v14.service", "systemd/crypto-binance-user-stream-v14.service"),
        ],
        "services": [],
        "post": [
            '{python} -c "import binance_user_stream_service; print(\'binance_user_stream_service import ok\')"',
            "sudo cp systemd/crypto-binance-user-stream.service /etc/systemd/system/crypto-binance-user-stream.service && sudo cp systemd/crypto-binance-user-stream-v16.service /etc/systemd/system/crypto-binance-user-stream-v16.service && sudo cp systemd/crypto-binance-user-stream-v14.service /etc/systemd/system/crypto-binance-user-stream-v14.service && sudo systemctl daemon-reload",
        ],
    },
    "research": {
        "files": RESEARCH_CORE
        + [
            file_pair("部署工具/counterfactual_open_skips.py", "counterfactual_open_skips.py"),
            file_pair("部署工具/apply_research_approval.py", "apply_research_approval.py"),
            file_pair("部署工具/a_v11_rollout_review.py", "a_v11_rollout_review.py"),
            file_pair("部署工具/b_v16_rollout_review.py", "b_v16_rollout_review.py"),
            file_pair("部署工具/backtest_engine.py", "backtest_engine.py"),
            file_pair("部署工具/backtest_module.py", "backtest_module.py"),
            file_pair("部署工具/v11_historical_research_report.py", "v11_historical_research_report.py"),
            file_pair("部署工具/b_v16_historical_research_report.py", "b_v16_historical_research_report.py"),
            file_pair("部署工具/c_v14_historical_research_report.py", "c_v14_historical_research_report.py"),
            file_pair("部署工具/d_e_f_historical_research_report.py", "d_e_f_historical_research_report.py"),
            file_pair("部署工具/alpha_discovery_research_report.py", "alpha_discovery_research_report.py"),
            file_pair("部署工具/regime_classifier.py", "regime_classifier.py"),
            file_pair("部署工具/signal_edge_lab.py", "signal_edge_lab.py"),
            file_pair("部署工具/context_alpha_lab.py", "context_alpha_lab.py"),
            file_pair("部署工具/j_k_l_indicator_research_report.py", "j_k_l_indicator_research_report.py"),
            file_pair("部署工具/k_alpha_research.py", "k_alpha_research.py"),
            file_pair("部署工具/l1_edge_strategy_reconstruction.py", "l1_edge_strategy_reconstruction.py"),
            file_pair("部署工具/j3_v2_strategy_research.py", "j3_v2_strategy_research.py"),
            file_pair("部署工具/research_paper_strategy_runner.py", "research_paper_strategy_runner.py"),
            file_pair("部署工具/account_state_service.py", "account_state_service.py"),
            file_pair("部署工具/cleanup_event_store.py", "cleanup_event_store.py"),
            file_pair("部署工具/data_maintenance.py", "data_maintenance.py"),
            file_pair("部署工具/daily_market_review.py", "daily_market_review.py"),
            file_pair("部署工具/decision_attention.py", "decision_attention.py"),
            file_pair("部署工具/experiment_report.py", "experiment_report.py"),
            file_pair("部署工具/experiment_runner.py", "experiment_runner.py"),
            file_pair("部署工具/research_memory_builder.py", "research_memory_builder.py"),
            file_pair("部署工具/research_kline_features.py", "research_kline_features.py"),
            file_pair("部署工具/research_kline_backfill.py", "research_kline_backfill.py"),
            file_pair("部署工具/historical_kline_backfill.py", "historical_kline_backfill.py"),
            file_pair("部署工具/research_depth_backfill.py", "research_depth_backfill.py"),
            file_pair("部署工具/external_replay_data_ingest.py", "external_replay_data_ingest.py"),
            file_pair("部署工具/research_store_retention.py", "research_store_retention.py"),
            file_pair("部署工具/research_store_compaction.py", "research_store_compaction.py"),
            file_pair("部署工具/replay_feature_dataset.py", "replay_feature_dataset.py"),
            file_pair("部署工具/replay_gate_audit.py", "replay_gate_audit.py"),
            file_pair("部署工具/replay_live_parity_audit.py", "replay_live_parity_audit.py"),
            file_pair("部署工具/replay_readiness_review.py", "replay_readiness_review.py"),
            file_pair("部署工具/waiting_period_optimization.py", "waiting_period_optimization.py"),
            file_pair("部署工具/paper_exchange_runner.py", "paper_exchange_runner.py"),
            file_pair("部署工具/rollback_watch_review.py", "rollback_watch_review.py"),
            file_pair("部署工具/rollback_execution_plan.py", "rollback_execution_plan.py"),
            file_pair("部署工具/rollback_automation_guard.py", "rollback_automation_guard.py"),
            file_pair("部署工具/auto_upgrade_readiness.py", "auto_upgrade_readiness.py"),
            file_pair("部署工具/strategy_candidate_governance.py", "strategy_candidate_governance.py"),
            file_pair("部署工具/waiting_period_progress.py", "waiting_period_progress.py"),
            file_pair("部署工具/long_term_skeleton_review.py", "long_term_skeleton_review.py"),
            file_pair("部署工具/runtime_data_reset.py", "runtime_data_reset.py"),
            file_pair("部署工具/research_store_export.py", "research_store_export.py"),
            file_pair("部署工具/research_store_query.py", "research_store_query.py"),
            file_pair("部署工具/research_review_dashboard.py", "research_review_dashboard.py"),
            file_pair("部署工具/strategy_truth_ledger.py", "strategy_truth_ledger.py"),
            file_pair("部署工具/sentinel_quality_review.py", "sentinel_quality_review.py"),
            file_pair("部署工具/signal_quality_review.py", "signal_quality_review.py"),
            file_pair("部署工具/strategy_evolution_gate.py", "strategy_evolution_gate.py"),
            file_pair("部署工具/systemd/crypto-data-maintenance.service", "systemd/crypto-data-maintenance.service"),
            file_pair("部署工具/systemd/crypto-data-maintenance.timer", "systemd/crypto-data-maintenance.timer"),
            file_pair("部署工具/systemd/crypto-historical-kline-incremental.service", "systemd/crypto-historical-kline-incremental.service"),
            file_pair("部署工具/systemd/crypto-historical-kline-incremental.timer", "systemd/crypto-historical-kline-incremental.timer"),
            file_pair("部署工具/systemd/crypto-research-paper-strategy.service", "systemd/crypto-research-paper-strategy.service"),
            file_pair("部署工具/systemd/crypto-research-paper-strategy.timer", "systemd/crypto-research-paper-strategy.timer"),
            file_pair("部署工具/systemd/run_strategy_evolution_gate.sh", "run_strategy_evolution_gate.sh"),
            file_pair("research_memory/approvals/manual_actions.jsonl", "research_memory/approvals/manual_actions.jsonl"),
            file_pair("research_memory/approvals/manual_actions_latest.jsonl", "research_memory/approvals/manual_actions_latest.jsonl"),
            file_pair("research_memory/approvals/approve_full_live_A_v11_trailing_pullback_2026-05-29.json", "research_memory/approvals/approve_full_live_A_v11_trailing_pullback_2026-05-29.json"),
            file_pair("research_memory/approvals/approve_full_live_B_v16_sample_expansion_2026-05-31.json", "research_memory/approvals/approve_full_live_B_v16_sample_expansion_2026-05-31.json"),
            file_pair("research_memory/approvals/auto_upgrade_policy.json", "research_memory/approvals/auto_upgrade_policy.json"),
        ],
        "services": [],
        "post": [
            "chmod +x {root}/run_strategy_evolution_gate.sh",
            "sudo cp systemd/crypto-historical-kline-incremental.service /etc/systemd/system/crypto-historical-kline-incremental.service && sudo cp systemd/crypto-historical-kline-incremental.timer /etc/systemd/system/crypto-historical-kline-incremental.timer && sudo systemctl daemon-reload && sudo systemctl enable --now crypto-historical-kline-incremental.timer",
            "sudo cp systemd/crypto-research-paper-strategy.service /etc/systemd/system/crypto-research-paper-strategy.service && sudo cp systemd/crypto-research-paper-strategy.timer /etc/systemd/system/crypto-research-paper-strategy.timer && sudo systemctl daemon-reload && sudo systemctl enable --now crypto-research-paper-strategy.timer",
        ],
    },
}

TENCENT_COMPONENTS["all"] = {
    "files": [],
    "services": [],
    "post": [],
}
for _name, _spec in list(TENCENT_COMPONENTS.items()):
    if _name == "all":
        continue
    TENCENT_COMPONENTS["all"]["files"].extend(_spec.get("files") or [])
    TENCENT_COMPONENTS["all"]["services"].extend(_spec.get("services") or [])
    TENCENT_COMPONENTS["all"]["post"].extend(_spec.get("post") or [])

ALIYUN_COMPONENTS: dict[str, dict[str, Any]] = {
    "shadow": {
        "files": RESEARCH_CORE
        + [
            file_pair("部署工具/daily_market_review.py", "daily_market_review.py"),
            file_pair("部署工具/decision_attention.py", "decision_attention.py"),
            file_pair("部署工具/experiment_report.py", "experiment_report.py"),
            file_pair("部署工具/experiment_runner.py", "experiment_runner.py"),
            file_pair("部署工具/a_v11_rollout_review.py", "a_v11_rollout_review.py"),
            file_pair("部署工具/b_v16_rollout_review.py", "b_v16_rollout_review.py"),
            file_pair("部署工具/backtest_engine.py", "backtest_engine.py"),
            file_pair("部署工具/backtest_module.py", "backtest_module.py"),
            file_pair("部署工具/v11_historical_research_report.py", "v11_historical_research_report.py"),
            file_pair("部署工具/b_v16_historical_research_report.py", "b_v16_historical_research_report.py"),
            file_pair("部署工具/c_v14_historical_research_report.py", "c_v14_historical_research_report.py"),
            file_pair("部署工具/d_e_f_historical_research_report.py", "d_e_f_historical_research_report.py"),
            file_pair("部署工具/alpha_discovery_research_report.py", "alpha_discovery_research_report.py"),
            file_pair("部署工具/apply_research_approval.py", "apply_research_approval.py"),
            file_pair("部署工具/account_state_service.py", "account_state_service.py"),
            file_pair("部署工具/cleanup_event_store.py", "cleanup_event_store.py"),
            file_pair("部署工具/data_maintenance.py", "data_maintenance.py"),
            file_pair("部署工具/portal_dashboard.py", "portal_dashboard.py"),
            file_pair("部署工具/decision_portal.py", "decision_portal.py"),
            file_pair("部署工具/research_memory_builder.py", "research_memory_builder.py"),
            file_pair("部署工具/research_kline_features.py", "research_kline_features.py"),
            file_pair("部署工具/research_kline_backfill.py", "research_kline_backfill.py"),
            file_pair("部署工具/research_depth_backfill.py", "research_depth_backfill.py"),
            file_pair("部署工具/external_replay_data_ingest.py", "external_replay_data_ingest.py"),
            file_pair("部署工具/research_store_retention.py", "research_store_retention.py"),
            file_pair("部署工具/research_store_compaction.py", "research_store_compaction.py"),
            file_pair("部署工具/replay_feature_dataset.py", "replay_feature_dataset.py"),
            file_pair("部署工具/replay_gate_audit.py", "replay_gate_audit.py"),
            file_pair("部署工具/replay_live_parity_audit.py", "replay_live_parity_audit.py"),
            file_pair("部署工具/replay_readiness_review.py", "replay_readiness_review.py"),
            file_pair("部署工具/waiting_period_optimization.py", "waiting_period_optimization.py"),
            file_pair("部署工具/paper_exchange_runner.py", "paper_exchange_runner.py"),
            file_pair("部署工具/rollback_watch_review.py", "rollback_watch_review.py"),
            file_pair("部署工具/rollback_execution_plan.py", "rollback_execution_plan.py"),
            file_pair("部署工具/rollback_automation_guard.py", "rollback_automation_guard.py"),
            file_pair("部署工具/auto_upgrade_readiness.py", "auto_upgrade_readiness.py"),
            file_pair("部署工具/strategy_candidate_governance.py", "strategy_candidate_governance.py"),
            file_pair("部署工具/waiting_period_progress.py", "waiting_period_progress.py"),
            file_pair("部署工具/long_term_skeleton_review.py", "long_term_skeleton_review.py"),
            file_pair("部署工具/runtime_data_reset.py", "runtime_data_reset.py"),
            file_pair("部署工具/research_store_export.py", "research_store_export.py"),
            file_pair("部署工具/research_store_query.py", "research_store_query.py"),
            file_pair("部署工具/research_review_dashboard.py", "research_review_dashboard.py"),
            file_pair("部署工具/shadow_sync_from_tencent.py", "shadow_sync_from_tencent.py"),
            file_pair("部署工具/strategy_truth_ledger.py", "strategy_truth_ledger.py"),
            file_pair("部署工具/sentinel_quality_review.py", "sentinel_quality_review.py"),
            file_pair("部署工具/counterfactual_open_skips.py", "counterfactual_open_skips.py"),
            file_pair("部署工具/sync_aliyun_reports_to_tencent.py", "sync_aliyun_reports_to_tencent.py"),
            file_pair("部署工具/attention_api_server.py", "attention_api_server.py"),
            file_pair("部署工具/aliyun_analysis_refresh.sh", "aliyun_analysis_refresh.sh"),
            file_pair("部署工具/aliyun_decision_portal_refresh.sh", "aliyun_decision_portal_refresh.sh"),
            file_pair("部署工具/aliyun_shadow_review.sh", "run_shadow_review.sh"),
            file_pair("部署工具/systemd/crypto-decision-portal-refresh.service", "systemd/crypto-decision-portal-refresh.service"),
            file_pair("部署工具/systemd/crypto-decision-portal-refresh.timer", "systemd/crypto-decision-portal-refresh.timer"),
            file_pair("部署工具/systemd/crypto-attention-api.service", "systemd/crypto-attention-api.service"),
            file_pair("部署工具/signal_quality_review.py", "signal_quality_review.py"),
            file_pair("部署工具/strategy_evolution_gate.py", "strategy_evolution_gate.py"),
            file_pair("research_memory/approvals/manual_actions.jsonl", "research_memory/approvals/manual_actions.jsonl"),
            file_pair("research_memory/approvals/manual_actions_latest.jsonl", "research_memory/approvals/manual_actions_latest.jsonl"),
            file_pair("research_memory/approvals/approve_full_live_A_v11_trailing_pullback_2026-05-29.json", "research_memory/approvals/approve_full_live_A_v11_trailing_pullback_2026-05-29.json"),
            file_pair("research_memory/approvals/approve_full_live_B_v16_sample_expansion_2026-05-31.json", "research_memory/approvals/approve_full_live_B_v16_sample_expansion_2026-05-31.json"),
            file_pair("research_memory/approvals/auto_upgrade_policy.json", "research_memory/approvals/auto_upgrade_policy.json"),
        ],
        "services": ["crypto-attention-api.service"],
        "post": [
            "chmod +x {root}/aliyun_analysis_refresh.sh {root}/run_shadow_review.sh {root}/aliyun_decision_portal_refresh.sh",
            "sudo cp systemd/crypto-decision-portal-refresh.service /etc/systemd/system/crypto-decision-portal-refresh.service && sudo cp systemd/crypto-decision-portal-refresh.timer /etc/systemd/system/crypto-decision-portal-refresh.timer && sudo systemctl daemon-reload && sudo systemctl enable --now crypto-decision-portal-refresh.timer",
            "sudo cp systemd/crypto-attention-api.service /etc/systemd/system/crypto-attention-api.service && sudo systemctl daemon-reload",
        ],
    }
}
ALIYUN_COMPONENTS["all"] = ALIYUN_COMPONENTS["shadow"]


def unique_pairs(pairs: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    seen = set()
    out = []
    for local, remote in pairs:
        key = remote
        if key in seen:
            continue
        seen.add(key)
        out.append((local, remote))
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head() -> str:
    try:
        import subprocess

        proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, timeout=5)
        return proc.stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def connect(target: RemoteTarget, timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = os.environ.get(target.key_env)
    kwargs: dict[str, Any] = {}
    if key:
        kwargs["key_filename"] = str(Path(key).expanduser())
    client.connect(
        os.environ.get(target.host_env, target.default_host),
        22,
        os.environ.get(target.user_env, target.default_user),
        password=os.environ.get(target.pass_env) or None,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=True,
        allow_agent=True,
        **kwargs,
    )
    return client


def run(client: paramiko.SSHClient, command: str, timeout: int = 30, check: bool = True) -> tuple[int, str, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if check and rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\n{err[-1200:] or out[-1200:]}")
    return rc, out, err


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def component_spec(target_name: str, component: str) -> dict[str, Any]:
    table = TENCENT_COMPONENTS if target_name == "tencent" else ALIYUN_COMPONENTS
    if component not in table:
        raise SystemExit(f"Unknown component {component!r} for target {target_name}. Choices: {', '.join(sorted(table))}")
    spec = table[component]
    return {
        "files": unique_pairs(list(spec.get("files") or [])),
        "services": sorted(set(spec.get("services") or [])),
        "post": list(dict.fromkeys(spec.get("post") or [])),
    }


def release_id(component: str) -> str:
    return datetime.now(CST).strftime("%Y%m%d-%H%M%S") + f"-{component}-{git_head()}"


def build_manifest(target: RemoteTarget, component: str, rid: str, pairs: list[tuple[Path, str]], services: list[str]) -> dict[str, Any]:
    files = []
    for local, remote in pairs:
        if not local.exists():
            raise FileNotFoundError(local)
        files.append(
            {
                "local": str(local.relative_to(ROOT)),
                "remote": remote,
                "bytes": local.stat().st_size,
                "sha256": sha256(local),
            }
        )
    return {
        "release_id": rid,
        "created_at": datetime.now(CST).isoformat(),
        "target": target.name,
        "remote_root": target.root,
        "component": component,
        "git_head": git_head(),
        "files": files,
        "services": services,
    }


def upload_manifest(sftp: paramiko.SFTPClient, manifest: dict[str, Any], remote_path: str) -> None:
    temp = ROOT / "runtime" / "last_release_manifest.json"
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    sftp.put(str(temp), remote_path)


def deploy(args: argparse.Namespace) -> int:
    if args.apply and getattr(args, "dry_run", False):
        raise SystemExit("--apply and --dry-run cannot be used together")
    target = TARGETS[args.target]
    spec = component_spec(args.target, args.component)
    rid = args.release_id or release_id(args.component)
    manifest = build_manifest(target, args.component, rid, spec["files"], spec["services"])
    print(json.dumps({"action": "deploy", "target": args.target, "component": args.component, "release_id": rid, "files": len(manifest["files"]), "services": spec["services"], "dry_run": not args.apply}, ensure_ascii=False, indent=2))
    if not args.apply:
        for item in manifest["files"]:
            print(f"DRY {item['local']} -> {target.root}/{item['remote']}")
        return 0

    client = connect(target, args.timeout)
    try:
        base = f"{target.root}/releases/{rid}"
        backup = f"{base}/backup"
        run(client, f"mkdir -p {shell_quote(backup)}", timeout=args.timeout)
        sftp = client.open_sftp()
        try:
            for item in manifest["files"]:
                remote = f"{target.root}/{item['remote']}"
                remote_dir = posixpath.dirname(remote)
                backup_path = f"{backup}/{item['remote']}"
                backup_dir = posixpath.dirname(backup_path)
                run(
                    client,
                    (
                        f"mkdir -p {shell_quote(remote_dir)} {shell_quote(backup_dir)}; "
                        f"if [ -f {shell_quote(remote)} ]; then cp -a {shell_quote(remote)} {shell_quote(backup_path)}; "
                        f"echo yes; else echo no; fi"
                    ),
                    timeout=args.timeout,
                )
                local = ROOT / item["local"]
                sftp.put(str(local), remote)
                print(f"UPLOADED {item['local']} -> {remote}")
            upload_manifest(sftp, manifest, f"{base}/manifest.json")
        finally:
            sftp.close()

        py_files = [f"{target.root}/{item['remote']}" for item in manifest["files"] if item["remote"].endswith(".py")]
        if py_files:
            compile_script = (
                "import pathlib, sys\n"
                "failed=[]\n"
                "for p in sys.argv[1:]:\n"
                "    try:\n"
                "        src=pathlib.Path(p).read_text(encoding='utf-8')\n"
                "        compile(src, p, 'exec')\n"
                "    except Exception as exc:\n"
                "        failed.append(f'{p}: {exc}')\n"
                "if failed:\n"
                "    print('\\n'.join(failed), file=sys.stderr)\n"
                "    raise SystemExit(1)\n"
            )
            run(
                client,
                f"cd {shell_quote(target.root)} && {shell_quote(target.python)} -c {shell_quote(compile_script)} "
                + " ".join(shell_quote(p) for p in py_files),
                timeout=120,
            )

        for command in spec["post"]:
            formatted = command.format(root=target.root, python=target.python)
            if not formatted.lstrip().startswith("cd "):
                formatted = f"cd {shell_quote(target.root)} && {formatted}"
            run(client, formatted, timeout=120)

        if spec["services"] and not args.no_restart:
            run(client, "sudo systemctl restart " + " ".join(shell_quote(s) for s in spec["services"]), timeout=120)
            time.sleep(2)
            _, out, _err = run(client, "systemctl show " + " ".join(shell_quote(s) for s in spec["services"]) + " -p ActiveState -p SubState -p Result -p NRestarts --no-pager", timeout=30)
            print(out.strip())
    finally:
        client.close()
    return 0


def list_releases(args: argparse.Namespace) -> int:
    target = TARGETS[args.target]
    client = connect(target, args.timeout)
    try:
        cmd = f"if [ -d {shell_quote(target.root + '/releases')} ]; then find {shell_quote(target.root + '/releases')} -maxdepth 2 -name manifest.json -print | sort; fi"
        _, out, _err = run(client, cmd, timeout=args.timeout)
        print(out.strip() or "No releases found.")
    finally:
        client.close()
    return 0


def rollback(args: argparse.Namespace) -> int:
    target = TARGETS[args.target]
    manifest_path = f"{target.root}/releases/{args.release_id}/manifest.json"
    client = connect(target, args.timeout)
    try:
        _, raw, _err = run(client, f"cat {shell_quote(manifest_path)}", timeout=args.timeout)
        manifest = json.loads(raw)
        services = list(manifest.get("services") or [])
        print(json.dumps({"action": "rollback", "target": args.target, "release_id": args.release_id, "files": len(manifest.get("files") or []), "services": services, "dry_run": not args.apply}, ensure_ascii=False, indent=2))
        if not args.apply:
            return 0
        backup = f"{target.root}/releases/{args.release_id}/backup"
        for item in manifest.get("files") or []:
            remote = f"{target.root}/{item['remote']}"
            backup_path = f"{backup}/{item['remote']}"
            run(
                client,
                (
                    f"if [ -f {shell_quote(backup_path)} ]; then cp -a {shell_quote(backup_path)} {shell_quote(remote)}; "
                    f"else rm -f {shell_quote(remote)}; fi"
                ),
                timeout=args.timeout,
            )
            print(f"RESTORED {remote}")
        if services and not args.no_restart:
            run(client, "sudo systemctl restart " + " ".join(shell_quote(s) for s in services), timeout=120)
            time.sleep(2)
            _, out, _err = run(client, "systemctl show " + " ".join(shell_quote(s) for s in services) + " -p ActiveState -p SubState -p Result -p NRestarts --no-pager", timeout=30)
            print(out.strip())
    finally:
        client.close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy or roll back AutoTrading components with remote backups")
    sub = parser.add_subparsers(dest="command", required=True)

    deploy_p = sub.add_parser("deploy", help="Deploy a component")
    deploy_p.add_argument("--target", choices=sorted(TARGETS), default="tencent")
    deploy_p.add_argument("--component", default="portal")
    deploy_p.add_argument("--release-id")
    deploy_p.add_argument("--timeout", type=int, default=20)
    deploy_p.add_argument("--apply", action="store_true", help="Actually upload, compile, and restart")
    deploy_p.add_argument("--dry-run", action="store_true", help="Explicit dry-run mode; this is also the default")
    deploy_p.add_argument("--no-restart", action="store_true")

    list_p = sub.add_parser("list", help="List remote releases")
    list_p.add_argument("--target", choices=sorted(TARGETS), default="tencent")
    list_p.add_argument("--timeout", type=int, default=20)

    rollback_p = sub.add_parser("rollback", help="Rollback a release")
    rollback_p.add_argument("--target", choices=sorted(TARGETS), default="tencent")
    rollback_p.add_argument("--release-id", required=True)
    rollback_p.add_argument("--timeout", type=int, default=20)
    rollback_p.add_argument("--apply", action="store_true", help="Actually restore files and restart")
    rollback_p.add_argument("--no-restart", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "deploy":
        return deploy(args)
    if args.command == "list":
        return list_releases(args)
    if args.command == "rollback":
        return rollback(args)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
