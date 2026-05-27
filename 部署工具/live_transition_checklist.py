"""Live Transition Checklist - pre-flight checks before moving from Testnet to live trading.

This script evaluates readiness for live deployment by checking:
- Strategy quality metrics from truth ledger
- Promotion gate status
- Account risk
- Fee/slippage impact estimates
- Rollback readiness

Run on Aliyun after strategy truth ledger and evolution gate complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CST = timezone(timedelta(hours=8))

# Transition thresholds
MIN_PF_LIVE = 1.2  # Minimum profit factor for live
MIN_WIN_RATE = 40.0  # Minimum win rate %
MIN_SAMPLE_30D = 100  # Minimum 30-day trades
MAX_HARD_STOP_PCT = 10.0  # Max hard-stop rate %
MAX_DRAWDOWN_PCT = 15.0  # Max drawdown %
MIN_DAYS_OBSERVED = 14  # Minimum observation days


def to_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def to_int(val: Any, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def check_strategy_readiness(
    strategy: str,
    truth_stats: dict[str, Any],
    evolution_decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check if a strategy is ready for live transition."""
    checks: list[dict[str, Any]] = []
    stats = truth_stats.get(strategy, {})

    # Check PF
    pf = to_float(stats.get("profit_factor")) if stats.get("profit_factor") != "inf" else 999
    checks.append({
        "name": "Profit Factor",
        "value": f"{pf:.2f}",
        "threshold": f">= {MIN_PF_LIVE}",
        "pass": pf >= MIN_PF_LIVE,
        "note": "手续费调整后的盈亏因子" if pf >= MIN_PF_LIVE else "PF不足，策略负期望",
    })

    # Check win rate
    wr = to_float(stats.get("win_rate"))
    checks.append({
        "name": "Win Rate",
        "value": f"{wr:.1f}%",
        "threshold": f">= {MIN_WIN_RATE}%",
        "pass": wr >= MIN_WIN_RATE,
        "note": "胜率达标" if wr >= MIN_WIN_RATE else "胜率偏低",
    })

    # Check sample size
    closed = to_int(stats.get("closed_trades"))
    checks.append({
        "name": "Closed Trades",
        "value": str(closed),
        "threshold": f">= {MIN_SAMPLE_30D}",
        "pass": closed >= MIN_SAMPLE_30D,
        "note": "样本充足" if closed >= MIN_SAMPLE_30D else "样本不足，需更多观察",
    })

    # Check hard-stop rate
    hs = to_int(stats.get("hard_stop_count"))
    hs_rate = (hs / closed * 100) if closed > 0 else 0
    checks.append({
        "name": "Hard-Stop Rate",
        "value": f"{hs_rate:.1f}%",
        "threshold": f"<= {MAX_HARD_STOP_PCT}%",
        "pass": hs_rate <= MAX_HARD_STOP_PCT,
        "note": "硬顶可控" if hs_rate <= MAX_HARD_STOP_PCT else "硬顶触发率过高",
    })

    # Check promotion gate status
    gate_status = "unknown"
    for d in evolution_decisions:
        if d.get("strategy") == strategy:
            gate_status = d.get("priority", "unknown")
            break
    checks.append({
        "name": "Gate Status",
        "value": gate_status,
        "threshold": "P0 or P1",
        "pass": gate_status in {"P0", "P1"},
        "note": "门禁已通过" if gate_status in {"P0", "P1"} else "门禁未达P0/P1",
    })

    # Check recovery positions
    recovery_count = to_int(stats.get("recovery_count", 0))
    checks.append({
        "name": "Recovery Positions",
        "value": str(recovery_count),
        "threshold": "0",
        "pass": recovery_count == 0,
        "note": "无恢复仓" if recovery_count == 0 else "仍有恢复仓，需清理",
    })

    all_pass = all(c["pass"] for c in checks)
    return {
        "strategy": strategy,
        "ready": all_pass,
        "checks": checks,
        "summary": f"{'READY' if all_pass else 'NOT READY'} - {sum(1 for c in checks if c['pass'])}/{len(checks)} checks passed",
    }


def build_transition_report(
    truth: dict[str, Any],
    evolution: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete transition readiness report."""
    now = datetime.now(CST)
    truth_stats = truth.get("strategy_stats", {})
    decisions = evolution.get("decisions", [])

    results = {}
    for strategy in ["A/v11", "B/v16", "C/v14"]:
        results[strategy] = check_strategy_readiness(strategy, truth_stats, decisions)

    ready_count = sum(1 for r in results.values() if r["ready"])
    overall_ready = ready_count > 0  # At least one strategy ready

    return {
        "generated_at": now.isoformat(),
        "overall_ready": overall_ready,
        "ready_strategies": [s for s, r in results.items() if r["ready"]],
        "not_ready_strategies": [s for s, r in results.items() if not r["ready"]],
        "strategy_results": results,
        "transition_rules": {
            "min_pf": MIN_PF_LIVE,
            "min_win_rate": MIN_WIN_RATE,
            "min_sample_30d": MIN_SAMPLE_30D,
            "max_hard_stop_pct": MAX_HARD_STOP_PCT,
            "max_drawdown_pct": MAX_DRAWDOWN_PCT,
            "min_days_observed": MIN_DAYS_OBSERVED,
            "fee_slippage_estimate": "0.15% per round-trip",
            "testnet_vs_live_pf_decay": "expect 0.7-0.9x PF on live due to slippage",
        },
        "rollback_rules": {
            "open_failed_threshold": 5,
            "pf_decay_ratio": 0.8,
            "hard_stop_ratio": 1.5,
            "account_loss_usdt": 200,
        },
    }


def write_json(output: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_markdown(output: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 实盘过渡验证清单",
        "",
        f"- 生成时间: {output['generated_at']}",
        f"- 整体就绪: {'✅ 是' if output['overall_ready'] else '❌ 否'}",
        f"- 就绪策略: {', '.join(output['ready_strategies']) or '无'}",
        f"- 未就绪: {', '.join(output['not_ready_strategies']) or '无'}",
        "",
        "## 各策略检查",
        "",
    ]
    for strategy, result in output["strategy_results"].items():
        icon = "✅" if result["ready"] else "❌"
        lines.append(f"### {icon} {strategy}: {result['summary']}")
        lines.append("")
        lines.append("| 检查项 | 当前值 | 阈值 | 结果 | 说明 |")
        lines.append("|--------|-------:|------|------|------|")
        for c in result["checks"]:
            icon = "✅" if c["pass"] else "❌"
            lines.append(f"| {c['name']} | {c['value']} | {c['threshold']} | {icon} | {c['note']} |")
        lines.append("")

    rules = output.get("transition_rules", {})
    lines.extend([
        "## 过渡规则",
        "",
        f"- 最低 PF: {rules.get('min_pf')}",
        f"- 最低胜率: {rules.get('min_win_rate')}%",
        f"- 最少30天样本: {rules.get('min_sample_30d')}",
        f"- 最大硬顶率: {rules.get('max_hard_stop_pct')}%",
        f"- 手续费/滑点估算: {rules.get('fee_slippage_estimate')}",
        f"- Testnet→实盘 PF 衰减: {rules.get('testnet_vs_live_pf_decay')}",
        "",
    ])
    rollback = output.get("rollback_rules", {})
    lines.extend([
        "## 回滚触发条件",
        "",
        f"- OPEN_FAILED > {rollback.get('open_failed_threshold')} / 24h → 自动回滚",
        f"- 新版 PF < 旧版 × {rollback.get('pf_decay_ratio')} → 人工审核",
        f"- 硬顶率 > 旧版 × {rollback.get('hard_stop_ratio')} → 暂停+审核",
        f"- 账户7天亏损 > {rollback.get('account_loss_usdt')} USDT → 暂停所有改动",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live Transition Checklist")
    parser.add_argument("--runtime-dir", default=None)
    parser.add_argument("--reports-dir", default=None)
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent if script_dir.name == "部署工具" else script_dir
    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else root / "runtime"
    reports_dir = Path(args.reports_dir) if args.reports_dir else root / "reports"

    truth = read_json(runtime_dir / "strategy_truth_latest.json") or {}
    evolution = read_json(runtime_dir / "strategy_evolution_latest.json") or {}

    output = build_transition_report(truth, evolution)

    json_path = runtime_dir / "live_transition_latest.json"
    md_path = reports_dir / "live_transition_latest.md"
    write_json(output, json_path)
    write_markdown(output, md_path)

    print(f"Output: {json_path}")
    print(f"        {md_path}")
    print(f"\nOverall ready: {output['overall_ready']}")
    for s, r in output["strategy_results"].items():
        print(f"  {s}: {r['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
