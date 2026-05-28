#!/bin/bash
set -euo pipefail
cd /opt/crypto-shadow-lab
export PYTHONIOENCODING=utf-8

PYTHON=/root/miniconda3/bin/python3
REMOTE_DIR=/opt/crypto-shadow-lab

echo "=== [$(date)] Shadow review start ==="

echo "--- Step 1: Sync data from Tencent ---"
$PYTHON shadow_sync_from_tencent.py --days 3

echo "--- Step 1.5: Strategy Truth Ledger ---"
$PYTHON strategy_truth_ledger.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || true

echo "--- Step 1.6: Sentinel Quality Review ---"
$PYTHON sentinel_quality_review.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || true

echo "--- Step 2: Build research memory ---"
timeout 180s $PYTHON research_memory_builder.py --data-root $REMOTE_DIR/server_logs_tencent --out-dir $REMOTE_DIR/research_memory --top 20 --min-abs-move 8 --market-limit 80 || true

echo "--- Step 3: Signal quality review ---"
$PYTHON signal_quality_review.py --days 3 --data-root $REMOTE_DIR/server_logs_tencent || true

echo "--- Step 4: Shadow experiments ---"
$PYTHON experiment_runner.py --data-root $REMOTE_DIR/server_logs_tencent --memory-dir $REMOTE_DIR/research_memory --days 3 --windows 3,7,14,30

echo "--- Step 5: Experiment report ---"
$PYTHON experiment_report.py --results $REMOTE_DIR/experiments/results/latest.jsonl --out-dir $REMOTE_DIR/reports

echo "--- Step 6: Counterfactual evaluation ---"
$PYTHON counterfactual_open_skips.py --root $REMOTE_DIR --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 || true

echo "--- Step 7: Strategy evolution gate ---"
$PYTHON strategy_evolution_gate.py --memory-dir $REMOTE_DIR/research_memory --experiments-dir $REMOTE_DIR/experiments --reports-dir $REMOTE_DIR/reports --runtime-dir $REMOTE_DIR/runtime || true

echo "--- Step 8: Decision attention ledger ---"
$PYTHON decision_attention.py || true

echo "--- Step 9: Research review dashboard ---"
$PYTHON research_review_dashboard.py --memory-dir $REMOTE_DIR/research_memory --experiment-results $REMOTE_DIR/experiments/results/latest.jsonl --out-dir $REMOTE_DIR/reports

echo "--- Step 10: Portal dashboard ---"
$PYTHON portal_dashboard.py --out-dir $REMOTE_DIR/reports

echo "--- Step 11: Reverse sync reports to Tencent ---"
$PYTHON sync_aliyun_reports_to_tencent.py || true

echo "=== [$(date)] Shadow review complete ==="
