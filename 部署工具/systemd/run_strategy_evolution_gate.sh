#!/bin/bash
set -euo pipefail
cd /opt/crypto-auto-trader
export PYTHONIOENCODING=utf-8
PY=/opt/crypto-auto-trader/.venv/bin/python

if [ -f /opt/crypto-auto-trader/research_memory_builder.py ]; then
  timeout 180s "$PY" /opt/crypto-auto-trader/research_memory_builder.py \
    --data-root /opt/crypto-auto-trader \
    --out-dir /opt/crypto-auto-trader/research_memory \
    --top 20 \
    --min-abs-move 8 \
    --market-limit 80 || echo "WARN research_memory_builder failed; using existing candidates if present"
fi

"$PY" /opt/crypto-auto-trader/experiment_runner.py \
  --data-root /opt/crypto-auto-trader \
  --out-dir /opt/crypto-auto-trader/experiments \
  --memory-dir /opt/crypto-auto-trader/research_memory \
  --days 3 \
  --windows 3,7,14,30

if [ -f /opt/crypto-auto-trader/experiment_report.py ]; then
  "$PY" /opt/crypto-auto-trader/experiment_report.py \
    --results /opt/crypto-auto-trader/experiments/results/latest.jsonl \
    --out-dir /opt/crypto-auto-trader/reports || true
fi

if [ -f /opt/crypto-auto-trader/research_review_dashboard.py ]; then
  "$PY" /opt/crypto-auto-trader/research_review_dashboard.py \
    --memory-dir /opt/crypto-auto-trader/research_memory \
    --experiment-results /opt/crypto-auto-trader/experiments/results/latest.jsonl \
    --out-dir /opt/crypto-auto-trader/reports || true
fi

"$PY" /opt/crypto-auto-trader/strategy_evolution_gate.py \
  --memory-dir /opt/crypto-auto-trader/research_memory \
  --experiments-dir /opt/crypto-auto-trader/experiments \
  --reports-dir /opt/crypto-auto-trader/reports \
  --runtime-dir /opt/crypto-auto-trader/runtime

"$PY" /opt/crypto-auto-trader/decision_attention.py
"$PY" /opt/crypto-auto-trader/portal_dashboard.py --out-dir /opt/crypto-auto-trader/reports
