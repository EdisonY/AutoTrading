#!/bin/bash
set -euo pipefail
cd /opt/crypto-shadow-lab
export PYTHONIOENCODING=utf-8

PYTHON=/root/miniconda3/bin/python3
REMOTE_DIR=/opt/crypto-shadow-lab

echo "=== [$(date)] Shadow review start ==="

echo "--- Step 1: Sync data from Tencent ---"
if ! timeout 300s $PYTHON shadow_sync_from_tencent.py --days 3 --max-age-hours 8; then
  echo "--- Step 1 fallback: bounded 1-day sync after 3-day sync timeout/failure ---"
  timeout 180s $PYTHON shadow_sync_from_tencent.py \
    --days 1 \
    --sqlite-days 1 \
    --sentinel-limit 5000 \
    --account-limit 500 \
    --max-age-hours 8
fi

echo "--- Step 1.5: Strategy Truth Ledger ---"
$PYTHON strategy_truth_ledger.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || true

echo "--- Step 1.6: Sentinel Quality Review ---"
$PYTHON sentinel_quality_review.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || true

echo "--- Step 1.7: Research store export/query ---"
$PYTHON research_store_export.py --db $REMOTE_DIR/server_logs_tencent/runtime/event_store.sqlite3 --out-dir $REMOTE_DIR/research_store --days 3 --format parquet || true
$PYTHON research_store_query.py --store $REMOTE_DIR/research_store --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --days 3 --format parquet || true

echo "--- Step 1.8: Daily market review report ---"
if ! timeout 900s $PYTHON daily_market_review.py --data-root $REMOTE_DIR/server_logs_tencent --top 15; then
  echo "--- Step 1.8 fallback: generate daily market review on Tencent and pull reports ---"
  TENCENT_HOST=${TENCENT_HOST:-129.226.151.144}
  TENCENT_USER=${TENCENT_USER:-ubuntu}
  timeout 930s ssh -o BatchMode=yes -o ConnectTimeout=12 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    "$TENCENT_USER@$TENCENT_HOST" \
    "cd /opt/crypto-auto-trader && timeout 900s python3 daily_market_review.py --data-root /opt/crypto-auto-trader --top 15" || true
  timeout 120s scp -o BatchMode=yes -o ConnectTimeout=12 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    "$TENCENT_USER@$TENCENT_HOST:/opt/crypto-auto-trader/reports/market_review_latest.md" "$REMOTE_DIR/reports/market_review_latest.md" || true
  timeout 120s scp -o BatchMode=yes -o ConnectTimeout=12 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    "$TENCENT_USER@$TENCENT_HOST:/opt/crypto-auto-trader/reports/market_review_latest.html" "$REMOTE_DIR/reports/market_review_latest.html" || true
  timeout 120s scp -o BatchMode=yes -o ConnectTimeout=12 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    "$TENCENT_USER@$TENCENT_HOST:/opt/crypto-auto-trader/reports/market_snapshot_latest.json" "$REMOTE_DIR/reports/market_snapshot_latest.json" || true
fi

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
timeout 180s $PYTHON sync_aliyun_reports_to_tencent.py || true

echo "=== [$(date)] Shadow review complete ==="
