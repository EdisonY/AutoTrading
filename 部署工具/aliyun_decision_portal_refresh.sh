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

TENCENT_HOST=${TENCENT_HOST:-129.226.151.144}
TENCENT_USER=${TENCENT_USER:-ubuntu}
TENCENT_ROOT=${TENCENT_ROOT:-/opt/crypto-auto-trader}
mkdir -p server_logs_tencent/runtime server_logs_tencent/reports
for rel in \
  runtime/paper_exchange_latest.json \
  runtime/research_paper_strategy_latest.json \
  runtime/historical_kline_backfill_latest.json \
  runtime/historical_kline_incremental_latest.json \
  runtime/backtest_module_latest.json \
  reports/historical_kline_backfill_latest.md \
  reports/historical_kline_incremental_latest.md \
  reports/backtest_module_latest.md
do
  tmp="server_logs_tencent/${rel}.tmp"
  if timeout 15s ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=4 -o ServerAliveCountMax=1 \
    "$TENCENT_USER@$TENCENT_HOST" "cat '$TENCENT_ROOT/$rel' 2>/dev/null" > "$tmp" && [ -s "$tmp" ]; then
    mv "$tmp" "server_logs_tencent/$rel"
    case "$rel" in
      runtime/paper_exchange_latest.json|runtime/research_paper_strategy_latest.json)
        cp "server_logs_tencent/$rel" "$rel"
        ;;
    esac
  else
    rm -f "$tmp"
  fi
done

$PYTHON decision_attention.py || echo "[WARN] attention ledger failed"
$PYTHON long_term_skeleton_review.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] long-term skeleton review failed"
$PYTHON waiting_period_optimization.py --root $REMOTE_DIR --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] waiting-period optimization failed"
$PYTHON rollback_execution_plan.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --attention-json $REMOTE_DIR/research_memory/attention/open_items.json || echo "[WARN] rollback execution plan failed"
$PYTHON rollback_automation_guard.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] rollback automation guard failed"
$PYTHON auto_upgrade_readiness.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] auto upgrade readiness failed"
$PYTHON strategy_candidate_governance.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports || echo "[WARN] strategy candidate governance failed"
$PYTHON waiting_period_progress.py --runtime-dir $REMOTE_DIR/runtime --reports-dir $REMOTE_DIR/reports --policy-json $REMOTE_DIR/research_memory/approvals/auto_upgrade_policy.json || echo "[WARN] waiting-period progress failed"
$PYTHON backtest_module.py --root $REMOTE_DIR --refresh || echo "[WARN] backtest module status failed"
$PYTHON portal_dashboard.py --out-dir $REMOTE_DIR/reports || echo "[WARN] portal generation failed"
$PYTHON decision_portal.py --out-dir $REMOTE_DIR/reports || echo "[WARN] decision portal generation failed"

timeout 75s $PYTHON sync_aliyun_reports_to_tencent.py \
  --priority-tar-only \
  --max-file-mb 1.2 \
  --file-timeout 20 \
  --max-errors 4 \
  --retries 1 || echo "[WARN] reverse sync failed or timed out"

echo "=== [$(date)] Decision portal refresh complete ==="
