"""L2 short-window microstructure validation.

Local-only research gate after L1. It uses only existing compact
OFI/CVD/depth/spread samples and local Kline warehouse returns. It does not
fake order-flow history from OHLCV.

No Binance, no cloud compute, no live scanner/config mutation, no paper/real
orders, and no automatic tuning/rollback/upgrade.
"""

from __future__ import annotations

import argparse
import json
import math
import random
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

CST = timezone(timedelta(hours=8))
UTC = timezone.utc
RUNTIME_JSON = ROOT / "runtime" / "l2_microstructure_short_window_latest.json"
REPORT_HTML = ROOT / "reports" / "l2_microstructure_short_window_latest.html"

INTERVALS = ("15m", "30m", "1h")
HORIZONS = (1, 2, 4)
MIN_EDGE_SAMPLES = 50


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        seconds = raw / 1000.0 if raw > 10_000_000_000 else raw
        try:
            return datetime.fromtimestamp(seconds, UTC).astimezone(CST)
        except Exception:
            return None
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        pass
    return rows


def normalize_symbol(value: Any) -> str:
    return str(value or "").upper().replace("/", "").replace("-", "")


def add_unique_event(events: list[dict[str, Any]], seen: set[tuple[str, int, str]], item: dict[str, Any]) -> None:
    symbol = normalize_symbol(item.get("symbol"))
    ts = parse_dt(item.get("ts") or item.get("unix_ts") or item.get("snapshot_time") or item.get("snapshot_time_ms"))
    if not symbol or ts is None:
        return
    key = (symbol, int(ts.timestamp()), str(item.get("kind") or "micro"))
    if key in seen:
        return
    seen.add(key)
    item["symbol"] = symbol
    item["event_ts"] = ts
    events.append(item)


def load_microstructure_events(root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for path in sorted((root / "runtime" / "market_microstructure").glob("*.jsonl")):
        for row in iter_jsonl(path):
            item = {
                "kind": "ofi_cvd",
                "source_path": str(path),
                "symbol": row.get("symbol"),
                "ts": row.get("ts"),
                "unix_ts": row.get("unix_ts"),
                "ofi": safe_float(row.get("ofi")),
                "cvd": safe_float(row.get("cvd")),
                "quality": row.get("quality") or "",
                "ofi_source": row.get("ofi_source") or "",
                "cvd_source": row.get("cvd_source") or "",
            }
            add_unique_event(events, seen, item)

    latest = read_json(root / "runtime" / "market_microstructure_latest.json")
    features = latest.get("features") if isinstance(latest, dict) else {}
    if isinstance(features, dict):
        for symbol, row in features.items():
            if not isinstance(row, dict):
                continue
            item = {
                "kind": "ofi_cvd",
                "source_path": "runtime/market_microstructure_latest.json",
                "symbol": symbol,
                "ts": row.get("ts"),
                "unix_ts": row.get("unix_ts"),
                "ofi": safe_float(row.get("ofi")),
                "cvd": safe_float(row.get("cvd")),
                "quality": row.get("quality") or "",
                "ofi_source": row.get("ofi_source") or "",
                "cvd_source": row.get("cvd_source") or "",
            }
            add_unique_event(events, seen, item)
    return sorted(events, key=lambda item: item["event_ts"])


def parse_levels(value: Any) -> list[list[float]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    out: list[list[float]] = []
    if not isinstance(value, list):
        return out
    for raw in value:
        try:
            if isinstance(raw, dict):
                price = safe_float(raw.get("price", raw.get("p")))
                qty = safe_float(raw.get("quantity", raw.get("qty", raw.get("q"))))
            else:
                price = safe_float(raw[0])
                qty = safe_float(raw[1])
        except Exception:
            continue
        if price > 0 and qty > 0:
            out.append([price, qty])
    return out


def depth_imbalance(bids: list[list[float]], asks: list[list[float]], depth: int = 5) -> float:
    bid_qty = sum(qty for _price, qty in bids[:depth])
    ask_qty = sum(qty for _price, qty in asks[:depth])
    total = bid_qty + ask_qty
    return (bid_qty - ask_qty) / total if total > 0 else 0.0


def load_depth_events(root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    roots = [
        root / "research_store" / "depth_snapshots",
        root / "runtime" / "external_replay_seed_20260608_0025" / "depth_snapshots",
        root / "runtime" / "depth_cache",
    ]
    files: list[Path] = []
    for base in roots:
        if not base.exists():
            continue
        files.extend(sorted(base.glob("date=*/data.jsonl")))
        files.extend(sorted(base.glob("*.json")))
    for path in files:
        rows = iter_jsonl(path) if path.suffix == ".jsonl" else [read_json(path)]
        for row in rows:
            if not isinstance(row, dict):
                continue
            bids = parse_levels(row.get("bids") or row.get("bids_json"))
            asks = parse_levels(row.get("asks") or row.get("asks_json"))
            if not bids or not asks:
                continue
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            mid = (best_bid + best_ask) / 2.0
            spread_bps = safe_float(row.get("spread_bps"), (best_ask - best_bid) / mid * 10_000.0 if mid > 0 else 0.0)
            item = {
                "kind": "depth",
                "source_path": str(path),
                "symbol": row.get("symbol"),
                "snapshot_time": row.get("snapshot_time") or row.get("ts"),
                "snapshot_time_ms": row.get("snapshot_time_ms") or row.get("time_ms"),
                "spread_bps": spread_bps,
                "imbalance_top5": depth_imbalance(bids, asks, 5),
                "imbalance_top10": depth_imbalance(bids, asks, 10),
                "bid_notional_top5": sum(price * qty for price, qty in bids[:5]),
                "ask_notional_top5": sum(price * qty for price, qty in asks[:5]),
                "quality": "ok",
            }
            add_unique_event(events, seen, item)
    return sorted(events, key=lambda item: item["event_ts"])


def date_range(start: datetime, end: datetime) -> list[str]:
    days: list[str] = []
    day = start.date()
    final = end.date()
    while day <= final:
        days.append(day.isoformat())
        day = day + timedelta(days=1)
    return days


def load_bars(root: Path, symbols: set[str], intervals: tuple[str, ...], start: datetime, end: datetime) -> dict[tuple[str, str], list[dict[str, Any]]]:
    table = root / "research_store" / "historical_klines"
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for day in date_range(start - timedelta(days=1), end + timedelta(days=2)):
        path = table / f"date={day}" / "data.jsonl"
        if not path.exists():
            continue
        for row in iter_jsonl(path):
            symbol = normalize_symbol(row.get("symbol"))
            interval = str(row.get("interval") or "")
            if symbol not in symbols or interval not in intervals:
                continue
            ts = parse_dt(row.get("open_time") or row.get("open_time_ms"))
            if ts is None or ts < start - timedelta(days=1) or ts > end + timedelta(days=2):
                continue
            item = dict(row)
            item["bar_ts"] = ts
            item["open"] = safe_float(row.get("open"))
            item["close"] = safe_float(row.get("close"))
            out[(symbol, interval)].append(item)
    for key in list(out):
        out[key].sort(key=lambda row: row["bar_ts"])
    return dict(out)


def find_entry_idx(bars: list[dict[str, Any]], event_ts: datetime) -> int | None:
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid]["bar_ts"] <= event_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(bars) else None


def directional_return(bars: list[dict[str, Any]], event_ts: datetime, horizon: int, side: str) -> dict[str, Any] | None:
    entry_idx = find_entry_idx(bars, event_ts)
    if entry_idx is None:
        return None
    exit_idx = entry_idx + max(1, int(horizon)) - 1
    if exit_idx >= len(bars):
        return None
    entry = safe_float(bars[entry_idx].get("open"))
    exit_price = safe_float(bars[exit_idx].get("close"))
    if entry <= 0 or exit_price <= 0:
        return None
    raw = (exit_price - entry) / entry * 100.0
    signed = raw if side == "long" else -raw
    return {
        "entry_ts": bars[entry_idx]["bar_ts"].isoformat(timespec="seconds"),
        "exit_ts": bars[exit_idx]["bar_ts"].isoformat(timespec="seconds"),
        "raw_return_pct": raw,
        "directional_return_pct": signed,
    }


def classify_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") == "ofi_cvd":
            ofi = safe_float(event.get("ofi"))
            cvd = safe_float(event.get("cvd"))
            if ofi >= 0.30 and cvd >= 0.18:
                classified.append({**event, "signal": "L2_ofi_cvd_strong_long", "side": "long"})
            if ofi <= -0.30 and cvd <= -0.18:
                classified.append({**event, "signal": "L2_ofi_cvd_strong_short", "side": "short"})
            if ofi >= 0.15 and cvd >= 0.08:
                classified.append({**event, "signal": "L2_ofi_cvd_aligned_long", "side": "long"})
            if ofi <= -0.15 and cvd <= -0.08:
                classified.append({**event, "signal": "L2_ofi_cvd_aligned_short", "side": "short"})
            if ofi * cvd < -0.03 and abs(ofi) >= 0.15 and abs(cvd) >= 0.08:
                side = "long" if cvd > 0 else "short"
                classified.append({**event, "signal": "L2_ofi_cvd_divergence_follow_cvd", "side": side})
        elif event.get("kind") == "depth":
            imbalance = safe_float(event.get("imbalance_top5"))
            spread = safe_float(event.get("spread_bps"))
            if imbalance >= 0.20:
                classified.append({**event, "signal": "L2_depth_bid_imbalance_long", "side": "long"})
            if imbalance <= -0.20:
                classified.append({**event, "signal": "L2_depth_ask_imbalance_short", "side": "short"})
            if spread >= 8.0:
                classified.append({**event, "signal": "L2_wide_spread_fade_long", "side": "long"})
                classified.append({**event, "signal": "L2_wide_spread_fade_short", "side": "short"})
    return classified


def build_records(classified: list[dict[str, Any]], bars_by_key: dict[tuple[str, str], list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], Counter]:
    records: list[dict[str, Any]] = []
    misses: Counter = Counter()
    for event in classified:
        for interval in INTERVALS:
            bars = bars_by_key.get((event["symbol"], interval)) or []
            if not bars:
                misses["missing_bars"] += 1
                continue
            for horizon in HORIZONS:
                ret = directional_return(bars, event["event_ts"], horizon, str(event.get("side")))
                if ret is None:
                    misses["missing_forward"] += 1
                    continue
                records.append(
                    {
                        "signal": event["signal"],
                        "kind": event["kind"],
                        "symbol": event["symbol"],
                        "event_ts": event["event_ts"].isoformat(timespec="seconds"),
                        "interval": interval,
                        "horizon_bars": horizon,
                        "side": event["side"],
                        "directional_return_pct": round(ret["directional_return_pct"], 6),
                        "raw_return_pct": round(ret["raw_return_pct"], 6),
                        "entry_ts": ret["entry_ts"],
                        "exit_ts": ret["exit_ts"],
                        "ofi": round(safe_float(event.get("ofi")), 6),
                        "cvd": round(safe_float(event.get("cvd")), 6),
                        "spread_bps": round(safe_float(event.get("spread_bps")), 6),
                        "imbalance_top5": round(safe_float(event.get("imbalance_top5")), 6),
                    }
                )
    return records, misses


def symbol_count(records: list[dict[str, Any]]) -> int:
    return len({normalize_symbol(row.get("symbol")) for row in records if normalize_symbol(row.get("symbol"))})


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "avg_pct": 0.0, "median_pct": 0.0, "win_rate_pct": 0.0, "p10_pct": 0.0, "p90_pct": 0.0}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "count": n,
        "avg_pct": round(sum(ordered) / n, 6),
        "median_pct": round(ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2, 6),
        "win_rate_pct": round(sum(1 for value in ordered if value > 0) / n * 100.0, 3),
        "p10_pct": round(ordered[max(0, int(n * 0.10) - 1)], 6),
        "p90_pct": round(ordered[min(n - 1, int(n * 0.90))], 6),
    }


def baseline_returns(
    records: list[dict[str, Any]],
    bars_by_key: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    controls: int,
) -> list[float]:
    out: list[float] = []
    for rec in records:
        bars = bars_by_key.get((rec["symbol"], rec["interval"])) or []
        if len(bars) < int(rec["horizon_bars"]) + 4:
            continue
        rng = random.Random(f"{rec['signal']}|{rec['symbol']}|{rec['interval']}|{rec['horizon_bars']}|{rec['event_ts']}")
        entry_event = parse_dt(rec["event_ts"])
        candidates = [idx for idx in range(0, len(bars) - int(rec["horizon_bars"])) if entry_event is None or abs((bars[idx]["bar_ts"] - entry_event).total_seconds()) > 3600]
        if not candidates:
            continue
        for _ in range(max(1, controls)):
            idx = candidates[rng.randrange(len(candidates))]
            pseudo_ts = bars[idx]["bar_ts"] - timedelta(seconds=1)
            ret = directional_return(bars, pseudo_ts, int(rec["horizon_bars"]), str(rec["side"]))
            if ret:
                out.append(safe_float(ret["directional_return_pct"]))
    return out


def split_stats(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not records:
        return {"train": stats([]), "validation": stats([]), "test": stats([])}
    ordered = sorted(records, key=lambda row: row["event_ts"])
    n = len(ordered)
    train_end = int(n * 0.60)
    val_end = int(n * 0.80)
    return {
        "train": stats([safe_float(row.get("directional_return_pct")) for row in ordered[:train_end]]),
        "validation": stats([safe_float(row.get("directional_return_pct")) for row in ordered[train_end:val_end]]),
        "test": stats([safe_float(row.get("directional_return_pct")) for row in ordered[val_end:]]),
    }


def evaluate(records: list[dict[str, Any]], bars_by_key: dict[tuple[str, str], list[dict[str, Any]]], controls: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[(record["signal"], record["interval"], int(record["horizon_bars"]), record["side"])].append(record)
    rows: list[dict[str, Any]] = []
    for (signal, interval, horizon, side), items in groups.items():
        signal_values = [safe_float(item.get("directional_return_pct")) for item in items]
        signal_stats = stats(signal_values)
        baseline_values = baseline_returns(items, bars_by_key, controls=controls)
        base_stats = stats(baseline_values)
        splits = split_stats(items)
        reasons: list[str] = []
        if signal_stats["count"] < MIN_EDGE_SAMPLES:
            reasons.append("sample_count_low")
        if signal_stats["avg_pct"] <= base_stats["avg_pct"]:
            reasons.append("avg_not_above_matched_baseline")
        if signal_stats["median_pct"] <= base_stats["median_pct"]:
            reasons.append("median_not_above_matched_baseline")
        if signal_stats["win_rate_pct"] - base_stats["win_rate_pct"] < 2.0:
            reasons.append("win_uplift_below_2pct")
        for split, metric in splits.items():
            if metric["count"] < 10:
                reasons.append(f"{split}_sample_count_low")
            if metric["avg_pct"] <= 0:
                reasons.append(f"{split}_avg_not_positive")
        decision = "microstructure_candidate" if not reasons else ("watchlist" if signal_stats["count"] >= 10 and signal_stats["avg_pct"] > base_stats["avg_pct"] else "rejected")
        rows.append(
            {
                "signal": signal,
                "interval": interval,
                "horizon_bars": horizon,
                "side": side,
                "decision": decision,
                "failed_gates": sorted(set(reasons)),
                "signal_stats": signal_stats,
                "baseline_stats": base_stats,
                "uplift_avg_pct": round(signal_stats["avg_pct"] - base_stats["avg_pct"], 6),
                "uplift_median_pct": round(signal_stats["median_pct"] - base_stats["median_pct"], 6),
                "uplift_win_rate_pct": round(signal_stats["win_rate_pct"] - base_stats["win_rate_pct"], 3),
                "split_stats": splits,
                "top_symbols": Counter(item["symbol"] for item in items).most_common(8),
            }
        )
    rows.sort(key=lambda row: (row["decision"] == "microstructure_candidate", row["uplift_avg_pct"], row["signal_stats"]["count"]), reverse=True)
    return rows


def render_html(payload: dict[str, Any]) -> str:
    rows = payload.get("results") or []
    table_rows = []
    for row in rows[:80]:
        s = row.get("signal_stats") or {}
        b = row.get("baseline_stats") or {}
        table_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('decision')))}</td>"
            f"<td>{escape(str(row.get('signal')))}</td>"
            f"<td>{escape(str(row.get('interval')))}</td>"
            f"<td>{escape(str(row.get('horizon_bars')))}</td>"
            f"<td>{escape(str(row.get('side')))}</td>"
            f"<td>{escape(str(s.get('count')))}</td>"
            f"<td>{escape(str(s.get('avg_pct')))}</td>"
            f"<td>{escape(str(b.get('avg_pct')))}</td>"
            f"<td>{escape(str(row.get('uplift_avg_pct')))}</td>"
            f"<td>{escape(str(row.get('uplift_win_rate_pct')))}</td>"
            f"<td>{escape(', '.join(row.get('failed_gates') or []))}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>L2 微结构短窗验证</title>
<style>
body{{margin:0;background:#0b1118;color:#d9e2ec;font:14px/1.55 Arial,"Microsoft YaHei",sans-serif}}
main{{max-width:1240px;margin:0 auto;padding:28px}}
.panel{{background:#111b26;border:1px solid #263545;border-radius:8px;padding:18px;margin:14px 0}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}
.kpi{{background:#0d1620;border:1px solid #263545;border-radius:8px;padding:14px}}
.kpi b{{display:block;color:#7dd3fc;font-size:22px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{border-bottom:1px solid #223040;padding:8px;text-align:left;vertical-align:top}}th{{color:#93c5fd}}
.bad{{color:#fca5a5}}.ok{{color:#86efac}}code{{color:#facc15}}
</style></head><body><main>
<h1>L2 微结构短窗验证</h1>
<p>Generated <code>{escape(payload.get('generated_at',''))}</code>. Local-only, no Binance, no cloud, no live mutation.</p>
<section class="grid">
<div class="kpi">状态<b>{escape(payload.get('status',''))}</b></div>
<div class="kpi">事件币种/可对齐币种<b>{payload.get('coverage',{}).get('event_symbols',0)} / {payload.get('coverage',{}).get('bar_symbols',0)}</b></div>
<div class="kpi">微结构/Depth 样本<b>{payload.get('coverage',{}).get('micro_events',0)} / {payload.get('coverage',{}).get('depth_events',0)}</b></div>
<div class="kpi">Aligned records<b>{payload.get('coverage',{}).get('aligned_records',0)}</b></div>
</section>
<section class="panel"><h2>结论</h2><p>{escape(payload.get('conclusion',''))}</p>
<p>Decision counts: <code>{escape(json.dumps(payload.get('decision_counts') or {}, ensure_ascii=False))}</code></p></section>
<section class="panel"><h2>验证结果</h2><table><thead><tr>
<th>决策</th><th>信号</th><th>周期</th><th>H</th><th>方向</th><th>N</th><th>信号均值%</th><th>基线均值%</th><th>提升%</th><th>胜率提升</th><th>失败门</th>
</tr></thead><tbody>{''.join(table_rows)}</tbody></table></section>
</main></body></html>"""


def build_payload(root: Path, controls: int) -> dict[str, Any]:
    micro_events = load_microstructure_events(root)
    depth_events = load_depth_events(root)
    events = [*micro_events, *depth_events]
    if events:
        start = min(item["event_ts"] for item in events)
        end = max(item["event_ts"] for item in events)
    else:
        start = datetime.now(CST) - timedelta(days=7)
        end = datetime.now(CST)
    symbols = {item["symbol"] for item in events}
    bars = load_bars(root, symbols, INTERVALS, start, end) if symbols else {}
    classified = classify_events(events)
    records, misses = build_records(classified, bars)
    results = evaluate(records, bars, controls) if records else []
    decision_counts = dict(Counter(row["decision"] for row in results))
    status = "completed_data_gap" if not any(row.get("decision") == "microstructure_candidate" for row in results) else "completed"
    if len(records) < MIN_EDGE_SAMPLES:
        conclusion = "真实微结构/盘口样本仍不足，当前只能确认 L2 框架可用，不能证明可交易 alpha。继续采集 compact OFI/CVD/depth/spread 样本后重跑。"
    elif status == "completed_data_gap":
        conclusion = "已有短窗样本未形成可晋级微结构候选。保留 watchlist，继续采样；不要用 Kline 伪造 OFI/depth。"
    else:
        conclusion = "存在 microstructure_candidate；仍只允许进入下一轮完整交易重构和人工复核。"
    return {
        "generated_at": now_iso(),
        "module": "l2_microstructure_short_window",
        "status": status,
        "controls": controls,
        "intervals": list(INTERVALS),
        "horizons": list(HORIZONS),
        "coverage": {
            "micro_events": len(micro_events),
            "depth_events": len(depth_events),
            "classified_events": len(classified),
            "aligned_records": len(records),
            "event_symbols": len(symbols),
            "classified_symbols": symbol_count(classified),
            "aligned_symbols": symbol_count(records),
            "bar_symbols": len({key[0] for key in bars}),
            "bar_series": len(bars),
            "event_start": start.isoformat(timespec="seconds"),
            "event_end": end.isoformat(timespec="seconds"),
            "misses": dict(misses),
        },
        "decision_counts": decision_counts,
        "results": results,
        "conclusion": conclusion,
        "safety": {
            "local_only": True,
            "binance_requests_enabled": False,
            "cloud_compute_enabled": False,
            "live_scanner_mutation": False,
            "paper_or_real_orders": False,
            "automatic_tuning_allowed": False,
            "automatic_rollback_allowed": False,
            "automatic_upgrade_allowed": False,
            "promotion_allowed": False,
        },
        "next": "Keep collecting real compact OFI/CVD/depth/spread samples; rerun L2 before any L3 strategy reconstruction.",
        "paths": {"json": str(RUNTIME_JSON), "html": str(REPORT_HTML)},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate real short-window microstructure edges locally")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--controls", type=int, default=8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = build_payload(args.root, max(1, int(args.controls)))
    write_json(RUNTIME_JSON, payload)
    REPORT_HTML.parent.mkdir(parents=True, exist_ok=True)
    REPORT_HTML.write_text(render_html(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload.get("status"),
                "coverage": payload.get("coverage"),
                "decision_counts": payload.get("decision_counts"),
                "best": (payload.get("results") or [{}])[0],
                "html": str(REPORT_HTML),
                "json": str(RUNTIME_JSON),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
