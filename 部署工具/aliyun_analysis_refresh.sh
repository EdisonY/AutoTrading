#!/bin/bash
# Aliyun lightweight analysis refresh - runs every 2 hours
# Syncs latest data from Tencent, runs analysis, generates portal, syncs back.
# NOTE: no 'set -e' — each step is independent, failures must not block the pipeline.
cd /opt/crypto-shadow-lab
export PYTHONIOENCODING=utf-8

PYTHON=/root/miniconda3/bin/python3
REMOTE_DIR=/opt/crypto-shadow-lab
SYNC_TIMEOUT=600  # 10 minutes max for sync

echo "=== [$(date)] Analysis refresh start ==="

echo "--- Step 1: Sync latest data from Tencent (timeout ${SYNC_TIMEOUT}s) ---"
timeout $SYNC_TIMEOUT $PYTHON shadow_sync_from_tencent.py --days 3 || echo "[WARN] Sync failed or timed out, continuing with local data"

echo "--- Step 1.5: Strategy Truth Ledger ---"
$PYTHON strategy_truth_ledger.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] truth ledger failed"

echo "--- Step 1.6: Sentinel Quality Review ---"
$PYTHON sentinel_quality_review.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] sentinel review failed"

echo "--- Step 1.7: Research store export/query ---"
$PYTHON research_store_export.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --out-dir $REMOTE_DIR/research_store --days 3 --format parquet || echo "[WARN] research store export failed"
$PYTHON research_store_query.py --store $REMOTE_DIR/research_store --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --days 3 --format parquet || echo "[WARN] research store query failed"

echo "--- Step 2: Counterfactual evaluation ---"
$PYTHON counterfactual_open_skips.py --root $REMOTE_DIR --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 || echo "[WARN] counterfactual failed"

echo "--- Step 3: Strategy evolution gate ---"
$PYTHON strategy_evolution_gate.py --memory-dir $REMOTE_DIR/research_memory --experiments-dir $REMOTE_DIR/experiments --reports-dir $REMOTE_DIR/reports --runtime-dir $REMOTE_DIR/runtime || echo "[WARN] evolution gate failed"

echo "--- Step 4: Decision attention ledger ---"
$PYTHON decision_attention.py || echo "[WARN] attention ledger failed"

echo "--- Step 5: Portal dashboard ---"
$PYTHON portal_dashboard.py --out-dir $REMOTE_DIR/reports || echo "[WARN] portal generation failed"

echo "--- Step 6: Reverse sync reports to Tencent ---"
timeout 180s $PYTHON sync_aliyun_reports_to_tencent.py || echo "[WARN] reverse sync failed or timed out"

echo "=== [$(date)] Analysis refresh complete ==="
