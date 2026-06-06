#!/bin/bash
# Aliyun lightweight decision-portal refresh.
# Reads Tencent mirrors over SSH only; does not submit Binance queue work.
cd /opt/crypto-shadow-lab
export PYTHONIOENCODING=utf-8

PYTHON=/root/miniconda3/bin/python3
REMOTE_DIR=/opt/crypto-shadow-lab
SYNC_TIMEOUT=90

echo "=== [$(date)] Decision portal refresh start ==="

timeout $SYNC_TIMEOUT $PYTHON shadow_sync_from_tencent.py \
  --days 1 \
  --sqlite-days 1 \
  --sentinel-limit 1200 \
  --account-limit 120 \
  --kline-limit 40 \
  --log-tail 80 \
  --max-age-hours 2 || echo "[WARN] tiny sync failed or timed out; using local mirror"

$PYTHON decision_attention.py || echo "[WARN] attention ledger failed"
$PYTHON long_term_skeleton_review.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] long-term skeleton review failed"
$PYTHON waiting_period_optimization.py --root $REMOTE_DIR --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] waiting-period optimization failed"
$PYTHON paper_exchange_runner.py --root $REMOTE_DIR --target-per-strategy 5 --margin-usdt 100 --leverage 4 || echo "[WARN] paper exchange refresh failed"
$PYTHON portal_dashboard.py --out-dir $REMOTE_DIR/reports || echo "[WARN] portal generation failed"
$PYTHON decision_portal.py --out-dir $REMOTE_DIR/reports || echo "[WARN] decision portal generation failed"

timeout 75s $PYTHON sync_aliyun_reports_to_tencent.py \
  --max-file-mb 1.2 \
  --file-timeout 20 \
  --max-errors 4 \
  --retries 1 || echo "[WARN] reverse sync failed or timed out"

echo "=== [$(date)] Decision portal refresh complete ==="
