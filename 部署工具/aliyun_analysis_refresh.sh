#!/bin/bash
# Aliyun lightweight analysis refresh - runs every 2 hours
# Syncs latest data from Tencent, runs analysis, generates portal, syncs back.
# NOTE: no 'set -e' — each step is independent, failures must not block the pipeline.
cd /opt/crypto-shadow-lab
export PYTHONIOENCODING=utf-8

PYTHON=/root/miniconda3/bin/python3
REMOTE_DIR=/opt/crypto-shadow-lab
SYNC_TIMEOUT=180  # hourly portal refresh must stay bounded
REFRESH_DAYS=1

echo "=== [$(date)] Analysis refresh start ==="

echo "--- Step 1: Sync latest data from Tencent (timeout ${SYNC_TIMEOUT}s) ---"
timeout $SYNC_TIMEOUT $PYTHON shadow_sync_from_tencent.py \
  --days $REFRESH_DAYS \
  --sqlite-days $REFRESH_DAYS \
  --sentinel-limit 5000 \
  --account-limit 500 \
  --max-age-hours 8 || echo "[WARN] Sync failed or timed out, continuing with local data"

echo "--- Step 1.5: Strategy Truth Ledger ---"
$PYTHON strategy_truth_ledger.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] truth ledger failed"

echo "--- Step 1.6: Sentinel Quality Review ---"
$PYTHON sentinel_quality_review.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] sentinel review failed"

echo "--- Step 1.7: Research store export/query ---"
$PYTHON research_store_export.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --out-dir $REMOTE_DIR/research_store --days $REFRESH_DAYS --format parquet || echo "[WARN] research store export failed"
$PYTHON research_kline_features.py --cache-dir $REMOTE_DIR/server_logs_tencent/runtime/kline_cache --out-dir $REMOTE_DIR/research_store --days $REFRESH_DAYS --format parquet || echo "[WARN] kline feature export failed"
$PYTHON research_kline_backfill.py --store $REMOTE_DIR/research_store --queue-db $REMOTE_DIR/runtime/binance_api_queue.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --target-days 30 --format parquet || echo "[WARN] kline backfill plan failed"
$PYTHON research_depth_backfill.py --store $REMOTE_DIR/research_store --queue-db $REMOTE_DIR/runtime/binance_api_queue.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --format parquet || echo "[WARN] depth snapshot plan failed"
$PYTHON research_store_retention.py --store $REMOTE_DIR/research_store --archive-dir $REMOTE_DIR/research_store_archive --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --format parquet || echo "[WARN] research store retention plan failed"
$PYTHON research_store_compaction.py --store $REMOTE_DIR/research_store --backup-dir $REMOTE_DIR/research_store_compaction_backup --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --format parquet || echo "[WARN] research store compaction plan failed"
$PYTHON research_store_query.py --store $REMOTE_DIR/research_store --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --days $REFRESH_DAYS --format parquet || echo "[WARN] research store query failed"
$PYTHON replay_feature_dataset.py --store $REMOTE_DIR/research_store --out-dir $REMOTE_DIR/research_store --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --days $REFRESH_DAYS --format parquet || echo "[WARN] replay feature dataset failed"
$PYTHON replay_gate_audit.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --days $REFRESH_DAYS || echo "[WARN] replay gate audit failed"
$PYTHON replay_live_parity_audit.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --days $REFRESH_DAYS || echo "[WARN] replay/live parity audit failed"
$PYTHON a_v11_rollout_review.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] A/v11 rollout review failed"
$PYTHON b_v16_rollout_review.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] B/v16 rollout review failed"
$PYTHON replay_readiness_review.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] replay readiness review failed"

echo "--- Step 2: Counterfactual evaluation ---"
$PYTHON counterfactual_open_skips.py --root $REMOTE_DIR --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 || echo "[WARN] counterfactual failed"

echo "--- Step 3: Strategy evolution gate ---"
$PYTHON strategy_evolution_gate.py --memory-dir $REMOTE_DIR/research_memory --experiments-dir $REMOTE_DIR/experiments --reports-dir $REMOTE_DIR/reports --runtime-dir $REMOTE_DIR/runtime || echo "[WARN] evolution gate failed"
$PYTHON rollback_watch_review.py --evolution-json $REMOTE_DIR/runtime/strategy_evolution_latest.json --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] rollback watch review failed"
$PYTHON rollback_execution_plan.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] rollback execution plan failed"

echo "--- Step 4: Decision attention ledger ---"
$PYTHON decision_attention.py || echo "[WARN] attention ledger failed"

echo "--- Step 5: Portal dashboard ---"
$PYTHON portal_dashboard.py --out-dir $REMOTE_DIR/reports || echo "[WARN] portal generation failed"

echo "--- Step 6: Reverse sync reports to Tencent ---"
timeout 180s $PYTHON sync_aliyun_reports_to_tencent.py || echo "[WARN] reverse sync failed or timed out"

echo "=== [$(date)] Analysis refresh complete ==="
