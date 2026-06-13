"""Local-only Kronos research adapter.

This script evaluates whether a Kronos-style Kline forecaster adds useful
signal-filtering power on the local historical warehouse. It is research-only:
no cloud, no Binance, no scanner config mutation, no paper/live orders, and no
automatic upgrade decision.
"""

from __future__ import annotations

import argparse
import html
import inspect
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "部署工具" else SCRIPT_DIR
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest_engine


CST = timezone(timedelta(hours=8))
RUNTIME_JSON = ROOT / "runtime" / "kronos_research_latest.json"
REPORT_HTML = ROOT / "reports" / "kronos_research_latest.html"
UNIVERSE_JSON = ROOT / "runtime" / "historical_kline_research_universe_latest.json"
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]


def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except Exception:
        return default
    return result if math.isfinite(result) else default


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def load_universe(limit: int) -> list[str]:
    payload = read_json(UNIVERSE_JSON)
    symbols = payload.get("eligible_symbols") if isinstance(payload.get("eligible_symbols"), list) else []
    if not symbols:
        symbols = DEFAULT_SYMBOLS
    return [str(symbol).upper() for symbol in symbols[: max(1, limit)]]


def rank_values(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda idx: values[idx])
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(order):
        end = pos + 1
        while end < len(order) and values[order[end]] == values[order[pos]]:
            end += 1
        rank = (pos + end - 1) / 2.0
        for idx in order[pos:end]:
            ranks[idx] = rank
        pos = end
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def spearman(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    return pearson(rank_values(xs), rank_values(ys))


def bars_to_frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamps": [pd.Timestamp(backtest_engine.ms_to_iso(int(row["open_time_ms"]))) for row in bars],
            "open": [safe_float(row.get("open")) for row in bars],
            "high": [safe_float(row.get("high")) for row in bars],
            "low": [safe_float(row.get("low")) for row in bars],
            "close": [safe_float(row.get("close")) for row in bars],
            "volume": [safe_float(row.get("volume")) for row in bars],
            "amount": [safe_float(row.get("quote_volume"), safe_float(row.get("volume"))) for row in bars],
        }
    )


def baseline_predict_pct(bars: list[dict[str, Any]], idx: int, context: int) -> float:
    start = max(0, idx - context + 1)
    closes = np.asarray([safe_float(row.get("close")) for row in bars[start : idx + 1]], dtype=float)
    if len(closes) < 32 or closes[-1] <= 0:
        return 0.0
    returns = np.diff(np.log(np.maximum(closes, 1e-12)))
    mom_12 = closes[-1] / closes[max(0, len(closes) - 13)] - 1.0
    mom_48 = closes[-1] / closes[max(0, len(closes) - 49)] - 1.0
    vol_48 = float(np.std(returns[-48:])) if len(returns) >= 8 else 0.0
    high = float(np.max(closes[-96:]))
    low = float(np.min(closes[-96:]))
    range_pos = ((closes[-1] - low) / max(high - low, 1e-12)) - 0.5
    pred = 0.42 * mom_12 + 0.28 * mom_48 - 0.18 * range_pos * min(vol_48 * 100.0, 1.0)
    return float(max(min(pred * 100.0, 8.0), -8.0))


class KronosBackend:
    def __init__(self, args: argparse.Namespace) -> None:
        if not args.kronos_repo:
            raise RuntimeError("kronos_repo_required")
        repo = Path(args.kronos_repo).expanduser().resolve()
        if not repo.exists():
            raise RuntimeError(f"kronos_repo_not_found:{repo}")
        sys.path.insert(0, str(repo))
        try:
            from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"kronos_import_failed:{exc}") from exc
        try:
            tokenizer = KronosTokenizer.from_pretrained(args.tokenizer_name)
            model = Kronos.from_pretrained(args.model_name)
            self.predictor = KronosPredictor(
                model,
                tokenizer,
                device=args.device,
                max_context=args.context,
            )
        except Exception as exc:
            raise RuntimeError(f"kronos_model_load_failed:{exc}") from exc

    def predict_pct(self, context_bars: list[dict[str, Any]], interval: str, horizon: int) -> float:
        frame = bars_to_frame(context_bars)
        interval_ms = backtest_engine.INTERVAL_MS.get(interval, 60 * 60_000)
        last_ms = int(context_bars[-1]["open_time_ms"])
        y_timestamp = [
            pd.Timestamp(backtest_engine.ms_to_iso(last_ms + interval_ms * step))
            for step in range(1, horizon + 1)
        ]
        signature = inspect.signature(self.predictor.predict)
        kwargs: dict[str, Any] = {}
        params = signature.parameters
        if "df" in params:
            kwargs["df"] = frame
        if "x_timestamp" in params:
            kwargs["x_timestamp"] = frame["timestamps"]
        if "y_timestamp" in params:
            kwargs["y_timestamp"] = pd.Series(y_timestamp)
        if "pred_len" in params:
            kwargs["pred_len"] = horizon
        if "T" in params:
            kwargs["T"] = 1.0
        if "top_p" in params:
            kwargs["top_p"] = 0.9
        if "sample_count" in params:
            kwargs["sample_count"] = 1
        pred = self.predictor.predict(**kwargs)
        if not isinstance(pred, pd.DataFrame) or "close" not in pred.columns or pred.empty:
            raise RuntimeError("kronos_predict_returned_no_close")
        start_close = safe_float(context_bars[-1].get("close"))
        end_close = safe_float(pred["close"].iloc[-1])
        return (end_close / start_close - 1.0) * 100.0 if start_close > 0 else 0.0


def evaluate_rows(rows: list[dict[str, Any]], min_abs_pred_pct: float) -> dict[str, Any]:
    if not rows:
        return {
            "samples": 0,
            "actionable_samples": 0,
            "direction_accuracy_pct": 0.0,
            "actionable_direction_accuracy_pct": 0.0,
            "rank_ic": 0.0,
            "top_bottom_spread_pct": 0.0,
            "avg_actual_pct": 0.0,
        }
    preds = [safe_float(row.get("pred_pct")) for row in rows]
    actuals = [safe_float(row.get("actual_pct")) for row in rows]
    hits = [1 for p, a in zip(preds, actuals) if p == 0 or a == 0 or (p > 0) == (a > 0)]
    actionable = [row for row in rows if abs(safe_float(row.get("pred_pct"))) >= min_abs_pred_pct]
    actionable_hits = [
        1
        for row in actionable
        if safe_float(row.get("pred_pct")) == 0
        or safe_float(row.get("actual_pct")) == 0
        or (safe_float(row.get("pred_pct")) > 0) == (safe_float(row.get("actual_pct")) > 0)
    ]
    ranked = sorted(rows, key=lambda row: safe_float(row.get("pred_pct")))
    bucket = max(1, int(len(ranked) * 0.2))
    bottom = ranked[:bucket]
    top = ranked[-bucket:]
    top_avg = sum(safe_float(row.get("actual_pct")) for row in top) / len(top)
    bottom_avg = sum(safe_float(row.get("actual_pct")) for row in bottom) / len(bottom)
    return {
        "samples": len(rows),
        "actionable_samples": len(actionable),
        "direction_accuracy_pct": sum(hits) / len(rows) * 100.0,
        "actionable_direction_accuracy_pct": (sum(actionable_hits) / len(actionable) * 100.0) if actionable else 0.0,
        "rank_ic": spearman(preds, actuals),
        "top_bottom_spread_pct": top_avg - bottom_avg,
        "avg_actual_pct": sum(actuals) / len(actuals),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    symbols = parse_csv(args.symbols) or load_universe(args.top_n)
    intervals = [item.strip() for item in args.intervals.split(",") if item.strip()]
    end = datetime.now(CST)
    start = end - timedelta(days=int(args.days))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    backend_status = "baseline_smoke"
    backend_error = ""
    kronos_backend: KronosBackend | None = None
    if args.backend == "kronos":
        try:
            kronos_backend = KronosBackend(args)
            backend_status = "kronos_loaded"
        except Exception as exc:
            backend_status = "kronos_unavailable_fallback_baseline"
            backend_error = str(exc)

    summaries: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for interval in intervals:
        interval_rows: list[dict[str, Any]] = []
        for symbol in symbols:
            bars = backtest_engine.load_bars(root=root, symbol=symbol, interval=interval, start_ms=start_ms, end_ms=end_ms)
            coverage.append({"symbol": symbol, "interval": interval, "bars": len(bars), "usable": len(bars) >= args.context + args.horizon + 8})
            if len(bars) < args.context + args.horizon + 8:
                continue
            step = max(1, (len(bars) - args.context - args.horizon) // max(1, args.max_samples_per_symbol))
            sample_count = 0
            for idx in range(args.context - 1, len(bars) - args.horizon, step):
                if sample_count >= args.max_samples_per_symbol:
                    break
                context_bars = bars[max(0, idx - args.context + 1) : idx + 1]
                if kronos_backend is not None:
                    try:
                        pred_pct = kronos_backend.predict_pct(context_bars, interval, args.horizon)
                    except Exception as exc:
                        backend_status = "kronos_predict_failed_fallback_baseline"
                        backend_error = str(exc)
                        pred_pct = baseline_predict_pct(bars, idx, args.context)
                        kronos_backend = None
                else:
                    pred_pct = baseline_predict_pct(bars, idx, args.context)
                entry = safe_float(bars[idx].get("close"))
                future = safe_float(bars[idx + args.horizon].get("close"))
                actual_pct = (future / entry - 1.0) * 100.0 if entry > 0 else 0.0
                row = {
                    "symbol": symbol,
                    "interval": interval,
                    "ts": bars[idx].get("ts"),
                    "pred_pct": pred_pct,
                    "actual_pct": actual_pct,
                    "abs_error_pct": abs(pred_pct - actual_pct),
                    "side": "long" if pred_pct > 0 else "short" if pred_pct < 0 else "flat",
                }
                interval_rows.append(row)
                if len(samples) < args.sample_limit:
                    samples.append(row)
                sample_count += 1
        summary = evaluate_rows(interval_rows, args.min_abs_pred_pct)
        summary.update({"interval": interval})
        summaries.append(summary)

    status = "research_smoke_ready"
    if args.backend == "kronos" and backend_status != "kronos_loaded":
        status = "kronos_adapter_ready_model_not_verified"
    best = max(summaries, key=lambda row: safe_float(row.get("rank_ic"))) if summaries else {}
    payload = {
        "generated_at": now_iso(),
        "module": "kronos_research_adapter",
        "status": status,
        "backend": args.backend,
        "backend_status": backend_status,
        "backend_error": backend_error,
        "safety": {
            "local_only": True,
            "binance_requests_enabled": False,
            "server_deploy": False,
            "paper_or_live_orders": False,
            "automatic_upgrade_allowed": False,
        },
        "config": {
            "symbols": symbols,
            "intervals": intervals,
            "days": args.days,
            "context": args.context,
            "horizon": args.horizon,
            "max_samples_per_symbol": args.max_samples_per_symbol,
            "min_abs_pred_pct": args.min_abs_pred_pct,
            "model_name": args.model_name,
            "tokenizer_name": args.tokenizer_name,
            "kronos_repo": args.kronos_repo,
        },
        "summaries": summaries,
        "best_by_rank_ic": best,
        "coverage": coverage,
        "samples": samples,
        "operator_read": {
            "plain_conclusion": "adapter landed; baseline smoke is only a wiring check, not alpha evidence",
            "next_action": "install Kronos dependencies and local model weights, then rerun with --backend kronos; compare to baseline and matched random before using as filter",
        },
    }
    write_outputs(payload, root)
    return payload


def write_outputs(payload: dict[str, Any], root: Path) -> None:
    runtime_path = root / "runtime" / "kronos_research_latest.json"
    report_path = root / "reports" / "kronos_research_latest.html"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    rows = "".join(
        f"""
<tr>
  <td>{h(row.get('interval'))}</td>
  <td>{h(row.get('samples'))}</td>
  <td>{h(f"{safe_float(row.get('direction_accuracy_pct')):.2f}%")}</td>
  <td>{h(f"{safe_float(row.get('actionable_direction_accuracy_pct')):.2f}%")}</td>
  <td>{h(f"{safe_float(row.get('rank_ic')):.4f}")}</td>
  <td>{h(f"{safe_float(row.get('top_bottom_spread_pct')):+.4f}%")}</td>
</tr>
""".strip()
        for row in payload.get("summaries", [])
        if isinstance(row, dict)
    )
    sample_rows = "".join(
        f"<tr><td>{h(row.get('symbol'))}</td><td>{h(row.get('interval'))}</td><td>{h(row.get('ts'))}</td><td>{safe_float(row.get('pred_pct')):+.4f}%</td><td>{safe_float(row.get('actual_pct')):+.4f}%</td><td>{h(row.get('side'))}</td></tr>"
        for row in payload.get("samples", [])[:80]
        if isinstance(row, dict)
    )
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kronos 本地研究</title>
<style>
body{{margin:0;background:#071018;color:#edf4ff;font:14px/1.55 "Segoe UI",Arial,sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:28px}}
.panel{{border:1px solid #213044;background:#0d1420;border-radius:8px;padding:16px;margin:14px 0}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}}
.card{{border:1px solid #213044;background:#0b1320;border-radius:8px;padding:12px}}
span,small{{color:#8ea2bd}} b{{display:block;font-size:20px;margin-top:4px}}
table{{width:100%;border-collapse:collapse}} th,td{{border-bottom:1px solid #213044;padding:8px;text-align:left}} th{{color:#8ea2bd;background:#0b1422}}
.warn{{color:#f4b740}} .good{{color:#21d18b}}
</style>
</head>
<body><main>
<h1>Kronos 本地研究</h1>
<p>生成 {h(payload.get('generated_at'))}。本报告只读本地历史 K线，不调用 Binance，不部署服务器，不改策略。</p>
<section class="panel grid">
  <div class="card"><span>状态</span><b>{h(payload.get('status'))}</b></div>
  <div class="card"><span>后端</span><b>{h(payload.get('backend_status'))}</b><small>{h(payload.get('backend_error'))}</small></div>
  <div class="card"><span>最好 RankIC</span><b>{safe_float((payload.get('best_by_rank_ic') or {}).get('rank_ic')):.4f}</b></div>
  <div class="card"><span>安全边界</span><b class="good">local only</b><small>不下单，不自动升级。</small></div>
</section>
<section class="panel"><h2>周期结果</h2><table><thead><tr><th>周期</th><th>样本</th><th>方向胜率</th><th>可交易方向胜率</th><th>RankIC</th><th>Top-Bottom</th></tr></thead><tbody>{rows}</tbody></table></section>
<section class="panel"><h2>样本预览</h2><table><thead><tr><th>币种</th><th>周期</th><th>时间</th><th>预测</th><th>实际</th><th>方向</th></tr></thead><tbody>{sample_rows}</tbody></table></section>
<section class="panel"><h2>下一步</h2><p>{h((payload.get('operator_read') or {}).get('next_action'))}</p></section>
</main></body></html>"""
    report_path.write_text(html_text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Kronos-style research adapter")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--backend", choices=["baseline", "kronos"], default="baseline")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--intervals", default="1h,4h")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--context", type=int, default=128)
    parser.add_argument("--horizon", type=int, default=3)
    parser.add_argument("--max-samples-per-symbol", type=int, default=48)
    parser.add_argument("--sample-limit", type=int, default=160)
    parser.add_argument("--min-abs-pred-pct", type=float, default=0.15)
    parser.add_argument("--kronos-repo", default="")
    parser.add_argument("--model-name", default="NeoQuasar/Kronos-small")
    parser.add_argument("--tokenizer-name", default="NeoQuasar/Kronos-Tokenizer-base")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    payload = run(args)
    print(json.dumps({"status": payload["status"], "backend_status": payload["backend_status"], "json": str(Path(args.root) / "runtime" / "kronos_research_latest.json"), "html": str(Path(args.root) / "reports" / "kronos_research_latest.html")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
