#!/bin/bash
# Aliyun lightweight analysis refresh - runs every 2 hours
# Syncs latest data from Tencent, runs quick analysis, generates portal, syncs back.
set -euo pipefail
cd /opt/crypto-shadow-lab
export PYTHONIOENCODING=utf-8

PYTHON=/root/miniconda3/bin/python3
REMOTE_DIR=/opt/crypto-shadow-lab

echo "=== [$(date)] Analysis refresh start ==="

echo "--- Step 1: Sync latest data from Tencent ---"
$PYTHON shadow_sync_from_tencent.py --days 3 || { echo "FATAL: sync failed"; exit 1; }

echo "--- Step 2: Counterfactual evaluation ---"
$PYTHON counterfactual_open_skips.py --data-root $REMOTE_DIR/server_logs_tencent --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || true

echo "--- Step 3: Strategy evolution gate ---"
$PYTHON strategy_evolution_gate.py --memory-dir $REMOTE_DIR/research_memory --experiments-dir $REMOTE_DIR/experiments --reports-dir $REMOTE_DIR/reports --runtime-dir $REMOTE_DIR/runtime || true

echo "--- Step 4: Portal dashboard ---"
$PYTHON portal_dashboard.py --out-dir $REMOTE_DIR/reports || true

echo "--- Step 5: Reverse sync reports to Tencent ---"
$PYTHON sync_aliyun_reports_to_tencent.py || true

echo "=== [$(date)] Analysis refresh complete ==="
