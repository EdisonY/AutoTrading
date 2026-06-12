"""Local market-regime classifier.

Research-only tool. Reads the local historical Kline warehouse and labels each
symbol/interval with simple, inspectable market-state buckets. It does not
change live configs, call Binance, restart services, or place orders.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
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

import backtest_engine
import d_e_f_historical_research_report as shared


CST = timezone(timedelta(hours=8))
DEFAULT_INTERVALS = ["15m", "30m", "1h", "4h"]
RUNTIME_JSON = ROOT / "runtime" / "regime_classifier_latest.json"
REPORT_HTML = ROOT / "reports" / "regime_classifier_latest.html"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def safe_float(value: Any, default: float = 0.0) -> float:
    return backtest_engine.safe_float(value, default)


def pct(a: float, b: float) -> float:
    return (b - a) / a * 100.0 if a > 0 else 0.0


def sma(values: list[float], idx: int, length: int) -> float:
    if idx + 1 < length:
        return 0.0
    window = values[idx - length + 1 : idx + 1]
    return sum(window) / len(window)


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((item - mean) ** 2 for item in values) / (len(values) - 1))


def quote_volume(row: dict[str, Any]) -> float:
    return safe_float(row.get("quote_volume"), safe_float(row.get("volume")))


def volume_ratio(bars: list[dict[str, Any]], idx: int, length: int = 20) -> float:
    if idx + 1 < length:
        return 1.0
    current = quote_volume(bars[idx])
    vals = [quote_volume(row) for row in bars[idx - length + 1 : idx + 1]]
    avg = sum(vals) / len(vals) if vals else 0.0
    return current / avg if avg > 0 else 1.0


def bollinger_width_pct(closes: list[float], idx: int, length: int = 20, dev: float = 2.0) -> float:
    if idx + 1 < length:
        return 0.0
    window = closes[idx - length + 1 : idx + 1]
    mid = sum(window) / len(window)
    width = 2.0 * dev * stdev(window)
    return width / mid * 100.0 if mid > 0 else 0.0


def build_features(bars: list[dict[str, Any]]) -> dict[str, list[float]]:
    closes = [safe_float(row.get("close")) for row in bars]
    ma50 = [sma(closes, idx, 50) for idx in range(len(bars))]
    ma200 = [sma(closes, idx, 200) for idx in range(len(bars))]
    bb_width = [bollinger_width_pct(closes, idx) for idx in range(len(bars))]
    vol_ratio = [volume_ratio(bars, idx) for idx in range(len(bars))]
    atr_pct = []
    for idx, close in enumerate(closes):
        atr = backtest_engine.atr(bars, idx)
        atr_pct.append(atr / close * 100.0 if close > 0 else 0.0)
    return {
        "closes": closes,
        "ma50": ma50,
        "ma200": ma200,
        "bb_width": bb_width,
        "vol_ratio": vol_ratio,
        "atr_pct": atr_pct,
    }


def classify_bar_with_features(bars: list[dict[str, Any]], features: dict[str, list[float]], idx: int) -> dict[str, Any] | None:
    if idx < 220 or idx >= len(bars):
        return None
    closes = features["closes"]
    close = closes[idx]
    if close <= 0:
        return None
    ma50 = features["ma50"][idx]
    ma200 = features["ma200"][idx]
    ret3 = pct(closes[idx - 3], close) if idx >= 3 else 0.0
    ret20 = pct(closes[idx - 20], close) if idx >= 20 else 0.0
    atr_pct = features["atr_pct"][idx]
    bb_width = features["bb_width"][idx]
    vol_ratio = features["vol_ratio"][idx]

    trend_up = ma50 > ma200 and close > ma50 and ret20 > 1.2
    trend_down = ma50 < ma200 and close < ma50 and ret20 < -1.2
    compression = bb_width > 0 and bb_width <= 4.5 and atr_pct <= 2.2
    impulse_up = ret3 >= 1.0 and vol_ratio >= 1.4
    impulse_down = ret3 <= -1.0 and vol_ratio >= 1.4
    high_vol = atr_pct >= 4.5

    if impulse_up and not trend_up:
        regime = "volume_impulse_up"
    elif impulse_down and not trend_down:
        regime = "volume_impulse_down"
    elif compression:
        regime = "compression_watch"
    elif trend_up:
        regime = "trend_up"
    elif trend_down:
        regime = "trend_down"
    elif high_vol:
        regime = "high_vol_chop"
    else:
        regime = "range_chop"
    return {
        "regime": regime,
        "close": round(close, 10),
        "ret3_pct": round(ret3, 6),
        "ret20_pct": round(ret20, 6),
        "atr_pct": round(atr_pct, 6),
        "bb_width_pct": round(bb_width, 6),
        "volume_ratio": round(vol_ratio, 6),
        "ma50": round(ma50, 10),
        "ma200": round(ma200, 10),
    }


def classify_bar(bars: list[dict[str, Any]], idx: int) -> dict[str, Any] | None:
    return classify_bar_with_features(bars, build_features(bars), idx)


def classify_series(symbol: str, interval: str, bars: list[dict[str, Any]], sample_step: int = 1) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    latest: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []
    features = build_features(bars) if bars else {}
    for idx in range(220, len(bars), max(1, sample_step)):
        row = classify_bar_with_features(bars, features, idx)
        if not row:
            continue
        counts[row["regime"]] += 1
        latest = {"symbol": symbol, "interval": interval, "ts": bars[idx].get("ts"), **row}
        if len(samples) < 80 or idx > len(bars) - 120:
            samples.append(latest)
    total = sum(counts.values())
    shares = {key: round(value / total * 100.0, 3) for key, value in counts.items()} if total else {}
    return {
        "symbol": symbol,
        "interval": interval,
        "bars": len(bars),
        "classified_bars": total,
        "regime_counts": dict(counts),
        "regime_share_pct": shares,
        "latest": latest,
        "samples": samples[-120:],
    }


def build_payload(root: Path, days: int, intervals: list[str]) -> dict[str, Any]:
    end = datetime.now(CST)
    start = end - timedelta(days=max(30, days))
    symbols = shared.universe_symbols(root, None)
    by_interval: dict[str, dict[str, list[dict[str, Any]]]] = {}
    rows: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    for interval in intervals:
        loaded = shared.load_interval_bars(root=root, symbols=symbols, interval=interval, start=start, end=end)
        by_interval[interval] = loaded
        for symbol, bars in loaded.items():
            result = classify_series(symbol, interval, bars)
            rows.append(result)
            totals.update(result["regime_counts"])
    latest_rows = [row["latest"] for row in rows if row.get("latest")]
    coverage = shared.coverage_rows(by_interval)
    payload = {
        "generated_at": now_iso(),
        "module": "regime_classifier",
        "status": "completed",
        "days": days,
        "intervals": intervals,
        "symbols": symbols,
        "coverage": coverage,
        "summary": {
            "classified_symbol_intervals": sum(1 for row in rows if row.get("classified_bars", 0) > 0),
            "regime_counts": dict(totals),
            "regime_share_pct": {key: round(value / max(1, sum(totals.values())) * 100.0, 3) for key, value in totals.items()},
        },
        "rows": rows,
        "latest_rows": latest_rows,
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
        "paths": {"json": str(RUNTIME_JSON), "html": str(REPORT_HTML)},
    }
    return payload


def table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int | None = None) -> str:
    use = rows[:limit] if limit else rows
    head = "".join(f"<th>{escape(label)}</th>" for _key, label in columns)
    body = []
    for row in use:
        cells = []
        for key, _label in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                text = f"{value:.3f}"
            else:
                text = str(value)
            cells.append(f"<td>{escape(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>empty</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(payload: dict[str, Any]) -> str:
    summary_rows = [{"regime": key, "count": value, "share_pct": payload["summary"]["regime_share_pct"].get(key, 0.0)} for key, value in payload["summary"]["regime_counts"].items()]
    summary_rows.sort(key=lambda item: item["count"], reverse=True)
    latest = sorted(payload["latest_rows"], key=lambda item: (item.get("interval", ""), item.get("symbol", "")))
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>Regime Classifier</title>
<style>
body{{margin:0;background:#08111a;color:#e9f1f8;font-family:Segoe UI,Arial,sans-serif}} main{{max-width:1280px;margin:0 auto;padding:28px}}
.grid{{display:grid;grid-template-columns:1fr 1.4fr;gap:14px}} .panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:16px;overflow:auto}}
h1{{margin:0 0 8px;font-size:28px}} h2{{font-size:18px}} p{{color:#91a2b1}} code{{color:#f4c95d}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #263545;padding:8px;text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}} th{{color:#91a2b1}}
</style></head><body><main>
<h1>本地行情状态分类</h1>
<p>Generated: <code>{escape(payload['generated_at'])}</code> / Local only / No Binance / No live mutation.</p>
<section class="grid">
<div class="panel"><h2>总体分布</h2>{table(summary_rows, [('regime','状态'),('count','数量'),('share_pct','占比%')])}</div>
<div class="panel"><h2>最新状态</h2>{table(latest, [('symbol','币种'),('interval','周期'),('regime','状态'),('ret3_pct','3bar%'),('ret20_pct','20bar%'),('atr_pct','ATR%'),('bb_width_pct','BB宽%'),('volume_ratio','量比')])}</div>
</section>
<p>JSON: <code>{escape(payload['paths']['json'])}</code></p>
</main></body></html>"""
    return html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify local historical Kline regimes.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    intervals = [item.strip() for item in str(args.intervals).split(",") if item.strip()]
    payload = build_payload(args.root, args.days, intervals)
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(json.dumps({"status": "completed", "summary": payload["summary"], "html": str(REPORT_HTML), "json": str(RUNTIME_JSON)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
