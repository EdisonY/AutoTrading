"""Stress review for indicator-factory candidates.

Local research only. Reads the local historical Kline warehouse and the
indicator-factory SQLite result DB, replays one candidate with all trades, then
renders pressure-test, symbol, month, and simple regime breakdowns.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import d_e_f_historical_research_report as shared
import indicator_factory as factory


CST = timezone(timedelta(hours=8))
DB_PATH = ROOT / "research_lab" / "indicator_factory" / "results.sqlite"
RUNTIME_JSON = ROOT / "runtime" / "indicator_candidate_stress_latest.json"
REPORT_HTML = ROOT / "research_lab" / "indicator_factory" / "indicator_candidate_stress_latest.html"
DEFAULT_EXTRA_COST_BPS = [0.0, 2.0, 5.0, 10.0, 20.0]


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        result = float(value)
        if result != result:
            return default
        return result
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def load_candidate(db_path: Path, run_id: str | None, combo_id: str | None, interval: str | None) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = ["decision='singularity_candidate'"]
    args: list[Any] = []
    if run_id:
        where.append("run_id=?")
        args.append(run_id)
    if combo_id:
        where.append("combo_id=?")
        args.append(combo_id)
    if interval:
        where.append("interval=?")
        args.append(interval)
    row = conn.execute(
        f"""
        select run_id, combo_id, combo_name, interval, indicator_ids_json,
               full_json, train_json, validation_json, test_json,
               robust_score, decision, anti_fit_reasons_json, params_json
        from combo_result_details
        where {' and '.join(where)}
        order by robust_score desc
        limit 1
        """,
        args,
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit("No singularity_candidate found for requested filters.")
    return {
        "run_id": row["run_id"],
        "combo_id": row["combo_id"],
        "combo_name": row["combo_name"],
        "interval": row["interval"],
        "indicator_ids": json.loads(row["indicator_ids_json"] or "[]"),
        "recorded": {
            "full": json.loads(row["full_json"] or "{}"),
            "train": json.loads(row["train_json"] or "{}"),
            "validation": json.loads(row["validation_json"] or "{}"),
            "test": json.loads(row["test_json"] or "{}"),
            "robust_score": safe_float(row["robust_score"]),
            "decision": row["decision"],
            "anti_fit_reasons": json.loads(row["anti_fit_reasons_json"] or "[]"),
        },
        "params": json.loads(row["params_json"] or "{}"),
    }


def find_combo(indicator_ids: list[str]) -> dict[str, Any]:
    ids = list(indicator_ids)
    for combo in factory.generate_combos(factory.indicator_registry(), 2, 4):
        if combo["indicator_ids"] == ids:
            return combo
    combo_id = f"manual-{factory.stable_id(ids)}"
    return {"combo_id": combo_id, "name": " + ".join(ids), "indicator_ids": ids}


def replay_trades(root: Path, candidate: dict[str, Any], days: int) -> dict[str, list[dict[str, Any]]]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    symbols = shared.universe_symbols(root, None)
    interval = candidate["interval"]
    loaded = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
    combo = find_combo(candidate["indicator_ids"])
    combo["combo_id"] = candidate["combo_id"]
    combo["name"] = candidate["combo_name"]
    common = (candidate.get("params") or {}).get("common") or {
        "atr_stop_multiplier": 1.8,
        "take_profit_atr": 3.2,
        "trailing_pullback_atr": 1.0,
        "trailing_activation_atr": 0.8,
        "max_hold_bars": 24,
        "trade_size_usdt": 100.0,
        "leverage": 2.0,
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for split in [None, "train", "validation", "test"]:
        trades: list[dict[str, Any]] = []
        for symbol, bars in loaded.items():
            use_bars = shared.split_sequence(bars, split)
            trades.extend(factory.simulate_combo_symbol(symbol, interval, use_bars, combo, common))
        out["full" if split is None else split] = sorted(trades, key=lambda item: str(item.get("exit_ts") or ""))
    return out


def notional_roundtrip(trade: dict[str, Any]) -> float:
    qty = abs(safe_float(trade.get("quantity") or trade.get("requested_quantity")))
    entry = abs(safe_float(trade.get("entry_price")))
    exit_price = abs(safe_float(trade.get("exit_price")))
    return qty * (entry + exit_price)


def summarize_with_extra_cost(trades: list[dict[str, Any]], extra_bps: float) -> dict[str, Any]:
    stressed: list[dict[str, Any]] = []
    for trade in trades:
        item = dict(trade)
        extra_cost = notional_roundtrip(trade) * extra_bps / 10_000.0
        item["net_pnl_usdt"] = safe_float(trade.get("net_pnl_usdt")) - extra_cost
        item["extra_cost_usdt"] = extra_cost
        stressed.append(item)
    summary, _charts = shared.summarize_trades(stressed)
    summary["extra_cost_bps_roundtrip"] = extra_bps
    summary["extra_cost_usdt"] = round(sum(safe_float(t.get("extra_cost_usdt")) for t in stressed), 6)
    return summary


def group_summary(trades: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(key_fn(trade))].append(trade)
    rows = []
    for key, items in groups.items():
        summary, _charts = shared.summarize_trades(items)
        rows.append({"key": key, **summary})
    rows.sort(key=lambda item: safe_float(item.get("net_profit_usdt")), reverse=True)
    return rows


def regime_key(trade: dict[str, Any]) -> str:
    atr = safe_float(trade.get("atr_pct"))
    bars = safe_int(trade.get("bars_held"))
    if atr >= 3.0:
        vol = "high_vol"
    elif atr >= 1.2:
        vol = "mid_vol"
    else:
        vol = "low_vol"
    if bars >= 18:
        hold = "long_hold"
    elif bars >= 6:
        hold = "mid_hold"
    else:
        hold = "short_hold"
    return f"{vol}/{hold}"


def side_key(trade: dict[str, Any]) -> str:
    return str(trade.get("side") or "unknown")


def month_key(trade: dict[str, Any]) -> str:
    ts = str(trade.get("exit_ts") or trade.get("entry_ts") or "")[:7]
    return ts or "unknown"


def build_payload(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    candidate = load_candidate(DB_PATH, args.run_id, args.combo_id, args.interval)
    trades_by_split = replay_trades(root, candidate, args.days)
    summaries = {name: shared.summarize_trades(trades)[0] for name, trades in trades_by_split.items()}
    full_trades = trades_by_split["full"]
    stress = [summarize_with_extra_cost(full_trades, bps) for bps in args.extra_cost_bps]
    payload = {
        "generated_at": now_iso(),
        "module": "indicator_candidate_stress_review",
        "status": "completed",
        "candidate": candidate,
        "days": args.days,
        "summaries": summaries,
        "stress_tests": stress,
        "breakdowns": {
            "by_symbol": group_summary(full_trades, lambda t: t.get("symbol") or "unknown"),
            "by_side": group_summary(full_trades, side_key),
            "by_exit_reason": group_summary(full_trades, lambda t: t.get("exit_reason") or "unknown"),
            "by_month": group_summary(full_trades, month_key),
            "by_trade_regime": group_summary(full_trades, regime_key),
        },
        "safety": {
            "local_only": True,
            "binance_requests_enabled": False,
            "cloud_compute": False,
            "live_config_mutation": False,
            "paper_or_real_orders": False,
            "automatic_tuning_allowed": False,
            "automatic_rollback_allowed": False,
            "automatic_upgrade_allowed": False,
        },
        "paths": {
            "json": str(RUNTIME_JSON),
            "html": str(REPORT_HTML),
            "source_db": str(DB_PATH),
        },
    }
    return payload


def fmt_num(value: Any, digits: int = 2) -> str:
    return f"{safe_float(value):.{digits}f}"


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int | None = None) -> str:
    use_rows = rows[:limit] if limit else rows
    head = "".join(f"<th>{escape(label)}</th>" for _key, label in columns)
    body = []
    for row in use_rows:
        cells = []
        for key, _label in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                text = fmt_num(value)
            else:
                text = str(value)
            cells.append(f"<td>{escape(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>empty</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def verdict(payload: dict[str, Any]) -> tuple[str, list[str]]:
    full = payload["summaries"]["full"]
    validation = payload["summaries"]["validation"]
    test = payload["summaries"]["test"]
    stress10 = next((row for row in payload["stress_tests"] if safe_float(row.get("extra_cost_bps_roundtrip")) == 10.0), {})
    by_symbol = payload["breakdowns"]["by_symbol"]
    positive_symbols = sum(1 for row in by_symbol if safe_float(row.get("net_profit_usdt")) > 0)
    reasons = []
    if safe_float(stress10.get("net_profit_usdt")) <= 0:
        reasons.append("10bps extra roundtrip cost wipes out edge")
    if positive_symbols < max(5, len(by_symbol) // 3):
        reasons.append("profit concentrated in too few symbols")
    if safe_int(validation.get("trades")) < 30 or safe_int(test.get("trades")) < 30:
        reasons.append("validation/test sample still thin")
    if safe_float(test.get("profit_factor")) > safe_float(full.get("profit_factor")) * 1.8:
        reasons.append("test PF much stronger than full sample; possible favorable recent regime")
    if reasons:
        return "research_watch_only", reasons
    return "candidate_for_deeper_parameter_free_validation", ["passes first pressure screen; still no live/paper approval"]


def render_html(payload: dict[str, Any]) -> str:
    candidate = payload["candidate"]
    decision, reasons = verdict(payload)
    summary_cols = [
        ("key", "分段"),
        ("net_profit_usdt", "净利"),
        ("profit_factor", "PF"),
        ("max_drawdown_pct", "回撤%"),
        ("win_rate_pct", "胜率%"),
        ("trades", "交易"),
    ]
    summaries = [{"key": key, **value} for key, value in payload["summaries"].items()]
    stress_cols = [
        ("extra_cost_bps_roundtrip", "额外成本bps"),
        ("net_profit_usdt", "净利"),
        ("profit_factor", "PF"),
        ("max_drawdown_pct", "回撤%"),
        ("win_rate_pct", "胜率%"),
        ("trades", "交易"),
        ("extra_cost_usdt", "额外成本"),
    ]
    breakdown_cols = [
        ("key", "分组"),
        ("net_profit_usdt", "净利"),
        ("profit_factor", "PF"),
        ("max_drawdown_pct", "回撤%"),
        ("win_rate_pct", "胜率%"),
        ("trades", "交易"),
    ]
    reason_html = "".join(f"<li>{escape(item)}</li>" for item in reasons)
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>Indicator Candidate Stress Review</title>
<style>
:root{{--bg:#091018;--panel:#111b26;--line:#263545;--text:#e8f1f8;--muted:#91a2b1;--cyan:#4cc9f0;--gold:#f4c95d;--bad:#ff6b6b;--good:#63d297}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:Segoe UI,Arial,sans-serif}}
main{{max-width:1280px;margin:0 auto;padding:28px}}
.hero{{border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:18px}}
h1{{font-size:28px;margin:0 0 8px}} h2{{font-size:18px;margin:22px 0 10px}} p{{color:var(--muted);line-height:1.6}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}} .panel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;overflow:auto}}
.verdict{{display:inline-block;padding:6px 10px;border-radius:6px;background:#182635;color:var(--gold);font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid var(--line);padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:var(--muted);font-weight:600}}
code{{color:var(--gold)}} ul{{color:var(--muted)}} .small{{font-size:12px;color:var(--muted)}}
</style>
</head>
<body><main>
<section class="hero">
<h1>候选压力复核</h1>
<p><b>{escape(candidate['combo_name'])}</b> / <code>{escape(candidate['combo_id'])}</code> / {escape(candidate['interval'])}</p>
<p class="verdict">{escape(decision)}</p>
<ul>{reason_html}</ul>
<p class="small">Local only. No Binance, no cloud, no live mutation, no orders, no auto tuning/rollback/upgrade.</p>
</section>
<section class="grid">
<div class="panel"><h2>Train / Validation / Test</h2>{table(summaries, summary_cols)}</div>
<div class="panel"><h2>成本压力</h2>{table(payload['stress_tests'], stress_cols)}</div>
<div class="panel"><h2>分币种</h2>{table(payload['breakdowns']['by_symbol'], breakdown_cols)}</div>
<div class="panel"><h2>月份</h2>{table(payload['breakdowns']['by_month'], breakdown_cols)}</div>
<div class="panel"><h2>方向</h2>{table(payload['breakdowns']['by_side'], breakdown_cols)}</div>
<div class="panel"><h2>退出原因</h2>{table(payload['breakdowns']['by_exit_reason'], breakdown_cols)}</div>
<div class="panel"><h2>交易状态粗分</h2>{table(payload['breakdowns']['by_trade_regime'], breakdown_cols)}</div>
<div class="panel"><h2>路径</h2><p>JSON: <code>{escape(payload['paths']['json'])}</code></p><p>HTML: <code>{escape(payload['paths']['html'])}</code></p></div>
</section>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay and stress-test one indicator-factory candidate.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--combo-id", default=None)
    parser.add_argument("--interval", default=None)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--extra-cost-bps", type=float, nargs="*", default=DEFAULT_EXTRA_COST_BPS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = build_payload(args.root, args)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    decision, reasons = verdict(payload)
    print(json.dumps({
        "status": "completed",
        "decision": decision,
        "reasons": reasons,
        "candidate": payload["candidate"]["combo_id"],
        "interval": payload["candidate"]["interval"],
        "full": payload["summaries"]["full"],
        "html": str(REPORT_HTML),
        "json": str(RUNTIME_JSON),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
