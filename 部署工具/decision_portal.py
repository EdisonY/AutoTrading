"""Generate a concise online decision portal for the operator.

This is the first screen. It only reads existing local/runtime artifacts and
does not call exchange APIs. The older full portal remains available as
``portal_latest.html`` for drilldown.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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
REPORTS_DIR = ROOT / "reports"
RUNTIME_DIR = ROOT / "runtime"
MIRROR_RUNTIME_DIR = ROOT / "server_logs_tencent" / "runtime"
ATTENTION_JSON = ROOT / "research_memory" / "attention" / "open_items.json"
LIVE_ATTENTION_JSON = RUNTIME_DIR / "live_attention_latest.json"
MIRROR_ATTENTION_JSON = MIRROR_RUNTIME_DIR / "live_attention_latest.json"
BACKTEST_MODULE_JSON = RUNTIME_DIR / "backtest_module_latest.json"
MIRROR_BACKTEST_MODULE_JSON = MIRROR_RUNTIME_DIR / "backtest_module_latest.json"
LOCAL_DB = RUNTIME_DIR / "event_store.sqlite3"
MIRROR_DB = ROOT / "server_logs_tencent" / "runtime" / "event_store.sqlite3"
EVENT_DB = MIRROR_DB if MIRROR_DB.exists() else LOCAL_DB
CST = timezone(timedelta(hours=8))
DEFAULT_TAKER_FEE_RATE = 0.0004
REPORT_REFRESH_SECONDS = 60
STRATEGY_NAMES = ("A/v11", "B/v16")
RETIRED_STRATEGY_NAMES = ("C/v14",)
STRATEGY_LIFECYCLE = {
    "A/v11": "active",
    "B/v16": "frozen_observe",
    "C/v14": "retired",
}


def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_first_json(*paths: Path) -> dict[str, Any]:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in paths:
        payload = read_json(path)
        if not payload:
            continue
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        candidates.append((mtime, payload))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return {}


def num(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def research_strategy_label(strategy: Any) -> str:
    text = str(strategy or "").replace("R-", "")
    if text.startswith("L1-"):
        return "L1"
    if text.startswith("J3-"):
        return "J3"
    if text.startswith("E-"):
        return "E/4h"
    return text or "-"


def research_signal_brief(research_paper: dict[str, Any]) -> str:
    rows = research_paper.get("results") if isinstance(research_paper.get("results"), list) else []
    parts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = research_strategy_label(row.get("strategy") or row.get("strategy_id"))
        signals = row.get("signals") if isinstance(row.get("signals"), list) else []
        if signals:
            counts: dict[str, int] = {}
            for signal in signals:
                if isinstance(signal, dict):
                    status = str(signal.get("status") or "-")
                    counts[status] = counts.get(status, 0) + 1
            bits = []
            if counts.get("checked"):
                bits.append(f"查{counts['checked']}")
            if counts.get("no_signal"):
                bits.append(f"无{counts['no_signal']}")
            if counts.get("data_gap"):
                bits.append(f"缺{counts['data_gap']}")
            parts.append(f"{label}:{'/'.join(bits) or '-'}")
            continue
        signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
        status = str(row.get("status") or signal.get("status") or "-")
        if status == "data_gap" and signal:
            need = int(num(signal.get("need_symbols")))
            times = int(num(signal.get("times")))
            parts.append(f"{label}:缺横截面{need}币/{times}时点")
        else:
            parts.append(f"{label}:{report_text(status)}")
    return "；".join(parts[:4]) or "等待首轮信号"


def read_best_historical_kline_json(*paths: Path) -> dict[str, Any]:
    candidates: list[tuple[tuple[float, float, float, float, str, float], dict[str, Any]]] = []
    mirror_candidates: list[tuple[tuple[float, float, float, float, str, float], dict[str, Any]]] = []
    for path in paths:
        payload = read_json(path)
        if not payload:
            continue
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        rank = (
            num(progress.get("written_rows")),
            num(progress.get("percent")),
            num(progress.get("completed_requests")) + num(progress.get("skipped_existing")),
            -num(progress.get("failed_requests")),
            str(payload.get("generated_at") or ""),
            mtime,
        )
        item = (rank, payload)
        candidates.append(item)
        if "server_logs_tencent" in str(path).replace("\\", "/"):
            mirror_candidates.append(item)
    if mirror_candidates:
        mirror_candidates.sort(key=lambda item: item[0], reverse=True)
        return mirror_candidates[0][1]
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def historical_kline_complete(payload: dict[str, Any]) -> bool:
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    return str(payload.get("status") or "") == "complete" and int(progress.get("pending_tasks") or 0) == 0


def read_alerts_json() -> dict[str, Any]:
    mirror = read_json(MIRROR_RUNTIME_DIR / "alerts_latest.json")
    if mirror:
        return mirror
    return read_json(RUNTIME_DIR / "alerts_latest.json")


def read_live_runtime_json(name: str) -> dict[str, Any]:
    mirror = read_json(MIRROR_RUNTIME_DIR / name)
    if mirror:
        return mirror
    return read_json(RUNTIME_DIR / name)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def age_text(dt: datetime | None) -> str:
    if not dt:
        return "无记录"
    seconds = max(0, int((datetime.now(CST) - dt).total_seconds()))
    if seconds < 90:
        return f"{seconds}秒前"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}小时前"
    return f"{hours // 24}天前"


def number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):+.{digits}f}"
    except Exception:
        return "0.00"


def amount(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "0.00"


def fmt_plain(value: Any, digits: int = 6, default: str = "-") -> str:
    try:
        num = float(value)
    except Exception:
        return default
    if num == 0:
        return "0"
    abs_num = abs(num)
    if abs_num < 10 ** -digits:
        return f"{num:.{digits}g}"
    if abs_num < 1:
        small_digits = min(12, max(digits, 2 - int(f"{abs_num:e}".split("e")[1])))
        return f"{num:.{small_digits}f}".rstrip("0").rstrip(".")
    return f"{num:.{digits}f}".rstrip("0").rstrip(".")


def report_text(value: Any, default: str = "-") -> str:
    text = str(value or default)
    replacements = {
        "OKX 15m/latest cached close; ": "OKX 15分钟K线/本地缓存收盘价；",
        "Binance mark/index may differ": "不同交易所标记价可能有轻微差异",
        "updated when paper_exchange_runner runs, not exchange tick-by-tick": "按模拟账本刷新，不是逐笔 tick",
        "not exchange-order-book exact; use conservative model before strategy promotion": "不是逐笔盘口撮合；策略升级前要用保守滑点模型",
        "ledger fee_rate=0.000400": "账本费率 0.04%",
        "Binance": "交易所",
        "币安": "交易所",
        "paper sample": "模拟采样",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def kline_cache_rows(symbol: str, timeframe: str = "15m") -> tuple[list[list[Any]], str]:
    safe = f"{str(symbol).upper()}_{timeframe}_*.json".replace("/", "_")
    rows_by_ts: dict[int, list[Any]] = {}
    sources: list[str] = []
    for cache_dir in (RUNTIME_DIR / "kline_cache", MIRROR_RUNTIME_DIR / "kline_cache"):
        if not cache_dir.exists():
            continue
        for path in cache_dir.glob(safe):
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
                if not isinstance(raw_rows, list):
                    continue
                for row in raw_rows:
                    if not isinstance(row, (list, tuple)) or len(row) < 6:
                        continue
                    try:
                        rows_by_ts[int(float(row[0]))] = list(row)
                    except Exception:
                        continue
                sources.append(path.name)
            except Exception:
                continue
    rows = [rows_by_ts[key] for key in sorted(rows_by_ts)]
    return rows, ", ".join(sorted(set(sources))[:3])


def kline_window(symbol: str, entry_at: Any, *, before: int = 30, after: int = 30) -> dict[str, Any]:
    rows, source = kline_cache_rows(symbol)
    entry_dt = parse_dt(entry_at)
    entry_ms = int(entry_dt.timestamp() * 1000) if entry_dt else None
    if not rows:
        return {"bars": [], "entry_index": None, "entry_ms": entry_ms, "source": source, "status": "missing"}
    if entry_ms is None:
        start = max(0, len(rows) - before - after)
        return {"bars": rows[start:], "entry_index": None, "entry_ms": None, "source": source, "status": "no_entry_time"}
    closest = min(range(len(rows)), key=lambda idx: abs(int(float(rows[idx][0])) - entry_ms))
    start = max(0, closest - before)
    end = min(len(rows), closest + after + 1)
    window = rows[start:end]
    return {
        "bars": window,
        "entry_index": closest - start if window else None,
        "entry_ms": entry_ms,
        "source": source,
        "status": "ok" if len(window) >= min(20, before) else "thin",
        "before": closest - start,
        "after": end - closest - 1,
    }


def _bar_float(row: list[Any], idx: int) -> float:
    return float(row[idx])


def render_kline_svg(symbol: str, position: dict[str, Any]) -> str:
    chart = kline_window(symbol, position.get("opened_at"))
    bars = chart.get("bars") if isinstance(chart.get("bars"), list) else []
    entry_price = None
    try:
        entry_price = float(position.get("entry_price"))
    except Exception:
        entry_price = None
    if not bars or entry_price is None:
        return f"""
<div class="chart-empty">
  <b>K线图暂缺</b>
  <span>本地还没有 {h(symbol)} 入场附近足够 K线。系统继续用外部行情补缓存，后续会自动变完整。</span>
</div>
""".strip()
    parsed: list[dict[str, float]] = []
    for row in bars:
        try:
            parsed.append({
                "ts": _bar_float(row, 0),
                "open": _bar_float(row, 1),
                "high": _bar_float(row, 2),
                "low": _bar_float(row, 3),
                "close": _bar_float(row, 4),
                "volume": _bar_float(row, 5),
            })
        except Exception:
            continue
    if not parsed:
        return '<div class="chart-empty"><b>K线图暂缺</b><span>缓存格式不完整。</span></div>'
    width, height = 980, 300
    left, right, top, bottom = 54, 18, 20, 48
    price_top = top
    price_bottom = height - bottom
    highs = [bar["high"] for bar in parsed] + [entry_price]
    lows = [bar["low"] for bar in parsed] + [entry_price]
    high = max(highs)
    low = min(lows)
    span = high - low if high > low else max(high * 0.01, 1.0)

    def x_at(idx: int) -> float:
        if len(parsed) <= 1:
            return (left + width - right) / 2
        return left + idx * ((width - left - right) / (len(parsed) - 1))

    def y_at(price: float) -> float:
        return price_bottom - ((price - low) / span) * (price_bottom - price_top)

    candle_width = max(4.0, min(12.0, (width - left - right) / max(len(parsed), 1) * 0.56))
    candles: list[str] = []
    for idx, bar in enumerate(parsed):
        x = x_at(idx)
        y_high = y_at(bar["high"])
        y_low = y_at(bar["low"])
        y_open = y_at(bar["open"])
        y_close = y_at(bar["close"])
        color = "#22c55e" if bar["close"] >= bar["open"] else "#ef4444"
        body_y = min(y_open, y_close)
        body_h = max(2.0, abs(y_close - y_open))
        candles.append(
            f'<line x1="{x:.1f}" y1="{y_high:.1f}" x2="{x:.1f}" y2="{y_low:.1f}" stroke="{color}" stroke-width="1.4" />'
            f'<rect x="{x - candle_width / 2:.1f}" y="{body_y:.1f}" width="{candle_width:.1f}" height="{body_h:.1f}" rx="1.5" fill="{color}" opacity=".9" />'
        )
    entry_index = chart.get("entry_index")
    if isinstance(entry_index, int) and 0 <= entry_index < len(parsed):
        entry_x = x_at(entry_index)
    else:
        entry_x = x_at(len(parsed) // 2)
    entry_y = y_at(entry_price)
    side = str(position.get("side") or "").upper()
    source = report_text(chart.get("source") or "本地K线缓存")
    coverage = f"前{chart.get('before', 0)}根 / 后{chart.get('after', 0)}根"
    return f"""
<svg class="kline-svg" viewBox="0 0 {width} {height}" role="img" aria-label="{h(symbol)} 入场K线">
  <rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="#08111d" />
  <line x1="{left}" y1="{y_at(entry_price):.1f}" x2="{width-right}" y2="{y_at(entry_price):.1f}" stroke="#5d8cff" stroke-dasharray="5 5" opacity=".72" />
  {''.join(candles)}
  <line x1="{entry_x:.1f}" y1="{top}" x2="{entry_x:.1f}" y2="{price_bottom}" stroke="#2bd4d6" stroke-width="1.7" />
  <circle cx="{entry_x:.1f}" cy="{entry_y:.1f}" r="5.5" fill="#2bd4d6" stroke="#07111c" stroke-width="2" />
  <text x="{left}" y="{height-20}" fill="#8ea2bd" font-size="12">{h(symbol)} {h(side)}  入场 {h(fmt_plain(entry_price, 6))}  {h(coverage)}  数据源 {h(source)}</text>
  <text x="{width-right-130}" y="{top+14}" fill="#b8c7d9" font-size="12" text-anchor="end">最高 {h(fmt_plain(high, 6))}</text>
  <text x="{width-right-130}" y="{price_bottom-6}" fill="#b8c7d9" font-size="12" text-anchor="end">最低 {h(fmt_plain(low, 6))}</text>
</svg>
""".strip()


def plain_level(level: str) -> str:
    return level if level in {"good", "warn", "bad", "muted"} else "muted"


def plain_status(value: Any) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    mapping = {
        "ok": "正常",
        "good": "正常",
        "ready": "已准备好",
        "missing": "缺少数据",
        "data_gap": "数据缺口",
        "coverage_gap": "覆盖不足",
        "planned": "待运行",
        "plan_only": "只读计划",
        "complete": "任务完成",
        "complete_with_provider_gaps": "完成但有供应商缺口",
        "collecting_or_blocked": "采集中或有阻塞",
        "paused_request_budget": "请求预算暂停",
        "paused_time_budget": "时间预算暂停",
        "ready_for_plan_only_data_work": "只做离线数据工作",
        "run_staged_kline_depth_ingest_then_replay_review": "先补K线/深度，再复盘",
        "blocked_non_sample_gaps": "还有非样本阻塞",
        "waiting_for_samples_report_only": "只读等待样本",
        "preconditions_met_report_only": "只读条件已齐",
        "clear_non_sample_blockers_then_wait_for_samples": "先清非样本阻塞，再等样本",
        "collect_fresh_contextual_paired_samples": "继续收新上下文闭环样本",
        "manual_operator_review_before_any_upgrade": "人工复核后再升级",
        "stale_mirror_unknown": "镜像过期，暂不判断",
        "blocked": "被挡住",
        "watch": "观察中",
        "pass": "通过",
        "fail": "未通过",
    }
    if lower in mapping:
        return mapping[lower]
    if not text:
        return "缺少数据"
    return text.replace("_", " ")


def plain_strategy_reason(reason: Any, kind: str = "skip") -> str:
    raw = str(reason or "").strip()
    lower = raw.lower()
    if not raw:
        if kind == "failed":
            return "有开仓执行失败，要看详情确认是不是账户状态、交易所规则或风控拦截。"
        if kind == "close_failed":
            return "有平仓或强平失败，要优先看详情确认仓位是否还在。"
        return "有候选，但被策略规则挡住；这通常不是系统故障。"
    checks = [
        (("15m", "确认"), "有候选，但15分钟确认没有跟上，所以策略按规则没开仓。"),
        (("open_submitted_unconfirmed",), "订单已提交到交易所，但还没有确认成交成仓；系统不会先建本地假仓，会等回执或下一轮核对。"),
        (("open_unfilled",), "交易所收到了开仓请求，但当前回包没有成交数量；系统先不当作已开仓。"),
        (("open_confirm_account_state_unavailable",), "订单已经提交，但成交后的账户回执还没回来；系统会等用户流或受控确认补证，不能把它当成策略没信号。"),
        (("close_confirm_account_state_unavailable",), "平仓已经提交，但账户回执还没确认仓位消失；系统会继续补证，不能把它当成普通失败。"),
        (("confirm_account_state_unavailable",), "交易请求已发出，但成交后账户回执还不够新；这是确认链路问题，不是策略没有机会。"),
        (("fresh central account state unavailable",), "账户资料太旧，系统先避免误判仓位；恢复期会用已验证账户状态和用户流补新，不该长期挡住开仓。"),
        (("scanner_order_disabled",), "当前是观察模式，只记录信号，不允许真开仓。"),
        (("合约不存在",), "候选来自外部行情，但不在当前策略可交易/可模拟合约清单里；系统没有让它进入自建模拟账本，不是账本下单失败。"),
        (("symbol_not_found",), "候选来自外部行情，但不在当前策略可交易/可模拟合约清单里；系统没有让它进入自建模拟账本，不是账本下单失败。"),
        (("not listed",), "候选来自外部行情，但不在当前策略可交易/可模拟合约清单里；系统没有让它进入自建模拟账本，不是账本下单失败。"),
        (("invalid symbol",), "候选来自外部行情，但不在当前策略可交易/可模拟合约清单里；系统没有让它进入自建模拟账本，不是账本下单失败。"),
        (("cooldown",), "接口处在保护/冷却状态，系统先退避，不继续加压。"),
        (("-1003",), "交易所提示请求过多，系统应先退避，不能硬冲。"),
        (("418",), "交易所触发接口保护，系统应先等冷却清干净。"),
        (("429",), "请求频率被限制，系统应先降压等待。"),
        (("min_notional",), "订单金额不满足交易所最小下单规则，所以提前挡住。"),
        (("-4164",), "订单金额不满足交易所最小下单规则，所以提前挡住。"),
        (("same_symbol",), "同币种已有仓位，风控不允许重复叠仓。"),
        (("duplicate", "position"), "同币种已有仓位，风控不允许重复叠仓。"),
        (("insufficient", "balance"), "可用余额或保证金不够，系统没有强行开仓。"),
        (("risk",), "风险检查没通过，所以策略没有继续下单。"),
        (("kline",), "K线数据不够新或不完整，策略先跳过，避免用脏数据开仓。"),
        (("no data",), "行情数据不完整，策略先跳过，避免用脏数据开仓。"),
        (("score",), "分数还没到策略要求，属于正常筛选。"),
        (("threshold",), "还没达到策略阈值，属于正常筛选。"),
        (("can_trade=false",), "策略判断当前不适合交易，所以没有开仓。"),
        (("open_skipped",), "候选被策略门控挡住；这是筛选结果，不是服务挂了。"),
    ]
    for keys, message in checks:
        if all(key in lower for key in keys):
            return message
    if kind == "failed":
        return f"开仓执行失败，需看详情定位：{raw}"
    if kind == "close_failed":
        return f"平仓/强平执行失败，需看详情定位：{raw}"
    return f"候选被策略规则挡住：{raw}"


def decode_payload(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
    return bool(row)


def queue_summary(db_path: Path = RUNTIME_DIR / "binance_api_queue.sqlite3") -> dict[str, Any]:
    candidates = [db_path]
    mirror_db = MIRROR_RUNTIME_DIR / "binance_api_queue.sqlite3"
    if mirror_db not in candidates:
        candidates.append(mirror_db)
    summary_candidates = [
        RUNTIME_DIR / "binance_api_queue_summary_latest.json",
        MIRROR_RUNTIME_DIR / "binance_api_queue_summary_latest.json",
    ]
    db_path = next((path for path in candidates if path.exists()), db_path)
    if not db_path.exists():
        summary = read_first_json(*summary_candidates)
        if summary:
            return {
                "available": True,
                "active": int(summary.get("active") or 0),
                "cooldowns": int(summary.get("cooldowns") or 0),
                "last": summary.get("last") if isinstance(summary.get("last"), list) else [],
                "counts": summary.get("counts") if isinstance(summary.get("counts"), dict) else {},
                "source": "summary",
            }
        return {"available": False, "active": 0, "cooldowns": 0, "last": [], "counts": {}}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        active = conn.execute(
            "select count(*) from api_requests where status in ('queued','deferred','leased')"
        ).fetchone()[0]
        cooldowns = conn.execute(
            "select count(*) from api_cooldowns where until_ms > ?",
            (now_ms,),
        ).fetchone()[0]
        counts = {
            row["status"]: int(row["n"])
            for row in conn.execute("select status, count(*) n from api_requests group by status")
        }
        last = [dict(row) for row in conn.execute(
            "select rowid,label,scope,account,path,status,result_status,error from api_requests order by rowid desc limit 6"
        )]
        conn.close()
        return {"available": True, "active": int(active), "cooldowns": int(cooldowns), "counts": counts, "last": last}
    except Exception as exc:
        return {"available": False, "error": str(exc), "active": 0, "cooldowns": 0, "last": [], "counts": {}}


def event_summary(db_path: Path = EVENT_DB) -> dict[str, Any]:
    empty = {
        "available": False,
        "events": 0,
        "sentinel_scans": 0,
        "account_snapshots": 0,
        "latest_ts": None,
        "strategies": [],
        "open_close": {},
    }
    if not db_path.exists():
        return empty
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        out = dict(empty)
        out["available"] = True
        if table_exists(conn, "events"):
            out["events"] = int(conn.execute("select count(*) from events").fetchone()[0])
            row = conn.execute("select ts from events order by id desc limit 1").fetchone()
            out["latest_ts"] = parse_dt(row["ts"]) if row else None
            since = (datetime.now(CST) - timedelta(hours=24)).strftime("%Y-%m-%d")
            strategies = []
            for name in ("A/v11", "B/v16", "C/v14"):
                latest = conn.execute(
                    "select ts,event_type,category,payload_json from events where strategy=? order by id desc limit 1",
                    (name,),
                ).fetchone()
                counts = conn.execute(
                    """
                    select
                      sum(case when event_type='OPEN' or category='opened' then 1 else 0 end) as opens,
                      sum(case when event_type in ('CLOSE','FORCED_CLOSE') or category in ('closed','forced_close') then 1 else 0 end) as closes,
                      sum(case when event_type='OPEN_FAILED' or category='open_failed' then 1 else 0 end) as open_failed,
                      sum(case when event_type='OPEN_SKIPPED' or category='open_skipped' then 1 else 0 end) as open_skipped,
                      sum(case when event_type like '%CLOSE_FAILED%' or category like '%close_failed%' then 1 else 0 end) as close_failed
                    from events
                    where strategy=? and substr(ts,1,10) >= ?
                    """,
                    (name, since),
                ).fetchone()
                skip_reason = conn.execute(
                    """
                    select coalesce(nullif(reason,''), stage, category, event_type) as reason, count(*) n
                    from events
                    where strategy=? and event_type='OPEN_SKIPPED' and substr(ts,1,10) >= ?
                    group by coalesce(nullif(reason,''), stage, category, event_type)
                    order by n desc
                    limit 1
                    """,
                    (name, since),
                ).fetchone()
                failed_reason = conn.execute(
                    """
                    select coalesce(nullif(reason,''), stage, category, event_type) as reason, count(*) n
                    from events
                    where strategy=? and event_type='OPEN_FAILED' and substr(ts,1,10) >= ?
                    group by coalesce(nullif(reason,''), stage, category, event_type)
                    order by n desc
                    limit 1
                    """,
                    (name, since),
                ).fetchone()
                strategies.append({
                    "name": name,
                    "latest": parse_dt(latest["ts"]) if latest else None,
                    "opens": int(counts["opens"] or 0) if counts else 0,
                    "closes": int(counts["closes"] or 0) if counts else 0,
                    "open_failed": int(counts["open_failed"] or 0) if counts else 0,
                    "open_skipped": int(counts["open_skipped"] or 0) if counts else 0,
                    "close_failed": int(counts["close_failed"] or 0) if counts else 0,
                    "skip_reason": str(skip_reason["reason"] or "") if skip_reason else "",
                    "failed_reason": str(failed_reason["reason"] or "") if failed_reason else "",
                })
            out["strategies"] = strategies
        if table_exists(conn, "sentinel_scans"):
            out["sentinel_scans"] = int(conn.execute("select count(*) from sentinel_scans").fetchone()[0])
        if table_exists(conn, "account_snapshots"):
            out["account_snapshots"] = int(conn.execute("select count(*) from account_snapshots").fetchone()[0])
        conn.close()
        return out
    except Exception as exc:
        out = dict(empty)
        out["error"] = str(exc)
        return out


def account_for_strategy(account: dict[str, Any], strategy: str) -> dict[str, Any]:
    accounts = account.get("accounts") if isinstance(account.get("accounts"), list) else []
    for row in accounts:
        if isinstance(row, dict) and row.get("strategy") == strategy:
            return row
    return {}


def fee_estimate(notional: Any) -> tuple[str, str]:
    try:
        value = abs(float(notional))
    except Exception:
        value = 0.0
    if value <= 0:
        return "-", "没有名义价值，暂不能估手续费。"
    one_way = value * DEFAULT_TAKER_FEE_RATE
    return f"单边约 {fmt_plain(one_way, 4)} / 往返约 {fmt_plain(one_way * 2, 4)} USDT", "估算：按 taker 0.04%，不是交易所逐笔扣费流水。"


def position_upnl_class(value: Any) -> str:
    try:
        return "up" if float(value) >= 0 else "down"
    except Exception:
        return "muted"


def strategy_detail_html(strategy: str, account: dict[str, Any]) -> str:
    account_row = account_for_strategy(account, strategy)
    positions = account_row.get("positions") if isinstance(account_row.get("positions"), list) else []
    rows: list[str] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        notional = pos.get("notional")
        fee_text, fee_note = fee_estimate(notional)
        mark = pos.get("mark")
        quality = "交易所快照"
        if mark in {0, 0.0, "0", "0.0", None, ""}:
            quality = "账户快照缺 mark，浮盈亏按中心账户状态展示，需等下一次行情/账户回执补新。"
        rows.append(
            f"""
<tr>
  <td>真实持仓</td>
  <td>{h(pos.get('symbol'))}</td>
  <td>{h(pos.get('side'))}</td>
  <td>{h(fmt_plain(pos.get('qty')))}</td>
  <td>{h(fmt_plain(pos.get('entry')))}</td>
  <td>{h(fmt_plain(mark))}</td>
  <td class="{position_upnl_class(pos.get('upnl'))}">{h(number(pos.get('upnl'), 4))}</td>
  <td>{h(fmt_plain(notional, 4))}</td>
  <td>{h(fmt_plain(pos.get('margin'), 4))}</td>
  <td>{h(fee_text)}<small>{h(fee_note)}</small></td>
  <td>{h(quality)}</td>
</tr>
""".strip()
        )
    if not rows:
        rows.append(
            """
<tr>
  <td colspan="11">当前没有可展示持仓。若策略有信号但没开仓，先看“主要原因”和候选被挡住。</td>
</tr>
""".strip()
        )
    summary = (
        f"账户 {h(account_row.get('account') or '-')}: "
        f"持仓 {h(account_row.get('open_positions') or 0)}，"
        f"浮盈亏 {h(number(account_row.get('unrealized_pnl_usdt'), 4))} USDT，"
        f"可用 {h(number(account_row.get('available_usdt'), 2))} USDT"
        if account_row
        else "未找到该策略账户快照"
    )
    return f"""
<details class="strategy-detail">
  <summary>查看持仓盈亏 / 手续费</summary>
  <p class="detail-note">{summary}。浮盈亏优先用账户快照；手续费无成交流水时只估算。</p>
  <div class="table-scroll"><table class="position-table">
    <thead><tr><th>类型</th><th>币种</th><th>方向</th><th>数量</th><th>开仓价</th><th>标记价</th><th>浮盈亏</th><th>名义价值</th><th>保证金</th><th>手续费</th><th>可信度</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
</details>
"""


def strategy_rows(
    event: dict[str, Any],
    alerts: dict[str, Any],
    account: dict[str, Any] | None = None,
    *,
    include_details: bool = False,
    live_services: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    services = alerts.get("services") if isinstance(alerts.get("services"), dict) else {}
    if live_services:
        services = {**services, **{str(key): value for key, value in live_services.items()}}
    service_map = {
        "A/v11": "crypto-scanner.service",
        "B/v16": "crypto-scanner-v16.service",
    }
    by_name = {row["name"]: row for row in event.get("strategies") or [] if isinstance(row, dict)}
    rows = []
    for name in STRATEGY_NAMES:
        item = by_name.get(name, {})
        service = services.get(service_map[name], "unknown")
        open_failed = int(item.get("open_failed") or 0)
        close_failed = int(item.get("close_failed") or 0)
        open_skipped = int(item.get("open_skipped") or 0)
        level = "bad" if service != "active" else "good"
        note = "正常运行"
        raw_note = ""
        note_kind = "normal"
        if open_failed:
            raw_note = str(item.get("failed_reason") or "")
            note = plain_strategy_reason(raw_note, "failed")
            note_kind = "failed"
        elif close_failed:
            note = plain_strategy_reason("", "close_failed")
            note_kind = "close_failed"
        elif open_skipped:
            raw_note = str(item.get("skip_reason") or "")
            note = plain_strategy_reason(raw_note, "skip")
            note_kind = "skip"
        rows.append({
            "level": level,
            "name": name,
            "service": "运行中" if service == "active" else f"异常({service})",
            "age": age_text(item.get("latest")),
            "opens": str(item.get("opens", 0)),
            "closes": str(item.get("closes", 0)),
            "open_failed": str(open_failed),
            "close_failed": str(close_failed),
            "open_skipped": str(open_skipped),
            "note": note,
            "raw_note": raw_note,
            "note_kind": note_kind,
            "detail_html": strategy_detail_html(name, account or {}) if include_details else "",
        })
    return rows


def attention_items(limit: int = 8) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = read_first_json(ATTENTION_JSON, LIVE_ATTENTION_JSON, MIRROR_ATTENTION_JSON)
    items = [
        item for item in payload.get("items", [])
        if (
            isinstance(item, dict)
            and item.get("status") == "open"
            and str(item.get("priority") or "") in {"P0", "P1"}
        )
    ]
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    items.sort(key=lambda item: (rank.get(str(item.get("priority") or "P3"), 9), str(item.get("title") or "")))
    return payload.get("summary") or {}, items[:limit]


def attention_level_label(priority: Any) -> str:
    return {
        "P0": "马上处理",
        "P1": "需要你决定",
        "P2": "观察项",
        "P3": "记录",
    }.get(str(priority or ""), "事项")


def item_strategy(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("title", "evidence", "item_id"))
    match = re.search(r"\b(A/v11|B/v16|C/v14)\b", text)
    return match.group(1) if match else "策略"


def plain_attention_title(item: dict[str, Any]) -> str:
    category = str(item.get("category") or "")
    title = str(item.get("title") or "需要确认")
    priority = str(item.get("priority") or "")
    strategy = item_strategy(item)
    item_id = str(item.get("item_id") or "")
    if category == "策略回滚" or item_id.startswith("rollback:"):
        change = plain_attention_change_name(item)
        return f"{strategy} {change}上线后表现需要你复核"
    if category == "策略进化":
        if priority in {"P0", "P1"}:
            return f"{strategy} 有策略改动需要你决定"
        return f"{strategy} 有策略改进在观察"
    return title


def plain_attention_change_name(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("item_id", "title", "evidence")).lower()
    if "atr-stop-bands" in text:
        return "ATR止损带改动"
    if "overheat-cap-85" in text:
        return "过热上限 85 改动"
    if "trailing-pullback" in text:
        return "移动止盈回撤改动"
    if "replacement-quality" in text:
        return "换仓质量改动"
    if "confirm-soft-pass" in text or "confirmation-soft-pass" in text:
        return "确认条件放宽改动"
    return "策略改动"


def plain_attention_metrics(text: str) -> tuple[str | None, str | None]:
    pnl = None
    pf = None
    pnl_match = re.search(r"pnl_after_cost=([-+]?\d+(?:\.\d+)?)", text)
    pf_match = re.search(r"profit_factor=([-+]?\d+(?:\.\d+)?)", text)
    if pnl_match:
        pnl = pnl_match.group(1)
    if pf_match:
        pf = pf_match.group(1)
    return pnl, pf


def plain_attention_metric_rows(item: dict[str, Any]) -> list[tuple[str, str]]:
    text = str(item.get("evidence") or "")
    pnl, pf = plain_attention_metrics(text)
    rows: list[tuple[str, str]] = []
    if pnl:
        rows.append(("最近扣费后盈亏", f"{pnl} USDT"))
    if pf:
        rows.append(("收益因子 PF", pf))
    threshold_match = re.search(r"profit_factor=[-+]?\d+(?:\.\d+)?<([-+]?\d+(?:\.\d+)?)", text)
    if threshold_match:
        rows.append(("PF 警戒线", threshold_match.group(1)))
    rows.append(("当前状态", "低于继续放开线，需要你选下一步"))
    rows.append(("按钮含义", "会写入决策台账；继续收样会从待确认移除"))
    return rows


def plain_attention_evidence(item: dict[str, Any]) -> str:
    text = str(item.get("evidence") or "")
    category = str(item.get("category") or "")
    item_id = str(item.get("item_id") or "")
    if "HTTP 418" in text or "-1003" in text:
        return "以前触发过交易所接口保护；如果再次出现，要先停扩量，等冷却清干净。"
    if category == "策略回滚" or item_id.startswith("rollback:"):
        pnl, pf = plain_attention_metrics(text)
        if pnl or pf:
            bits = []
            if pnl:
                bits.append(f"扣费后盈亏约 {pnl} USDT")
            if pf:
                bits.append(f"收益因子 PF={pf}")
            return "最近评估窗口里 " + "，".join(bits) + "，低于系统的继续放开警戒线；所以提醒你复核这次改动是否还要继续跑。"
        return "这项已经上线过，但最近表现触发了复核线；现在要判断继续观察、收窄，还是准备回滚。"
    if category == "策略进化":
        if "small_live_monitoring" in text:
            return "现在只是小仓观察，不能当成已经验证好的正式升级。"
        if "shadow_validating" in text:
            return "还在影子验证，缺少真实成交或纸面撮合盈利证据。"
        if "ready_for_review" in text:
            return "已有一些证据，但还需要人工决定下一步。"
        if "样本不足" in text:
            return "样本还不够，先继续观察，不急着改实盘。"
    if text:
        return text[:180]
    return "没有更多说明。"


def plain_attention_action(item: dict[str, Any]) -> str:
    action = str(item.get("recommended_action") or "")
    category = str(item.get("category") or "")
    priority = str(item.get("priority") or "")
    if category in {"策略进化", "策略回滚"}:
        if "rollback" in action or category == "策略回滚":
            return "你要做：先点右侧“策略进化”或“完整旧版详情”看这项的盈亏、失败原因和样本数。看完后，如果接受继续收样，就点“我已读”；如果不接受，告诉我收窄 B/v16 或准备回滚。点“我已读”不会自动改策略。"
        if "shadow" in action:
            return "不用现在上线，继续收样；等有真实/纸面盈利证据再说。"
        return "先看详情，再决定继续观察还是暂停扩样。"
    if priority == "P0":
        return "先处理这个风险；确认已经解决或接受风险后，再点确认。"
    if priority == "P1":
        return "看一眼是否接受这个风险；接受或处理完后点确认。"
    return "不用现在处理，继续观察。"


def plain_attention_action_html(item: dict[str, Any]) -> str:
    category = str(item.get("category") or "")
    item_id = str(item.get("item_id") or "")
    if category in {"策略进化", "策略回滚"} or item_id.startswith(("rollback:", "evolution:")):
        metric_rows = "".join(
            f"<li><span>{h(label)}</span><b>{h(value)}</b></li>"
            for label, value in plain_attention_metric_rows(item)
        )
        return f"""
<div class="decision-box">
  <ul class="decision-facts">{metric_rows}</ul>
  <div class="decision-actions">
    <button class="decision-btn good" onclick="decideItem('{h(item_id)}','continue_observe',this)">继续收样</button>
    <button class="decision-btn warn" onclick="decideItem('{h(item_id)}','narrow_b_v16',this)">收窄 B/v16</button>
    <button class="decision-btn bad" onclick="decideItem('{h(item_id)}','prepare_rollback',this)">准备回滚</button>
  </div>
  <p class="decision-note">你在这里点选后会写入决策台账；不用再单独告诉我。收窄/回滚是执行请求，后续由执行链路处理并在台账留痕。</p>
</div>
""".strip()
    return h(plain_attention_action(item))


def attention_button_label(item: dict[str, Any]) -> str:
    category = str(item.get("category") or "")
    item_id = str(item.get("item_id") or "")
    if category in {"策略进化", "策略回滚"} or item_id.startswith(("rollback:", "evolution:")):
        return "我已读"
    return "确认"


def market_mover_phase(change_pct: Any, velocity_pct: Any = None) -> str:
    try:
        change = float(change_pct or 0.0)
    except Exception:
        change = 0.0
    try:
        velocity = float(velocity_pct or 0.0)
    except Exception:
        velocity = 0.0
    direction = "上涨" if change >= 0 else "下跌"
    start = "起涨" if change >= 0 else "起跌"
    abs_change = abs(change)
    abs_velocity = abs(velocity)
    aligned = (change >= 0 and velocity >= -0.05) or (change < 0 and velocity <= 0.05)
    if abs_change < 3:
        return f"{start}初段" if aligned or abs_velocity < 0.2 else f"{direction}初段反向波动"
    if abs_change >= 12 and abs_velocity < 0.4:
        return f"{direction}末段放缓"
    if abs_change >= 12 and not aligned:
        return f"{direction}末段回撤"
    if abs_change >= 8 and abs_velocity < 0.3:
        return f"{direction}末段放缓"
    if abs_velocity >= 0.7 and aligned:
        return f"{direction}中段加速"
    return f"{direction}中段"


def _mover_record_status(record: dict[str, Any]) -> str:
    event_type = str(record.get("event_type") or "").upper()
    scan_result = str(record.get("scan_result") or "").lower()
    reason = str(record.get("reason") or "").lower()
    if event_type == "OPEN":
        return "已开仓"
    if event_type == "OPEN_FAILED":
        return "执行失败"
    if event_type == "OPEN_SKIPPED":
        return "策略挡住"
    if event_type in {"SIGNAL", "SIGNAL_ONLY"}:
        return "候选信号"
    if any(key in scan_result or key in reason for key in ("reject", "skip", "fail", "blocked", "false")):
        return "未通过"
    return "已扫描"


def _mover_reason_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    priority = {"OPEN_FAILED": 0, "OPEN_SKIPPED": 1, "SENTINEL_SCANNED": 2, "SIGNAL": 3, "SIGNAL_ONLY": 3, "OPEN": 4}
    ranked = sorted(records, key=lambda row: priority.get(str(row.get("event_type") or "").upper(), 9))
    for record in ranked:
        if record.get("reason") or record.get("scan_result") or record.get("stage"):
            return record
    return ranked[0] if ranked else None


def _mover_strategy_filter_summary(records: list[dict[str, Any]]) -> str:
    if not records:
        return "未见策略扫描"
    grouped: dict[str, list[str]] = {
        "已开": [],
        "挡": [],
        "执行失败": [],
        "已扫": [],
        "未扫": [],
    }
    for name in STRATEGY_NAMES:
        strategy_records = [row for row in records if row.get("strategy") == name]
        if not strategy_records:
            grouped["未扫"].append(name)
            continue
        status = _mover_record_status(strategy_records[0])
        if status == "已开仓":
            grouped["已开"].append(name)
        elif status == "执行失败":
            grouped["执行失败"].append(name)
        elif status in {"策略挡住", "未通过"}:
            grouped["挡"].append(name)
        else:
            grouped["已扫"].append(name)
    return "；".join(f"{label}：{'、'.join(names)}" for label, names in grouped.items() if names)


def summarize_mover_diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "strategy_filter": "未见策略扫描",
            "no_entry_reason": "榜单进入观察池，但近24h没有对应策略事件；需等下一轮扫描或检查 symbol 覆盖。",
            "raw_no_entry_reason": "",
        }
    reason_record = _mover_reason_record(records)
    raw_reason = ""
    reason_text = "已有策略事件，但未记录明确阻塞原因。"
    if reason_record:
        raw_reason = str(reason_record.get("reason") or reason_record.get("scan_result") or reason_record.get("event_type") or "")
        kind = "failed" if str(reason_record.get("event_type") or "").upper() == "OPEN_FAILED" else "skip"
        reason_text = plain_strategy_reason(raw_reason, kind)
        stage = plain_status(reason_record.get("stage"))
        layer = plain_status(reason_record.get("layer"))
        if stage != "缺少数据" or layer != "缺少数据":
            reason_text = f"{reason_text} 阶段：{stage}；筛选层：{layer}。"
    return {
        "strategy_filter": _mover_strategy_filter_summary(records),
        "no_entry_reason": reason_text,
        "raw_no_entry_reason": raw_reason,
    }


def load_market_mover_diagnostics(market: dict[str, Any], db_path: Path = EVENT_DB, *, limit: int = 20) -> dict[str, dict[str, Any]]:
    movers = market.get("market_mover_preview") if isinstance(market.get("market_mover_preview"), list) else []
    symbols = [str(row.get("symbol") or "").upper() for row in movers[:limit] if isinstance(row, dict) and row.get("symbol")]
    if not symbols or not db_path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    since_date = (datetime.now(CST) - timedelta(hours=24)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for symbol in symbols:
            records: list[dict[str, Any]] = []
            if table_exists(conn, "events"):
                for row in conn.execute(
                    """
                    select ts,strategy,symbol,event_type,reason,category,stage,layer,side,score,payload_json
                    from events
                    where upper(symbol)=? and event_type in ('SIGNAL','SIGNAL_ONLY','OPEN','OPEN_SKIPPED','OPEN_FAILED')
                      and substr(ts,1,10) >= ?
                    order by id desc
                    limit 18
                    """,
                    (symbol, since_date),
                ):
                    payload = decode_payload(row["payload_json"])
                    records.append({
                        "ts": row["ts"],
                        "strategy": row["strategy"],
                        "symbol": row["symbol"],
                        "event_type": row["event_type"],
                        "reason": row["reason"] or payload.get("skip_reason") or payload.get("sentinel_reason") or payload.get("reason"),
                        "stage": row["stage"] or payload.get("decision_stage") or payload.get("stage"),
                        "layer": row["layer"] or payload.get("filter_layer") or payload.get("layer"),
                        "scan_result": payload.get("sentinel_scan_result") or "",
                    })
            if table_exists(conn, "sentinel_scans"):
                for row in conn.execute(
                    """
                    select ts,strategy,symbol,event_type,reason,category,decision_stage,filter_layer,
                           change_pct,velocity_pct,quote_volume,scan_result,payload_json
                    from sentinel_scans
                    where upper(symbol)=?
                      and substr(ts,1,10) >= ?
                    order by id desc
                    limit 18
                    """,
                    (symbol, since_date),
                ):
                    payload = decode_payload(row["payload_json"])
                    records.append({
                        "ts": row["ts"],
                        "strategy": row["strategy"],
                        "symbol": row["symbol"],
                        "event_type": row["event_type"] or "SENTINEL_SCANNED",
                        "reason": row["reason"] or payload.get("sentinel_reason") or payload.get("reason"),
                        "stage": row["decision_stage"] or payload.get("decision_stage"),
                        "layer": row["filter_layer"] or payload.get("filter_layer"),
                        "scan_result": row["scan_result"] or payload.get("sentinel_scan_result") or "",
                        "change_pct": row["change_pct"],
                        "velocity_pct": row["velocity_pct"],
                    })
            records.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
            out[symbol] = summarize_mover_diagnostics(records)
        conn.close()
    except Exception:
        return {}
    return out


def market_mover_rows(state: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    paper = state.get("paper_exchange") if isinstance(state.get("paper_exchange"), dict) else {}
    diagnostics = state.get("mover_diagnostics") if isinstance(state.get("mover_diagnostics"), dict) else {}
    movers = market.get("market_mover_preview") if isinstance(market.get("market_mover_preview"), list) else []
    positions = paper.get("positions") if isinstance(paper.get("positions"), list) else []
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        symbol = str(pos.get("symbol") or "").upper()
        if symbol:
            by_symbol.setdefault(symbol, []).append(pos)

    rows: list[dict[str, Any]] = []
    for idx, mover in enumerate(movers[:limit], start=1):
        if not isinstance(mover, dict):
            continue
        symbol = str(mover.get("symbol") or "").upper()
        try:
            change = float(mover.get("change_pct") or 0.0)
        except Exception:
            change = 0.0
        try:
            velocity = float(mover.get("velocity_pct") or 0.0)
        except Exception:
            velocity = 0.0
        diag = diagnostics.get(symbol) if isinstance(diagnostics.get(symbol), dict) else {}
        try:
            price_tick = float(mover.get("price_tick_pct"))
        except Exception:
            price_tick = None
        reason = str(mover.get("reason") or "")
        phase = str(diag.get("phase") or mover.get("phase") or market_mover_phase(change, velocity))
        if reason == "起涨捕捉" or phase.startswith("起涨"):
            desired = "long"
            move_label = "起涨"
        elif reason == "起跌捕捉" or phase.startswith("起跌"):
            desired = "short"
            move_label = "起跌"
        else:
            desired = "long" if change >= 0 else "short"
            move_label = "上涨" if change >= 0 else "下跌"
        matched = by_symbol.get(symbol, [])
        pnl = sum(float(pos.get("unrealized_pnl") or 0.0) for pos in matched)
        side_bits: list[str] = []
        correct = "未进场"
        if matched:
            aligned = 0
            for pos in matched:
                side = str(pos.get("side") or "").lower()
                strategy = str(pos.get("strategy") or "-")
                if side == desired:
                    aligned += 1
                side_bits.append(f"{strategy} {side or '-'}")
            if aligned == len(matched):
                correct = "顺势"
            elif aligned == 0:
                correct = "逆势"
            else:
                correct = "多空混合"
        result = "未进场"
        if matched:
            result = "赚钱" if pnl > 0 else "亏钱" if pnl < 0 else "持平"
        rows.append({
            "rank": idx,
            "symbol": symbol,
            "reason": reason or move_label,
            "move_label": move_label,
            "change_pct": change,
            "velocity_pct": velocity,
            "price_tick_pct": price_tick,
            "phase": phase,
            "quote_volume": mover.get("quote_volume"),
            "source": ",".join(str(x) for x in (mover.get("sources") or [mover.get("source") or "-"])),
            "scan": diag.get("strategy_filter") or ("已进扫描池；未见 A/B/C 策略筛选记录" if not matched else "已进扫描池"),
            "no_entry_reason": "已进入模拟账本" if matched else diag.get("no_entry_reason") or "榜单进入观察池，但 report 未读到对应策略事件；等下一轮扫描或查 symbol 覆盖。",
            "raw_no_entry_reason": diag.get("raw_no_entry_reason") or "",
            "entry": "已进场" if matched else "未进场",
            "direction": correct,
            "positions": "；".join(side_bits) if side_bits else "-",
            "pnl": pnl,
            "result": result,
        })
    return rows


def render_market_movers(state: dict[str, Any]) -> str:
    rows = market_mover_rows(state)
    if not rows:
        return '<p class="empty">今天还没有可展示的涨跌榜/突然加速榜。</p>'
    entered = sum(1 for row in rows if row["entry"] == "已进场")
    aligned = sum(1 for row in rows if row["direction"] == "顺势")
    pnl = sum(float(row.get("pnl") or 0.0) for row in rows)
    def entry_result(row: dict[str, Any]) -> str:
        if row.get("entry") != "已进场":
            raw = f"<small>原始原因：{h(row['raw_no_entry_reason'])}</small>" if row.get("raw_no_entry_reason") else ""
            return f'<b class="muted">未进场</b><small>{h(row["no_entry_reason"])}</small>{raw}'
        return (
            f'<b class="{position_upnl_class(row["pnl"])}">{h(row["result"])} {h(number(row["pnl"], 4))}</b>'
            f'<small>{h(row["direction"])}；{h(row["positions"])}</small>'
        )
    body = "".join(
        f"""
<tr>
  <td>{h(row['rank'])}</td>
  <td>{h(row['symbol'])}</td>
  <td>{h(row['reason'])}<small>{h(row['phase'])}；{h(row.get('move_label') or ('上涨' if row['change_pct'] >= 0 else '下跌'))} 24h {h(number(row['change_pct'], 2))}%；速度 {h(number(row.get('velocity_pct'), 2))}%{('；tick ' + h(number(row.get('price_tick_pct'), 2)) + '%') if row.get('price_tick_pct') not in (None, '') else ''}</small></td>
  <td>{h(row['scan'])}</td>
  <td>{entry_result(row)}</td>
  <td>{h(fmt_plain(row.get('quote_volume'), 2))}<small>{h(row['source'])}</small></td>
</tr>
""".strip()
        for row in rows
    )
    return f"""
<div class="mover-summary">
  <div><span>榜单数量</span><b>{h(len(rows))}</b></div>
  <div><span>已进场</span><b>{h(entered)}</b></div>
  <div><span>顺势方向</span><b>{h(aligned)}</b></div>
  <div><span>榜单持仓浮盈亏</span><b class="{position_upnl_class(pnl)}">{h(number(pnl, 4))} USDT</b></div>
</div>
<div class="table-scroll"><table class="mover-table">
  <thead><tr><th>#</th><th>币种</th><th>信号阶段</th><th>策略判断</th><th>进场结果</th><th>成交额来源</th></tr></thead>
  <tbody>{body}</tbody>
</table></div>
<p class="empty">未进场只展示原因；方向和浮盈亏只在已有模拟持仓时展示。</p>
"""


def cleanup_summary() -> dict[str, Any]:
    # Read-only coarse sizing. Data maintenance/retention performs actual moves.
    paths = [
        ("runtime", ROOT / "runtime"),
        ("logs", ROOT / "logs"),
        ("reports", ROOT / "reports"),
        ("archive", ROOT / "archive"),
        ("server mirror", ROOT / "server_logs_tencent"),
    ]
    rows = []
    total = 0
    for label, path in paths:
        size = 0
        if path.exists():
            try:
                size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            except Exception:
                size = 0
        total += size
        rows.append({"label": label, "bytes": size, "mb": round(size / 1024 / 1024, 1)})
    return {"total_mb": round(total / 1024 / 1024, 1), "rows": rows}


def build_state() -> dict[str, Any]:
    live_context = read_first_json(RUNTIME_DIR / "live_context_summary_latest.json", MIRROR_RUNTIME_DIR / "live_context_summary_latest.json")
    alerts = read_alerts_json()
    account = read_first_json(RUNTIME_DIR / "account_snapshot_latest.json", MIRROR_RUNTIME_DIR / "account_snapshot_latest.json")
    evolution = read_first_json(RUNTIME_DIR / "strategy_evolution_latest.json", MIRROR_RUNTIME_DIR / "strategy_evolution_latest.json")
    replay = read_first_json(RUNTIME_DIR / "replay_readiness_latest.json", MIRROR_RUNTIME_DIR / "replay_readiness_latest.json")
    auto_upgrade = read_first_json(RUNTIME_DIR / "auto_upgrade_readiness_latest.json", MIRROR_RUNTIME_DIR / "auto_upgrade_readiness_latest.json")
    waiting_progress = read_first_json(RUNTIME_DIR / "waiting_period_progress_latest.json", MIRROR_RUNTIME_DIR / "waiting_period_progress_latest.json")
    paper_real_calibration_plan = read_first_json(RUNTIME_DIR / "paper_real_calibration_plan_latest.json", MIRROR_RUNTIME_DIR / "paper_real_calibration_plan_latest.json")
    waiting = read_first_json(RUNTIME_DIR / "waiting_period_optimization_latest.json", MIRROR_RUNTIME_DIR / "waiting_period_optimization_latest.json")
    parity = read_first_json(RUNTIME_DIR / "replay_live_parity_latest.json", MIRROR_RUNTIME_DIR / "replay_live_parity_latest.json")
    skeleton = read_first_json(RUNTIME_DIR / "long_term_skeleton_latest.json", MIRROR_RUNTIME_DIR / "long_term_skeleton_latest.json")
    research = read_first_json(RUNTIME_DIR / "research_store_summary_latest.json", MIRROR_RUNTIME_DIR / "research_store_summary_latest.json")
    kline = read_first_json(RUNTIME_DIR / "research_kline_backfill_latest.json", MIRROR_RUNTIME_DIR / "research_kline_backfill_latest.json")
    historical_kline = read_best_historical_kline_json(RUNTIME_DIR / "historical_kline_backfill_latest.json", MIRROR_RUNTIME_DIR / "historical_kline_backfill_latest.json")
    historical_kline_incremental = read_best_historical_kline_json(
        RUNTIME_DIR / "historical_kline_incremental_latest.json",
        MIRROR_RUNTIME_DIR / "historical_kline_incremental_latest.json",
    )
    backtest_module = read_first_json(BACKTEST_MODULE_JSON, MIRROR_BACKTEST_MODULE_JSON)
    depth = read_first_json(RUNTIME_DIR / "research_depth_backfill_latest.json", MIRROR_RUNTIME_DIR / "research_depth_backfill_latest.json")
    paper_exchange = read_live_runtime_json("paper_exchange_latest.json")
    research_paper = read_live_runtime_json("research_paper_strategy_latest.json")
    market = read_live_runtime_json("market_data_cache.json")
    mover_diagnostics = load_market_mover_diagnostics(market if isinstance(market, dict) else {})
    microstructure = read_live_runtime_json("market_microstructure_latest.json")
    reset = read_live_runtime_json("testnet_data_reset_latest.json")
    q = queue_summary()
    ev = event_summary()
    att_summary, att_items = attention_items()
    account_summary = account.get("summary") if isinstance(account.get("summary"), dict) else {}
    stale = account_summary.get("stale_accounts") if isinstance(account_summary.get("stale_accounts"), list) else []
    active_alerts = [
        a for a in alerts.get("alerts", [])
        if isinstance(a, dict) and a.get("level") in {"bad", "warn"}
    ]
    alerts_ts = parse_dt(alerts.get("ts"))
    live_services = live_context.get("services") if isinstance(live_context.get("services"), dict) else {}
    alerts_stale = bool(live_services and (not alerts_ts or (datetime.now(CST) - alerts_ts).total_seconds() > 1800))
    if alerts_stale:
        active_alerts = []
    bad_alerts = [a for a in active_alerts if a.get("level") == "bad"]
    overall = "good"
    if bad_alerts or q.get("cooldowns") or q.get("active"):
        overall = "bad"
    elif active_alerts or stale:
        overall = "warn"
    return {
        "generated_at": datetime.now(CST),
        "overall": overall,
        "live_context": live_context,
        "alerts_stale": alerts_stale,
        "alerts": alerts,
        "account": account,
        "account_summary": account_summary,
        "evolution": evolution,
        "replay": replay,
        "auto_upgrade": auto_upgrade,
        "waiting_progress": waiting_progress,
        "paper_real_calibration_plan": paper_real_calibration_plan,
        "waiting": waiting,
        "parity": parity,
        "skeleton": skeleton,
        "research": research,
        "kline": kline,
        "historical_kline": historical_kline,
        "historical_kline_incremental": historical_kline_incremental,
        "backtest_module": backtest_module,
        "depth": depth,
        "paper_exchange": paper_exchange,
        "research_paper": research_paper,
        "market": market,
        "mover_diagnostics": mover_diagnostics,
        "microstructure": microstructure,
        "reset": reset,
        "queue": q,
        "event": ev,
        "attention_summary": att_summary,
        "attention_items": att_items,
        "cleanup": cleanup_summary(),
    }


def status_text(state: dict[str, Any]) -> str:
    if state["overall"] == "bad":
        return "先别扩张：有红灯要处理"
    if state["overall"] == "warn":
        return "可以运行：有黄灯要观察"
    return "可以运行：当前没有红灯"


def waiting_top100_text(waiting: dict[str, Any]) -> tuple[str, str]:
    summary = waiting.get("summary") if isinstance(waiting.get("summary"), dict) else {}
    coverage = waiting.get("scan_coverage") if isinstance(waiting.get("scan_coverage"), dict) else {}
    if coverage.get("coverage_status") == "stale_mirror_unknown":
        return "未知", "warn"
    try:
        pct = float(summary.get("top100_pct") or 0.0)
    except Exception:
        pct = 0.0
    level = "good" if pct >= 80 else "warn" if pct >= 35 else "bad"
    return f"{summary.get('top100_scanned', 0)}/{coverage.get('target_count', 100)}", level


def render_badges(state: dict[str, Any]) -> str:
    account = state["account_summary"]
    paper = state.get("paper_exchange") or {}
    alerts = state["alerts"]
    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    micro = state.get("microstructure") if isinstance(state.get("microstructure"), dict) else {}
    skeleton_summary = (state["skeleton"].get("summary") or {}) if isinstance(state["skeleton"], dict) else {}
    source_count = len(market.get("sources") or [])
    available_symbols = len(market.get("available_symbols") or [])
    alert_value = "偏旧" if state.get("alerts_stale") else str(alerts.get("alert_count", 0))
    alert_level = "warn" if state.get("alerts_stale") else "bad" if alerts.get("status") == "bad" else "warn" if alerts.get("status") == "warn" else "good"
    items = [
        ("三策略", "运行中", "good"),
        ("行情源", f"{source_count} 路 / {available_symbols} 币", "good" if source_count >= 2 else "warn"),
        ("盘口/CVD", f"{micro.get('fresh_symbols_240s', 0)}/{micro.get('coverage_symbols', 0)}", "good" if int(micro.get("fresh_symbols_240s") or 0) >= 80 else "warn"),
        ("模拟持仓", str(paper.get("open_positions", account.get("open_positions", 0))), "good"),
        ("模拟浮盈亏", f"{number(paper.get('total_unrealized_pnl', account.get('unrealized_pnl_usdt')))} USDT", "good"),
        ("告警", alert_value, alert_level),
        ("长期骨架", f"{skeleton_summary.get('ready_bones', 0)}/{skeleton_summary.get('total_bones', 0)}", "good"),
    ]
    return "".join(
        f'<article class="metric {plain_level(level)}"><span>{h(label)}</span><b>{h(value)}</b></article>'
        for label, value, level in items
    )


def render_paper_exchange(state: dict[str, Any]) -> str:
    paper = state.get("paper_exchange") or {}
    if not paper:
        return '<p class="empty">模拟账本还没生成。系统会先生成持仓、盯市盈亏和手续费。</p>'
    by_strategy = paper.get("by_strategy") if isinstance(paper.get("by_strategy"), dict) else {}
    paper_ts = parse_dt(paper.get("ts"))
    fidelity = paper.get("fidelity") if isinstance(paper.get("fidelity"), dict) else {}
    lifecycle = paper.get("strategy_lifecycle") if isinstance(paper.get("strategy_lifecycle"), dict) else {}
    research_paper = state.get("research_paper") if isinstance(state.get("research_paper"), dict) else {}
    strategy_names = list(STRATEGY_NAMES)
    research_names: list[str] = []
    for name in sorted(by_strategy):
        row = by_strategy.get(name) if isinstance(by_strategy.get(name), dict) else {}
        status = lifecycle.get(name)
        is_research = name.startswith("R-") or status == "research_paper"
        if is_research and name not in research_names:
            research_names.append(name)
        should_show = (not is_research and int(row.get("positions") or 0) > 0) or (is_research and int(row.get("positions") or 0) > 0)
        if name not in strategy_names and should_show:
            strategy_names.append(name)
    cards = []
    for idx, name in enumerate(strategy_names):
        row = by_strategy.get(name) if isinstance(by_strategy.get(name), dict) else {}
        status = lifecycle.get(name) or ("research_paper" if name.startswith("R-") else "active")
        cards.append(
            f"""
<button class="paper-card strategy-tab {'active' if idx == 0 else ''}" type="button" data-strategy="{h(name)}" onclick="showPaperStrategy('{h(name)}')">
  <span>{h(name)} · {h(report_text(status))}</span>
  <b>{h(row.get('positions', 0))} 仓 / {h(number(row.get('unrealized_pnl'), 4))} USDT</b>
  <p>权益 {h(amount(row.get('equity'), 2))}，已实现 {h(number(row.get('realized_pnl'), 4))}，手续费 {h(amount(row.get('fees_paid'), 4))}</p>
</button>
""".strip()
        )
    positions = paper.get("positions") if isinstance(paper.get("positions"), list) else []
    panels: list[str] = []
    for idx, name in enumerate(strategy_names):
        strategy_positions = [pos for pos in positions if isinstance(pos, dict) and pos.get("strategy") == name]
        body_rows: list[str] = []
        for pos_idx, pos in enumerate(strategy_positions[:80]):
            upnl = pos.get("unrealized_pnl")
            detail_id = re.sub(r"[^A-Za-z0-9_-]+", "-", f"paper-{name}-{pos.get('symbol')}-{pos_idx}")
            chart = render_kline_svg(str(pos.get("symbol") or ""), pos)
            opened = age_text(parse_dt(pos.get("opened_at")))
            body_rows.append(
                f"""
<tr class="position-row" onclick="togglePositionDetail('{h(detail_id)}')">
  <td>{h(pos.get('symbol'))}<small>{h(opened)} 开</small></td>
  <td>{h(pos.get('side'))}</td>
  <td>{h(fmt_plain(pos.get('qty')))}</td>
  <td>{h(fmt_plain(pos.get('entry_price')))}</td>
  <td>{h(fmt_plain(pos.get('mark_price')))}</td>
  <td class="{position_upnl_class(upnl)}">{h(number(upnl, 4))}</td>
  <td>{h(fmt_plain(pos.get('notional'), 4))}</td>
  <td>{h(fmt_plain(pos.get('margin'), 4))}</td>
  <td>{h(amount(pos.get('fees_paid'), 4))}</td>
  <td>{h(report_text(pos.get('mark_source') or '外部行情'))}</td>
  <td><button class="mini-btn" type="button">展开</button></td>
</tr>
<tr id="{h(detail_id)}" class="position-detail-row">
  <td colspan="11">
    <div class="position-detail-grid">
      <div class="chart-wrap">{chart}</div>
      <div class="position-facts">
        <b>{h(pos.get('symbol'))} 持仓详情</b>
        <p>订单号 {h(pos.get('order_id') or '-')}</p>
        <p>杠杆 {h(fmt_plain(pos.get('leverage'), 2))}x，原因 {h(report_text(pos.get('reason') or '-'))}</p>
        <p>盯市刷新 {h(age_text(parse_dt(pos.get('mark_updated_at'))))}</p>
        <p>说明：图中蓝线是入场价，竖线是入场附近K线。数据不足时先显示现有缓存。</p>
      </div>
    </div>
  </td>
</tr>
""".strip()
            )
        if not body_rows:
            body_rows.append('<tr><td colspan="11">当前策略暂无模拟持仓。</td></tr>')
        panels.append(
            f"""
<div class="paper-panel {'active' if idx == 0 else ''}" data-strategy="{h(name)}">
  <div class="table-scroll"><table class="paper-table">
    <thead><tr><th>币种</th><th>方向</th><th>数量</th><th>开仓价</th><th>盯市价</th><th>浮盈亏</th><th>名义价值</th><th>保证金</th><th>手续费</th><th>价格源</th><th></th></tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table></div>
</div>
""".strip()
        )
    research_line = ""
    if research_paper:
        pressure = research_paper.get("api_pressure") if isinstance(research_paper.get("api_pressure"), dict) else {}
        signal_brief = research_signal_brief(research_paper)
        research_positions = sum(int((by_strategy.get(name) or {}).get("positions") or 0) for name in research_names)
        research_upnl = sum(num((by_strategy.get(name) or {}).get("unrealized_pnl")) for name in research_names)
        research_open_notional = sum(num((by_strategy.get(name) or {}).get("open_notional")) for name in research_names)
        strategy_bits = " / ".join(
            f"{name.replace('R-', '')}:{int((by_strategy.get(name) or {}).get('positions') or 0)}仓"
            for name in research_names
        ) or "等待首轮信号"
        research_line = (
            "<div class='research-paper-strip'>"
            "<div><span>研究账本采样</span>"
            f"<b>{h(report_text(research_paper.get('status') or 'waiting'))} · {h(len(research_names))} 条</b>"
            f"<small>{h(strategy_bits)}</small></div>"
            "<div><span>本轮动作</span>"
            f"<b>开 {h(research_paper.get('opened_this_run', 0))} / 平 {h(research_paper.get('closed_this_run', 0))}</b>"
            f"<small>扫描 {h(research_paper.get('symbol_count', 0))} 币；{h(signal_brief)}</small></div>"
            "<div><span>研究持仓</span>"
            f"<b>{h(research_positions)} 仓 / {h(number(research_upnl, 4))} USDT</b>"
            f"<small>名义价值 {h(number(research_open_notional, 2))} USDT；未触发就不展开空表</small></div>"
            "<div><span>API 压力</span>"
            f"<b>直连 {h(pressure.get('direct_exchange_requests', 0))}</b>"
            f"<small>{h(report_text(pressure.get('market_data_source') or '只读中心缓存'))}</small></div>"
            "</div>"
        )
    return f"""
<div class="paper-summary">
  <div><span>总权益</span><b>{h(amount(paper.get('total_equity'), 2))} USDT</b></div>
  <div><span>总浮盈亏</span><b class="{position_upnl_class(paper.get('total_unrealized_pnl'))}">{h(number(paper.get('total_unrealized_pnl'), 4))} USDT</b></div>
  <div><span>开仓数</span><b>{h(paper.get('open_positions', 0))}</b></div>
  <div><span>盯市刷新</span><b>{h(age_text(paper_ts))}</b></div>
</div>
{research_line}
<div class="paper-cards">{''.join(cards)}</div>
<div class="paper-panels">{''.join(panels)}</div>
<p class="empty">这是自建模拟账本：不下真实单。价格：{h(report_text(fidelity.get('price') or 'OKX/本地K线'))}；时间：{h(report_text(fidelity.get('time') or '按 runner 刷新'))}；滑点：{h(report_text(fidelity.get('slippage') or '非真实撮合'))}；手续费：{h(report_text(fidelity.get('fees') or '按账本费率'))}。</p>
"""


def render_fresh_start(state: dict[str, Any]) -> str:
    reset = state.get("reset") if isinstance(state.get("reset"), dict) else {}
    db_reset = reset.get("db_reset") if isinstance(reset.get("db_reset"), dict) else {}
    after = db_reset.get("counts_after") if isinstance(db_reset.get("counts_after"), dict) else {}
    paper = state.get("paper_exchange") if isinstance(state.get("paper_exchange"), dict) else {}
    event = state.get("event") if isinstance(state.get("event"), dict) else {}
    archive_root = reset.get("archive_root") or "暂无本轮归档"
    reset_at = parse_dt(reset.get("generated_at"))
    status = "已从零开始" if reset.get("apply") and after else "等待清理"
    rows = [
        ("清理状态", status, f"归档位置：{archive_root}"),
        ("事件库", f"{event.get('events', 0)} 条", "清零后这里只应出现新 paper/open/close/scan 事件。"),
        ("模拟账本", f"{paper.get('open_positions', 0)} 仓", "主 PnL 只看新 paper exchange，不混旧真实残留。"),
        ("清理时间", age_text(reset_at), "归档保留证据，当前运行从 reset receipt 之后重新算。"),
    ]
    return "".join(
        f'<article class="info"><span>{h(title)}</span><b>{h(value)}</b><p>{h(body)}</p></article>'
        for title, value, body in rows
    )


def render_evolution_readiness(state: dict[str, Any]) -> str:
    paper = state.get("paper_exchange") if isinstance(state.get("paper_exchange"), dict) else {}
    auto_upgrade = state.get("auto_upgrade") if isinstance(state.get("auto_upgrade"), dict) else {}
    auto_summary = auto_upgrade.get("summary") if isinstance(auto_upgrade.get("summary"), dict) else {}
    fills = paper.get("recent_fills") if isinstance(paper.get("recent_fills"), list) else []
    opens = sum(1 for row in fills if isinstance(row, dict) and row.get("action") == "OPEN")
    closes = sum(1 for row in fills if isinstance(row, dict) and row.get("action") == "CLOSE")
    positions = int(paper.get("open_positions") or 0)
    verdict = "能复盘骨架，暂不能升级策略"
    if closes >= 30:
        verdict = "可以开始小样本进化复核"
    elif positions >= 15:
        verdict = "正在收持仓样本，等平仓闭环"
    rows = [
        ("当前判断", verdict, "不是只等持仓数量；要等开仓、持仓、平仓、费用、行情上下文成套闭环。"),
        (
            "自动升级闸门",
            plain_status(auto_upgrade.get("status") or "missing"),
            f"只读报告；Auto/Apply 都是 no。非样本阻塞 {int(auto_summary.get('non_sample_blockers') or 0)}，样本阻塞 {int(auto_summary.get('sample_blockers') or 0)}。",
        ),
        ("开仓样本", str(opens), "足够看执行和持仓展示，但还不足以证明策略优劣。"),
        ("平仓样本", str(closes), "进化需要 CLOSED 样本。只有浮盈亏还不能算胜率、PF、回撤。"),
        ("下一步", plain_status(auto_upgrade.get("next_action") or "继续收完整交易"), "优先自然产生 CLOSE；满 30 笔闭环后再看参数升级，满 100 笔更可靠。"),
    ]
    return "".join(
        f'<article class="info"><span>{h(title)}</span><b>{h(value)}</b><p>{h(body)}</p></article>'
        for title, value, body in rows
    )


def render_strategy_table(rows: list[dict[str, str]]) -> str:
    body = "".join(
        f"""
<tr>
  <td><span class="dot {plain_level(row['level'])}"></span>{h(row['name'])}</td>
  <td>{h(row['service'])}</td>
  <td>{h(row['age'])}</td>
  <td>{h(row['opens'])}</td>
  <td>{h(row['closes'])}</td>
  <td>{h(row['open_failed'])}</td>
  <td>{h(row['close_failed'])}</td>
  <td>{h(row['open_skipped'])}</td>
  <td class="reason">{h(row.get('note') or plain_strategy_reason(row.get('raw_note') or '', row.get('note_kind') or 'skip'))}{('<small>原始原因：' + h(row['raw_note']) + '</small>') if row.get('raw_note') else ''}{row.get('detail_html', '')}</td>
</tr>
""".strip()
        for row in rows
    )
    return f"""
<div class="table-scroll"><table class="strategy-table">
  <thead><tr><th>策略</th><th>服务</th><th>最新数据</th><th>24h开仓</th><th>24h平仓</th><th>开仓执行失败</th><th>平仓/强平失败</th><th>候选被挡住</th><th>主要原因</th></tr></thead>
  <tbody>{body}</tbody>
</table></div>
"""


def render_attention(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<p class="empty">暂无需要你确认的 P0/P1 事项。P2 观察项在完整详情里，不占用首页确认区。</p>'
    rows = []
    for item in items:
        item_id = str(item.get("item_id") or "")
        priority = str(item.get("priority") or "")
        title = plain_attention_title(item)
        evidence = plain_attention_evidence(item)
        action_html = plain_attention_action_html(item)
        button = attention_button_label(item)
        is_strategy_attention = str(item.get("category") or "") in {"策略进化", "策略回滚"} or item_id.startswith(("rollback:", "evolution:"))
        subtitle = "这不是服务故障，是上线后表现复核提醒。" if is_strategy_attention else "这是运行提醒；确认后会从首页待确认区移除。"
        final_button = (
            f'<button class="icon-btn" onclick="ackItem(\'{h(item_id)}\', this)">{h(button)}</button>'
            if not is_strategy_attention
            else ""
        )
        rows.append(
            f"""
<tr data-item="{h(item_id)}">
  <td><b>{h(attention_level_label(priority))}</b><small>{h(priority)}</small></td>
  <td>{h(title)}<small>{h(subtitle)}</small></td>
  <td>{h(evidence)}</td>
  <td>{action_html}</td>
  <td>{final_button}</td>
</tr>
""".strip()
        )
    return f"""
<table>
  <thead><tr><th>级别</th><th>事项</th><th>为什么出现</th><th>你现在要做什么</th><th></th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def render_cards(state: dict[str, Any]) -> str:
    evolution = state["evolution"].get("summary") or {}
    expansion = evolution.get("expansion_readiness") if isinstance(evolution.get("expansion_readiness"), dict) else {}
    replay = state["replay"]
    replay_summary = replay.get("summary") if isinstance(replay.get("summary"), dict) else {}
    parity_summary = state["parity"].get("summary") if isinstance(state["parity"].get("summary"), dict) else {}
    research = state["research"]
    kline_acceptance = research.get("kline_acceptance") if isinstance(research.get("kline_acceptance"), dict) else {}
    waiting_progress = state.get("waiting_progress") if isinstance(state.get("waiting_progress"), dict) else {}
    historical_incremental = state.get("historical_kline_incremental") if isinstance(state.get("historical_kline_incremental"), dict) else {}
    incremental_progress = historical_incremental.get("progress") if isinstance(historical_incremental.get("progress"), dict) else {}
    waiting_summary = waiting_progress.get("summary") if isinstance(waiting_progress.get("summary"), dict) else {}
    b_gap = waiting_progress.get("b_v16_context_gap") if isinstance(waiting_progress.get("b_v16_context_gap"), dict) else {}
    calibration = state.get("paper_real_calibration_plan") if isinstance(state.get("paper_real_calibration_plan"), dict) else {}
    paper = state.get("paper_exchange") if isinstance(state.get("paper_exchange"), dict) else {}
    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    micro = state.get("microstructure") if isinstance(state.get("microstructure"), dict) else {}
    rows = [
        ("模拟账本", f"{paper.get('open_positions', 0)} 仓 / {number(paper.get('total_unrealized_pnl'), 4)} USDT", "这是当前主 PnL 入口。点击下方策略表，可看每个持仓和入场K线。"),
        ("行情覆盖", f"{len(market.get('available_symbols') or [])} 币 / Top {len(market.get('top_symbols') or [])}", f"来源：{report_text(','.join(market.get('sources') or []) or '外部公开行情')}。"),
        ("盘口/CVD", f"{micro.get('fresh_symbols_240s', 0)} 新鲜", "给 B/v16 和后续复盘用。只存紧凑特征，不存全量原始 tick。"),
        ("策略升级样本", f"可考虑 {expansion.get('ready_count', 0)} / 继续收样 {expansion.get('maturing_count', 0)}", f"过去24小时还缺 {expansion.get('missing_samples_24h', 0)} 个样本。先让系统自然交易，不靠拍脑袋放大。"),
        (
            "等待期推进",
            f"{waiting_summary.get('ready_or_active', 0)}/{waiting_summary.get('tasks', 0)} 项",
            f"watch {waiting_summary.get('watch', 0)}，bad {waiting_summary.get('bad', 0)}；自动升级、自动回滚、自动调参仍是关闭。",
        ),
        (
            "B/v16上下文",
            f"open缺 {waiting_summary.get('b_v16_missing_open', b_gap.get('missing_open', 0))} / ATR缺 {waiting_summary.get('b_v16_missing_atr', b_gap.get('missing_atr', 0))}",
            "只等新 OPEN/CLOSE 带源周期、ATR、paper fill；旧样本不硬补成可升级证据。",
        ),
        (
            "纸实校准",
            f"{calibration.get('pairs', 0)}/{calibration.get('min_pairs', 20)} 对",
            "这里只是 plan-only 校准门槛；未批准、不启动真实小单、不解除自动升级 blocker。",
        ),
        ("回放验收", plain_status(replay.get("status")), f"已准备 {replay_summary.get('ready_components', 0)}/{replay_summary.get('total_components', 0)} 块；下一步：{plain_status(replay.get('next_action'))}。"),
        ("同输入审计", f"{float(parity_summary.get('pass_rate_pct') or 0):.1f}% 通过", f"同一批输入下，已验 {parity_summary.get('gate_cases', 0)} 个策略判断，不一致 {parity_summary.get('mismatched', 0)} 个。"),
        ("K线/深度", plain_status(kline_acceptance.get("status")), "这是以后回测和升级策略的燃料。第一版先看是否在稳定积累，不急着一次补满。"),
        (
            "每日历史增量",
            f"{plain_status(historical_incremental.get('status') or 'missing')} / {int(incremental_progress.get('written_rows') or 0)} 行",
            "每天低速补最近3天到同一回测仓。只跑 Tencent timer，不改三策略扫描频率，不用私有/下单接口。",
        ),
    ]
    return "".join(
        f'<article class="info"><span>{h(title)}</span><b>{h(value)}</b><p>{h(body)}</p></article>'
        for title, value, body in rows
    )


def render_historical_kline_progress(state: dict[str, Any]) -> str:
    payload = state.get("historical_kline") if isinstance(state.get("historical_kline"), dict) else {}
    if not payload:
        return """
<div class="history-progress">
  <div class="history-bar"><span style="width:0%"></span></div>
  <div class="history-grid">
    <div><span>状态</span><b>未启动</b><small>还没有历史K线进度文件。</small></div>
    <div><span>安全边界</span><b>不影响扫描</b><small>报表刷新只读文件，不打历史API。</small></div>
    <div><span>API</span><b>未使用</b><small>启动离线命令后才会低速拉取。</small></div>
    <div><span>范围</span><b>Top30 / 一年</b><small>15m、30m、1h、4h。</small></div>
  </div>
</div>
""".strip()
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    universe = payload.get("universe") if isinstance(payload.get("universe"), dict) else {}
    last = payload.get("last_task") if isinstance(payload.get("last_task"), dict) else {}
    incremental = state.get("historical_kline_incremental") if isinstance(state.get("historical_kline_incremental"), dict) else {}
    incremental_progress = incremental.get("progress") if isinstance(incremental.get("progress"), dict) else {}
    incremental_cfg = incremental.get("config") if isinstance(incremental.get("config"), dict) else {}
    pct = max(0.0, min(100.0, float(progress.get("percent") or 0.0)))
    symbols = universe.get("symbols") if isinstance(universe.get("symbols"), list) else []
    body = f"""
<div class="history-progress">
  <div class="history-bar"><span style="width:{pct:.2f}%"></span></div>
  <div class="history-grid">
    <div><span>状态</span><b>{h(plain_status(payload.get('status')))}</b><small>{h(payload.get('mode') or 'plan_only')}；更新 {h(age_text(parse_dt(payload.get('generated_at'))))}</small></div>
    <div><span>进度</span><b>{pct:.2f}%</b><small>{int(progress.get('completed_requests') or 0)} 完成 / {int(progress.get('total_tasks') or 0)} 任务，已跳过 {int(progress.get('skipped_existing') or 0)}</small></div>
    <div><span>数据量</span><b>{int(progress.get('written_rows') or 0)} 行</b><small>预计 {int(progress.get('planned_bars_estimate') or 0)} 根；失败 {int(progress.get('failed_requests') or 0)}</small></div>
    <div><span>限速</span><b>{h(cfg.get('max_rps', '-'))} req/s</b><small>私有/下单请求 {h(str(payload.get('binance_requests_enabled')))}；扫描频率改动 {h(str(payload.get('strategy_frequency_change')))}（不影响三策略扫描频率）</small></div>
    <div><span>覆盖质量</span><b>{h(plain_status(quality.get('status') or '-'))}</b><small>{int(quality.get('covered_symbol_count') or 0)}/{int(quality.get('target_symbol_count') or 0)} 币；{int(quality.get('covered_symbol_interval_count') or 0)}/{int(quality.get('target_symbol_interval_count') or 0)} 币种周期；缺口 {int(quality.get('unavailable_symbol_interval_count') or 0)}，部分 {int(quality.get('partial_symbol_interval_count') or 0)}</small></div>
    <div><span>每日增量</span><b>{h(plain_status(incremental.get('status') or 'missing'))}</b><small>最近 {h(incremental_cfg.get('days', '-'))} 天；已写 {int(incremental_progress.get('written_rows') or 0)} 行；限速 {h(incremental_cfg.get('max_rps', '-'))} req/s；更新 {h(age_text(parse_dt(incremental.get('generated_at'))))}</small></div>
  </div>
  <div class="history-detail">
    <b>质量说明</b>
    <span>{h(quality.get('note') or '任务进度和覆盖质量分开看；100% 只代表当前任务队列无待处理。')}</span>
  </div>
  <div class="history-detail">
    <b>Universe</b>
    <span>{h(', '.join(symbols[:30]) or '-')}</span>
  </div>
  <div class="history-detail">
    <b>最近任务</b>
    <span>{h(last.get('symbol') or '-')} {h(last.get('interval') or '-')} {h(last.get('start') or '-')} → {h(last.get('end') or '-')}；provider {h(last.get('provider') or '-')}；rows {h(last.get('rows') or 0)}</span>
  </div>
</div>
""".strip()
    return body


def backtest_fmt(value: Any, digits: int = 2, signed: bool = False) -> str:
    try:
        number_value = float(value)
    except Exception:
        return "-"
    prefix = "+" if signed and number_value > 0 else ""
    return f"{prefix}{number_value:.{digits}f}".rstrip("0").rstrip(".")


def backtest_svg(points: list[dict[str, Any]], *, key: str = "equity", height: int = 160) -> str:
    usable = [row for row in points if isinstance(row, dict) and row.get(key) is not None]
    if len(usable) < 2:
        return '<div class="backtest-empty-chart">暂无足够曲线数据</div>'
    values = [num(row.get(key)) for row in usable]
    min_v = min(values)
    max_v = max(values)
    span = max(max_v - min_v, 1e-9)
    width = 720
    plot = []
    for idx, value in enumerate(values):
        x = idx / max(1, len(values) - 1) * width
        y = height - ((value - min_v) / span * (height - 18) + 9)
        plot.append(f"{x:.1f},{y:.1f}")
    color = "#39d98a" if values[-1] >= values[0] else "#ff6b6b"
    first = backtest_fmt(values[0], 2)
    last = backtest_fmt(values[-1], 2)
    return f"""
<div class="backtest-chart">
  <svg viewBox="0 0 {width} {height}" role="img" aria-label="权益曲线">
    <polyline points="{h(' '.join(plot))}" fill="none" stroke="{color}" stroke-width="3" vector-effect="non-scaling-stroke" />
    <line x1="0" y1="{height - 9}" x2="{width}" y2="{height - 9}" stroke="rgba(255,255,255,.12)" />
  </svg>
  <div class="backtest-chart-caption"><span>起点 {h(first)}</span><span>终点 {h(last)}</span></div>
</div>
""".strip()


def backtest_summary_report(job: dict[str, Any]) -> str:
    if not job:
        return ""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    recommendation = result.get("recommendation") if isinstance(result.get("recommendation"), dict) else {}
    sweep = result.get("parameter_sweep") if isinstance(result.get("parameter_sweep"), dict) else {}
    review = sweep.get("anti_overfit_review") if isinstance(sweep.get("anti_overfit_review"), dict) else {}
    benchmark = result.get("benchmark") if isinstance(result.get("benchmark"), dict) else {}
    net = num(summary.get("net_profit_usdt"))
    trades = int(summary.get("trades") or 0)
    pf = num(summary.get("profit_factor"))
    dd = num(summary.get("max_drawdown_pct"))
    buy_hold = benchmark.get("buy_hold_return_pct")
    if trades < 30:
        verdict = "样本偏少"
        advice = "先扩大币种或周期复测；不用于升级。"
    elif net > 0 and pf >= 1.1 and dd <= 20 and review.get("status") == "passed_research_only":
        verdict = "可进入人工研究复核"
        advice = "仍需 fresh paper 样本和跨周期稳定性；不能自动应用。"
    elif net > 0:
        verdict = "全窗口盈利但门控未过"
        advice = "重点看 OOS、邻近参数稳定性和不同币种表现，避免拟合。"
    else:
        verdict = "当前不建议调优应用"
        advice = "优先找亏损来源、降低复杂参数数量，避免用单窗口强行优化。"
    return f"""
<div class="backtest-advice-grid">
  <div><span>结论</span><b>{h(verdict)}</b><small>{h(advice)}</small></div>
  <div><span>收益/风险</span><b>{h(backtest_fmt(net, 2, signed=True))} USDT</b><small>回撤 {h(backtest_fmt(dd, 2))}%；PF {h(backtest_fmt(pf, 2))}；交易 {trades}</small></div>
  <div><span>基准对比</span><b>{h(backtest_fmt(buy_hold, 2, signed=True))}%</b><small>买入持有平均收益；只做参考。</small></div>
  <div><span>建议状态</span><b>{h(recommendation.get('action') or 'research_review_only')}</b><small>{h(recommendation.get('reason') or '-')}; OOS {h(review.get('status') or '-')}</small></div>
</div>
""".strip()


def render_backtest_recent_jobs(backtest: dict[str, Any]) -> str:
    recent = backtest.get("recent_jobs") if isinstance(backtest.get("recent_jobs"), dict) else {}
    jobs = recent.get("jobs") if isinstance(recent.get("jobs"), list) else []
    if not jobs:
        return '<p class="empty">暂无历史任务列表。任务文件保存在 runtime/backtest_jobs，提交后会显示最近任务。</p>'
    rows = []
    for job in jobs[:20]:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id") or "-")
        rows.append(
            f"""
<tr>
  <td>{h(job_id)}<small>{h(job.get('created_at') or '-')}</small></td>
  <td>{h(job.get('strategy') or '-')} / {h(job.get('interval') or '-')}<small>{h(', '.join(job.get('symbols') or []) or '-')}</small></td>
  <td>{h(job.get('status') or '-')}<small>{h(job.get('engine_parity') or 'research_adapter')}</small></td>
  <td>{h(backtest_fmt(job.get('net_profit_usdt'), 2, signed=True))}<small>交易 {h(job.get('trades') or 0)}；回撤 {h(backtest_fmt(job.get('max_drawdown_pct'), 2))}%；PF {h(backtest_fmt(job.get('profit_factor'), 2))}</small></td>
  <td>{h(job.get('recommendation_reason') or '-')}<small>OOS {h(job.get('anti_overfit_status') or '-')}</small></td>
  <td><button class="mini-btn danger" type="button" onclick="deleteBacktestJob('{h(job_id)}')">删除</button></td>
</tr>
""".strip()
        )
    total = int(recent.get("total_jobs") or len(jobs))
    display_limit = int(recent.get("display_limit") or 20)
    return f"""
<details class="backtest-history-collapse">
  <summary>历史任务列表（{h(total)}）<span>默认收起；展开后可查看最近 {h(display_limit)} 条并删除单条任务记录。</span></summary>
  <div class="table-scroll">
  <table class="backtest-table">
    <thead><tr><th>任务</th><th>策略/周期</th><th>状态</th><th>结果</th><th>建议</th><th>操作</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  </div>
  <p class="backtest-note">删除只删 `runtime/backtest_jobs` 里的任务记录，不删 Tencent 历史 K线仓。</p>
</details>
<p class="backtest-note">历史任务不是只保留一个：服务端保留 job 文件，首页只展示最近 {h(display_limit)} 条；当前总数 {h(total)}。</p>
""".strip()


def render_backtest_trade_rows(trades: list[Any]) -> str:
    rows = []
    for trade in list(trades)[-80:]:
        if not isinstance(trade, dict):
            continue
        rows.append(
            f"""
<tr>
  <td>{h(trade.get('symbol') or '-')}<small>{h(trade.get('side') or '-')} / {h(trade.get('interval') or '-')}</small></td>
  <td>{h(trade.get('entry_ts') or '-')}<small>{h(backtest_fmt(trade.get('entry_price'), 8))}</small></td>
  <td>{h(trade.get('exit_ts') or '-')}<small>{h(backtest_fmt(trade.get('exit_price'), 8))}</small></td>
  <td>{h(backtest_fmt(trade.get('net_pnl_usdt'), 4, signed=True))}<small>fee {h(backtest_fmt(trade.get('fee_usdt'), 4))}；bars {h(trade.get('bars_held') or 0)}</small></td>
  <td>{h(trade.get('exit_reason') or '-')}<small>score {h(backtest_fmt(trade.get('score'), 2))} / threshold {h(backtest_fmt(trade.get('threshold'), 2))}</small></td>
</tr>
""".strip()
        )
    if not rows:
        return '<p class="empty">暂无交易明细。通常是历史 K线不足、策略未触发、或周期/方向组合无信号。</p>'
    return f"""
<div class="table-scroll backtest-trades-scroll">
  <table class="backtest-table backtest-trades-table">
    <thead><tr><th>标的</th><th>开仓</th><th>平仓</th><th>盈亏</th><th>原因</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
""".strip()


def render_backtest_parameter_controls(schema: dict[str, Any]) -> str:
    strategies = schema.get("strategies") if isinstance(schema.get("strategies"), dict) else {}
    active = strategies.get("A/v11") if isinstance(strategies.get("A/v11"), list) else []
    controls = []
    for row in active:
        if not isinstance(row, dict):
            continue
        key = row.get("key") or ""
        controls.append(
            f"""
<label class="param-control" data-param-key="{h(key)}">
  <span><input type="checkbox" name="param_enabled" value="{h(key)}"> {h(row.get('label') or key)}</span>
  <input name="param_value_{h(key)}" value="" placeholder="{h(row.get('min'))} - {h(row.get('max'))}" inputmode="decimal" data-min="{h(row.get('min'))}" data-max="{h(row.get('max'))}" data-step="{h(row.get('step'))}">
  <small>{h(key)}；范围 {h(row.get('min'))} - {h(row.get('max'))}。{h(row.get('note') or '')}</small>
</label>
""".strip()
        )
    return "".join(controls)


def render_backtest_module(state: dict[str, Any]) -> str:
    historical = state.get("historical_kline") if isinstance(state.get("historical_kline"), dict) else {}
    quality = historical.get("quality") if isinstance(historical.get("quality"), dict) else {}
    incremental = state.get("historical_kline_incremental") if isinstance(state.get("historical_kline_incremental"), dict) else {}
    incremental_progress = incremental.get("progress") if isinstance(incremental.get("progress"), dict) else {}
    backtest = state.get("backtest_module") if isinstance(state.get("backtest_module"), dict) else {}
    caps = backtest.get("capabilities") if isinstance(backtest.get("capabilities"), dict) else {}
    schema = backtest.get("parameter_schema") if isinstance(backtest.get("parameter_schema"), dict) else {}
    latest = backtest.get("latest_job") if isinstance(backtest.get("latest_job"), dict) else {}
    latest_spec = latest.get("spec") if isinstance(latest.get("spec"), dict) else {}
    latest_result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
    latest_summary = latest_result.get("summary") if isinstance(latest_result.get("summary"), dict) else {}
    latest_charts = latest_result.get("charts") if isinstance(latest_result.get("charts"), dict) else {}
    latest_trades = latest_result.get("trades") if isinstance(latest_result.get("trades"), list) else []
    monthly = latest_charts.get("monthly_returns") if isinstance(latest_charts.get("monthly_returns"), list) else []
    history_done = historical_kline_complete(historical)
    if not history_done:
        return render_historical_kline_progress(state)

    metric_cards = ""
    if latest:
        metric_cards = f"""
  <div class="backtest-metrics">
    <div><span>净收益</span><b>{h(backtest_fmt(latest_summary.get('net_profit_usdt'), 2, signed=True))} USDT</b><small>收益率 {h(backtest_fmt(latest_summary.get('return_pct'), 2, signed=True))}%</small></div>
    <div><span>最大回撤</span><b>{h(backtest_fmt(latest_summary.get('max_drawdown_pct'), 2))}%</b><small>胜率 {h(backtest_fmt(latest_summary.get('win_rate_pct'), 2))}%</small></div>
    <div><span>Profit Factor</span><b>{h(backtest_fmt(latest_summary.get('profit_factor'), 2))}</b><small>交易 {h(latest_summary.get('trades') or 0)}；均值 {h(backtest_fmt(latest_summary.get('avg_trade_usdt'), 4, signed=True))}</small></div>
    <div><span>成本</span><b>{h(backtest_fmt(latest_summary.get('fees_usdt'), 2))} USDT</b><small>滑点/冲击 {h(backtest_fmt(latest_summary.get('slippage_usdt'), 2))}</small></div>
  </div>
""".rstrip()

    monthly_rows = "".join(
        f"<tr><td>{h(row.get('month') or '-')}</td><td>{h(backtest_fmt(row.get('return_pct'), 2, signed=True))}%</td></tr>"
        for row in monthly[-12:]
        if isinstance(row, dict)
    )
    monthly_html = (
        f"""
<div class="table-scroll">
  <table class="backtest-table backtest-monthly-table">
    <thead><tr><th>月份</th><th>收益率</th></tr></thead>
    <tbody>{monthly_rows}</tbody>
  </table>
</div>
""".strip()
        if monthly_rows
        else '<p class="empty">暂无月度收益数据。</p>'
    )

    latest_html = (
        f"""
<div class="backtest-latest">
  <div class="history-detail">
    <b>最新任务</b>
    <span>{h(latest.get('job_id') or '-')}；{h(latest_spec.get('strategy') or '-')} / {h(latest_spec.get('interval') or '-')} / {h(', '.join(latest_spec.get('symbols') or []) or '-')}；{h(latest_spec.get('period_days') or '-')} 天；{h(latest.get('status') or latest_result.get('status') or '-')}；{h(latest_result.get('engine_parity') or 'research_adapter')}。</span>
  </div>
  {backtest_summary_report(latest)}
  {metric_cards}
  <div class="backtest-detail-grid">
    <div>
      <h3>权益 / PnL 曲线</h3>
      {backtest_svg(latest_charts.get('equity_curve') if isinstance(latest_charts.get('equity_curve'), list) else [])}
    </div>
    <div>
      <h3>月度收益</h3>
      {monthly_html}
    </div>
  </div>
  <div class="backtest-results">
    <h3>详细开平仓记录</h3>
    {render_backtest_trade_rows(latest_trades)}
  </div>
</div>
""".strip()
        if latest
        else '<p class="empty">暂无回测任务。提交后会在 Tencent 历史仓执行研究级回测，并返回指标、曲线和交易明细；结果不自动改策略。</p>'
    )
    schema_json = json.dumps(schema, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")

    return f"""
<div class="history-progress backtest-module">
  <script type="application/json" id="backtestParameterSchema">{schema_json}</script>
  <div class="history-grid">
    <div><span>历史基线</span><b>已完成</b><small>{int(quality.get('covered_symbol_count') or 0)}/{int(quality.get('target_symbol_count') or 0)} 币；{int(quality.get('covered_symbol_interval_count') or 0)}/{int(quality.get('target_symbol_interval_count') or 0)} 币种周期；质量 {h(plain_status(quality.get('status') or '-'))}</small></div>
    <div><span>每日增量</span><b>{h(plain_status(incremental.get('status') or 'missing'))}</b><small>最近写入 {int(incremental_progress.get('written_rows') or 0)} 行；只跑 Tencent 低速 timer。</small></div>
    <div><span>前端任务</span><b>{h(plain_status(caps.get('job_submit_api', True)))}</b><small>提交后执行只读历史仓回测；Aliyun 会委托 Tencent 历史仓。</small></div>
    <div><span>策略收益计算</span><b>{h(plain_status(caps.get('strategy_replay_adapter', False)))}</b><small>A/B research adapter 可用；C/v14 已退役；D/E/F 使用独立研究报告，不自动改策略。</small></div>
  </div>
  <form class="backtest-form" onsubmit="submitBacktestJob(event)">
    <label><span>策略</span><select name="strategy"><option>A/v11</option><option>B/v16</option></select></label>
    <label><span>币种</span><input name="symbols" value="BTCUSDT" maxlength="260"></label>
    <label><span>分时</span><select name="interval"><option>15m</option><option>30m</option><option selected>1h</option><option>4h</option></select></label>
    <label><span>周期</span><select name="period_days"><option value="90">90天</option><option value="180">180天</option><option value="365" selected>365天</option></select></label>
    <label><span>方向</span><select name="direction"><option value="strategy_default" selected>策略默认</option><option value="long">只多</option><option value="short">只空</option><option value="both">多空</option></select></label>
    <label><span>本金</span><input name="capital_usdt" value="10000" inputmode="decimal"></label>
    <label><span>手续费 bps</span><input name="fee_bps" value="4" inputmode="decimal"></label>
    <label><span>滑点 bps</span><input name="slippage_bps" value="0" inputmode="decimal"></label>
    <label><span>参数变体数</span><input name="parameter_variants" value="1" inputmode="numeric"></label>
    <div class="backtest-param-panel wide">
      <div class="param-head"><b>可调参数</b><small>勾选后才进入回测；最多 {h(schema.get('max_tuned_parameters') or 3)} 个参数，最多 {h(schema.get('max_parameter_variants') or 24)} 组变体。别用一堆参数硬拟合。</small></div>
      <div id="backtestParamControls" class="param-controls">{render_backtest_parameter_controls(schema)}</div>
    </div>
    <label class="wide backtest-advanced"><span>高级 JSON</span><textarea name="params" rows="3">{{}}</textarea><small>上方控件会自动生成；只在需要手写额外研究参数时修改。</small></label>
    <div class="backtest-actions">
      <button class="icon-btn" type="submit">创建回测任务</button>
      <span id="backtestStatus" class="refresh-status">反拟合门控启用；结果只做研究建议，不自动改策略。</span>
    </div>
  </form>
  <div class="backtest-results">
    <h3>最新回测报告</h3>
    {latest_html}
    <h3>历史任务列表</h3>
    {render_backtest_recent_jobs(backtest)}
  </div>
</div>
""".strip()


def render_history_or_backtest_panel(state: dict[str, Any]) -> str:
    historical = state.get("historical_kline") if isinstance(state.get("historical_kline"), dict) else {}
    if historical_kline_complete(historical):
        return ""
    title = "历史数据拉取进度"
    body = render_historical_kline_progress(state)
    return f"""
  <section class="panel" id="backtest">
    <h2>{h(title)}</h2>
    {body}
  </section>
""".rstrip()


def render_cleanup(state: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{h(row['label'])}</td><td>{h(row['mb'])} MB</td><td>{'保留/按维护计划归档'}</td></tr>"
        for row in state["cleanup"]["rows"]
    )
    return f"""
<table>
  <thead><tr><th>目录</th><th>大小</th><th>处理原则</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
"""


def render_release_gate(state: dict[str, Any]) -> str:
    waiting = state.get("waiting") or {}
    summary = waiting.get("summary") if isinstance(waiting.get("summary"), dict) else {}
    readiness = waiting.get("readiness") if isinstance(waiting.get("readiness"), dict) else {}
    checks = [
        ("队列清空", int(summary.get("active_requests") or 0) == 0, f"当前 {summary.get('active_requests', 0)}"),
        ("无冷却", int(summary.get("active_cooldowns") or 0) == 0, f"当前 {summary.get('active_cooldowns', 0)}"),
        ("无坏请求", int(summary.get("recent_bad") or 0) == 0, f"当前 {summary.get('recent_bad', 0)}"),
        ("不提频", not bool(readiness.get("can_raise_frequency")), "保持保护"),
    ]
    rows = "".join(
        f'<div><span class="dot {"good" if ok else "warn"}"></span><b>{h(label)}</b><small>{h(detail)}</small></div>'
        for label, ok, detail in checks
    )
    return f"""
<div class="gate-grid">{rows}</div>
<p class="empty">判断：{h(readiness.get("decision") or waiting.get("status") or "缺少等待期报表")}。{h(readiness.get("reason") or "")}</p>
"""


def render_html() -> str:
    state = build_state()
    live_services = state.get("live_context", {}).get("services") if isinstance(state.get("live_context"), dict) else {}
    strategies = strategy_rows(state["event"], state["alerts"], state["account"], live_services=live_services if isinstance(live_services, dict) else {})
    generated = state["generated_at"].strftime("%Y-%m-%d %H:%M:%S")
    alerts = state["alerts"].get("alerts") if isinstance(state["alerts"].get("alerts"), list) else []
    if state.get("alerts_stale"):
        alert_list = '<li class="warn"><b>自动告警文件偏旧</b><span>首页已改看当前服务/模拟账本数据；旧施工告警不再占主屏。</span></li>'
    else:
        alert_list = "".join(
            f'<li class="{plain_level(a.get("level"))}"><b>{h(a.get("title"))}</b><span>{h(report_text(a.get("body")))}</span></li>'
            for a in alerts[:6] if isinstance(a, dict)
        ) or '<li class="good"><b>无红灯</b><span>当前没有需要立即停机的告警。</span></li>'
    event = state["event"]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REPORT_REFRESH_SECONDS}">
<title>AutoTrading 决策入口</title>
<style>
:root {{
  --bg:#070b12; --panel:#0d1420; --panel2:#101a29; --panel3:#121f31;
  --ink:#edf4ff; --muted:#8ea2bd; --soft:#b8c7d9; --line:#213044;
  --good:#21d18b; --warn:#f4b740; --bad:#ff5b6e; --up:#22c55e; --down:#ef4444;
  --blue:#5d8cff; --cyan:#2bd4d6; --violet:#8b7cff; --shadow:0 24px 70px rgba(0,0,0,.35);
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; background:radial-gradient(circle at 20% 0%, rgba(43,212,214,.18), transparent 34%), linear-gradient(180deg,#08111e 0%,#070b12 42%,#09101a 100%); color:var(--ink); font:15px/1.58 "Inter","Segoe UI",Arial,sans-serif; }}
.app-shell {{ min-height:100vh; display:grid; grid-template-columns:240px minmax(0,1fr); }}
.side-rail {{ position:sticky; top:0; height:100vh; padding:24px 18px; background:linear-gradient(180deg,#0b1320,#070b12); border-right:1px solid var(--line); }}
.brand {{ display:flex; align-items:center; gap:10px; margin-bottom:28px; }}
.brand-mark {{ width:34px; height:34px; border-radius:8px; background:linear-gradient(135deg,var(--cyan),var(--blue)); box-shadow:0 0 30px rgba(43,212,214,.28); }}
.brand b {{ display:block; font-size:15px; }} .brand span {{ display:block; color:var(--muted); font-size:12px; }}
.nav {{ display:grid; gap:8px; }}
.nav a {{ color:var(--soft); text-decoration:none; padding:10px 12px; border-radius:8px; border:1px solid transparent; background:transparent; }}
.nav a.active,.nav a:hover {{ color:var(--ink); border-color:var(--line); background:#101827; }}
.rail-note {{ position:absolute; left:18px; right:18px; bottom:22px; color:var(--muted); font-size:12px; border:1px solid var(--line); border-radius:8px; padding:12px; background:#0b1320; }}
.wrap {{ max-width:1680px; width:100%; margin:0 auto; padding:24px 28px 40px; }}
header {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:18px; align-items:start; margin-bottom:18px; }}
h1 {{ margin:0; font-size:30px; letter-spacing:0; font-weight:850; }}
.sub {{ color:var(--muted); margin-top:6px; max-width:920px; }}
.status {{ padding:10px 14px; border-radius:8px; font-weight:850; color:#06101a; background:var(--muted); border:1px solid rgba(255,255,255,.12); box-shadow:var(--shadow); }}
.status.good {{ background:var(--good); }} .status.warn {{ background:var(--warn); }} .status.bad {{ background:var(--bad); color:#fff; }}
.metrics {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(168px, 1fr)); gap:12px; margin:14px 0 18px; }}
.metric,.panel,.info {{ background:linear-gradient(180deg,rgba(18,31,49,.96),rgba(13,20,32,.96)); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); }}
.metric {{ padding:15px; min-height:98px; position:relative; overflow:hidden; }}
.metric:after {{ content:""; position:absolute; left:14px; right:14px; bottom:10px; height:3px; border-radius:99px; background:linear-gradient(90deg,var(--blue),transparent); opacity:.55; }}
.metric span,.info span,.paper-summary span,.paper-card span {{ color:var(--muted); display:block; font-size:12px; letter-spacing:.02em; }}
.metric b {{ display:block; font-size:22px; margin-top:8px; color:var(--ink); word-break:keep-all; }}
.metric.good {{ border-top:3px solid var(--good); }} .metric.warn {{ border-top:3px solid var(--warn); }} .metric.bad {{ border-top:3px solid var(--bad); }}
.panel {{ padding:16px; margin-bottom:14px; }}
.panel h2 {{ margin:0 0 12px; font-size:17px; font-weight:850; color:#f7fbff; }}
.cards {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; }}
.info {{ padding:14px; min-height:120px; background:linear-gradient(180deg,#101b2b,#0b1320); }}
.info b {{ display:block; font-size:18px; margin:6px 0; color:#f7fbff; }}
.info p,.empty,.paper-card p {{ margin:0; color:var(--muted); }}
.paper-summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-bottom:12px; }}
.paper-summary div,.paper-card {{ border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:12px; }}
.paper-summary b,.paper-card b {{ display:block; font-size:21px; margin-top:4px; color:#f7fbff; }}
.research-paper-strip {{ display:grid; grid-template-columns:1.2fr 1fr 1fr 1fr; gap:10px; margin:0 0 12px; }}
.research-paper-strip div {{ border:1px solid rgba(93,140,255,.34); border-radius:8px; background:linear-gradient(180deg,#101a29,#0a121f); padding:11px 12px; }}
.research-paper-strip span {{ display:block; color:#8ea2bd; font-size:12px; letter-spacing:.02em; }}
.research-paper-strip b {{ display:block; color:#f7fbff; font-size:16px; margin-top:3px; }}
.research-paper-strip small {{ display:block; color:#9fb1c8; margin-top:4px; line-height:1.42; }}
.paper-cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; margin-bottom:12px; }}
.paper-card {{ width:100%; text-align:left; color:var(--ink); cursor:pointer; font:inherit; }}
.paper-card.active {{ border-color:var(--cyan); box-shadow:0 0 0 1px rgba(43,212,214,.18), var(--shadow); }}
.paper-panel {{ display:none; }}
.paper-panel.active {{ display:block; }}
.paper-table {{ min-width:1260px; }}
.position-row {{ cursor:pointer; }}
.position-row:hover td {{ background:#132033; }}
.position-detail-row {{ display:none; }}
.position-detail-row.open {{ display:table-row; }}
.position-detail-row td {{ background:#09111d; }}
.position-detail-grid {{ display:grid; grid-template-columns:minmax(0,1fr) 260px; gap:14px; align-items:start; }}
.chart-wrap {{ overflow-x:auto; }}
.kline-svg {{ width:100%; min-width:760px; height:auto; display:block; border:1px solid var(--line); border-radius:8px; background:#08111d; }}
.chart-empty {{ min-height:220px; display:grid; place-content:center; gap:6px; border:1px dashed var(--line); border-radius:8px; color:var(--muted); text-align:center; background:#08111d; }}
.chart-empty b {{ color:#f7fbff; }}
.position-facts {{ border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:12px; }}
.position-facts b {{ display:block; margin-bottom:8px; }}
.position-facts p {{ margin:0 0 8px; color:var(--muted); }}
.mini-btn {{ border:1px solid var(--line); background:#101827; color:#bfe8ff; border-radius:8px; padding:6px 9px; cursor:pointer; }}
.mini-btn.danger {{ color:#ffd0d6; border-color:rgba(255,91,110,.45); background:#25111a; }}
.backtest-history-collapse {{ border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:10px 12px; }}
.backtest-history-collapse summary {{ cursor:pointer; color:#f7fbff; font-weight:800; list-style:none; }}
.backtest-history-collapse summary::-webkit-details-marker {{ display:none; }}
.backtest-history-collapse summary span {{ display:block; color:var(--muted); font-size:12px; font-weight:500; margin-top:2px; }}
.backtest-history-collapse .table-scroll {{ margin-top:10px; }}
.backtest-progress-line {{ color:#bfe8ff; }}
.grid {{ display:grid; grid-template-columns:minmax(0, 1.62fr) minmax(360px, .38fr); gap:16px; align-items:start; }}
.table-scroll {{ width:100%; overflow-x:auto; border:1px solid var(--line); border-radius:8px; background:#0a111c; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ padding:10px 9px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ color:var(--muted); font-size:12px; font-weight:800; background:#0b1422; position:sticky; top:0; z-index:1; }}
td {{ color:#dbe7f6; }} td small {{ display:block; color:var(--muted); margin-top:4px; }}
tr:hover td {{ background:#101827; }}
.strategy-table {{ min-width:1160px; }}
.strategy-table th:last-child,.strategy-table td.reason {{ width:34%; min-width:370px; }}
.strategy-table td.reason {{ color:#dbe7f6; }}
.strategy-detail {{ margin-top:10px; border:1px solid var(--line); border-radius:8px; background:#0b1320; }}
.strategy-detail summary {{ cursor:pointer; padding:8px 10px; font-weight:850; color:#9bdcff; }}
.detail-note {{ margin:0; padding:0 10px 8px; color:var(--muted); }}
.position-table {{ min-width:1320px; font-size:13px; }}
.position-table th,.position-table td {{ padding:8px 7px; }}
.up {{ color:var(--up); font-weight:850; }}
.down {{ color:var(--down); font-weight:850; }}
.muted {{ color:var(--muted); }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:8px; background:var(--muted); box-shadow:0 0 12px currentColor; }}
.dot.good {{ background:var(--good); color:var(--good); }} .dot.warn {{ background:var(--warn); color:var(--warn); }} .dot.bad {{ background:var(--bad); color:var(--bad); }}
.alerts {{ list-style:none; padding:0; margin:0; display:grid; gap:8px; }}
.alerts li {{ border-left:4px solid var(--line); padding:10px; background:#0b1320; border-radius:8px; }}
.alerts li.good {{ border-color:var(--good); }} .alerts li.warn {{ border-color:var(--warn); }} .alerts li.bad {{ border-color:var(--bad); }}
.alerts b,.alerts span {{ display:block; }} .alerts span {{ color:var(--muted); margin-top:2px; }}
.icon-btn {{ border:0; background:linear-gradient(135deg,var(--cyan),var(--blue)); color:#06101a; border-radius:8px; padding:8px 12px; cursor:pointer; font-weight:850; }}
.icon-btn:disabled {{ opacity:.65; cursor:default; }}
.decision-box {{ display:grid; gap:10px; min-width:380px; }}
.decision-facts {{ list-style:none; padding:0; margin:0; display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }}
.decision-facts li {{ border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:8px; }}
.decision-facts span {{ display:block; color:var(--muted); font-size:12px; }}
.decision-facts b {{ display:block; color:#f7fbff; margin-top:2px; }}
.decision-actions {{ display:flex; flex-wrap:wrap; gap:8px; }}
.decision-btn {{ border:1px solid var(--line); color:#06101a; border-radius:8px; padding:8px 10px; cursor:pointer; font-weight:850; }}
.decision-btn.good {{ background:var(--good); }}
.decision-btn.warn {{ background:var(--warn); }}
.decision-btn.bad {{ background:var(--bad); color:#fff; }}
.decision-btn:disabled {{ opacity:.65; cursor:default; }}
.decision-note {{ margin:0; color:var(--muted); font-size:12px; }}
.mover-summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-bottom:12px; }}
.mover-summary div {{ border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:12px; }}
.mover-summary span {{ display:block; color:var(--muted); font-size:12px; }}
.mover-summary b {{ display:block; font-size:20px; margin-top:4px; }}
.mover-table {{ min-width:1080px; }}
.history-progress {{ display:grid; gap:12px; }}
.history-bar {{ height:10px; border-radius:999px; background:#07111c; border:1px solid var(--line); overflow:hidden; }}
.history-bar span {{ display:block; height:100%; background:linear-gradient(90deg,var(--cyan),var(--blue)); }}
.history-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
.history-grid div,.history-detail {{ border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:12px; }}
.history-grid span,.history-grid small,.history-detail span {{ display:block; color:var(--muted); }}
.history-grid b,.history-detail b {{ display:block; color:#f7fbff; margin:3px 0; }}
.backtest-form {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:12px; }}
.backtest-form label {{ display:grid; gap:5px; color:var(--muted); font-size:12px; }}
.backtest-form input,.backtest-form select,.backtest-form textarea {{ width:100%; border:1px solid var(--line); border-radius:8px; background:#08111d; color:var(--ink); padding:8px 9px; font:inherit; }}
.backtest-form textarea {{ resize:vertical; min-height:78px; }}
.backtest-form .wide {{ grid-column:span 4; }}
.backtest-actions {{ grid-column:span 4; display:flex; flex-wrap:wrap; align-items:center; gap:10px; }}
.backtest-results h3 {{ margin:14px 0 8px; font-size:15px; color:#f7fbff; }}
.backtest-table {{ min-width:900px; }}
.backtest-note {{ margin:8px 0 0; color:var(--muted); font-size:12px; }}
.backtest-param-panel {{ border:1px solid var(--line); border-radius:8px; background:#08111d; padding:10px; display:grid; gap:10px; }}
.param-head b,.param-head small {{ display:block; }}
.param-head small,.param-control small,.backtest-advanced small {{ color:var(--muted); }}
.param-controls {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
.param-control {{ border:1px solid var(--line); border-radius:8px; padding:9px; background:#0b1320; }}
.param-control span {{ display:flex; align-items:center; gap:6px; color:#e8f0fb; font-weight:800; }}
.param-control input[type="checkbox"] {{ width:auto; }}
.backtest-advice-grid,.backtest-metrics,.backtest-detail-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
.backtest-detail-grid {{ grid-template-columns:2fr 1fr; }}
.backtest-advice-grid div,.backtest-metrics div,.backtest-detail-grid > div {{ border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:12px; }}
.backtest-advice-grid span,.backtest-advice-grid small,.backtest-metrics span,.backtest-metrics small {{ display:block; color:var(--muted); }}
.backtest-advice-grid b,.backtest-metrics b {{ display:block; color:#f7fbff; margin:3px 0; }}
.backtest-chart svg {{ width:100%; height:160px; display:block; }}
.backtest-chart-caption {{ display:flex; justify-content:space-between; color:var(--muted); font-size:12px; margin-top:6px; }}
.backtest-empty-chart {{ border:1px dashed var(--line); border-radius:8px; padding:48px 12px; color:var(--muted); text-align:center; }}
.backtest-trades-scroll {{ max-height:420px; overflow:auto; }}
.backtest-trades-table {{ min-width:1180px; }}
.backtest-monthly-table {{ min-width:360px; }}
.gate-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
.gate-grid div {{ border:1px solid var(--line); border-radius:8px; background:#0b1320; padding:12px; min-height:76px; }}
.gate-grid b {{ display:block; margin:2px 0 4px; }}
.links {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }}
.links a {{ color:#bfe8ff; background:#0b1320; border:1px solid var(--line); padding:7px 10px; border-radius:8px; text-decoration:none; }}
.links a:hover {{ border-color:var(--cyan); color:#fff; }}
.top-actions {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-top:10px; }}
.refresh-status {{ color:var(--muted); font-size:12px; }}
.refresh-countdown {{ display:inline-flex; align-items:center; gap:6px; border:1px solid var(--line); border-radius:8px; padding:7px 10px; color:#dbe7f6; background:#0b1320; font-weight:800; }}
@media (max-width: 1320px) {{ .metrics,.cards {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }} .app-shell {{ grid-template-columns:1fr; }} .side-rail {{ position:relative; height:auto; display:none; }} }}
@media (max-width: 980px) {{ .metrics,.cards,.grid,.paper-summary,.paper-cards,.gate-grid,.history-grid,.position-detail-grid,.backtest-form,.param-controls,.backtest-advice-grid,.backtest-metrics,.backtest-detail-grid {{ grid-template-columns:1fr; }} .backtest-form .wide,.backtest-actions {{ grid-column:span 1; }} header {{ grid-template-columns:1fr; }} .wrap {{ padding:18px; }} }}
</style>
</head>
<body>
<div class="app-shell">
<aside class="side-rail">
  <div class="brand"><div class="brand-mark"></div><div><b>AutoTrading</b><span>Operations</span></div></div>
  <nav class="nav">
    <a class="active" href="#overview">总览</a>
    <a href="#paper">模拟账本</a>
    <a href="#movers">涨跌榜</a>
    <a href="#strategies">三策略</a>
    <a href="#actions">确认事项</a>
  </nav>
  <div class="rail-note">只读报表。策略运行、持仓、盈亏、图表都来自当前模拟账本和外部行情。</div>
</aside>
<main class="wrap" id="overview">
  <header>
    <div>
      <h1>AutoTrading 决策入口</h1>
      <div class="sub">更新 {h(generated)}。线上首页 60 秒自动刷新；这里只读现有数据。</div>
      <div class="top-actions">
        <button class="icon-btn refresh-btn" onclick="refreshReport(this)" title="同步服务器镜像并重新生成报表">刷新报表</button>
        <span class="refresh-countdown">下次自动刷新 <b id="refreshCountdown">01:00</b></span>
        <span id="refreshStatus" class="refresh-status">安全刷新：只更新报表和镜像，不下单。</span>
      </div>
    </div>
    <div class="status {plain_level(state['overall'])}">{h(status_text(state))}</div>
  </header>
  <section class="metrics">{render_badges(state)}</section>
  <section class="panel">
    <h2>今日重点</h2>
    <div class="cards">{render_cards(state)}</div>
  </section>
{render_history_or_backtest_panel(state)}
  <section class="panel" id="paper">
    <h2>三策略模拟账本运行总览</h2>
    {render_paper_exchange(state)}
  </section>
  <section class="panel" id="movers">
    <h2>今日涨跌榜跟踪</h2>
    {render_market_movers(state)}
  </section>
  <section class="panel">
    <h2>复盘 / 进化成熟度</h2>
    <div class="cards">{render_evolution_readiness(state)}</div>
  </section>
  <section class="grid">
    <div>
      <section class="panel" id="strategies">
        <h2>三策略现在是否正常</h2>
        {render_strategy_table(strategies)}
      </section>
      <section class="panel" id="actions">
        <h2>你需要确认的事项</h2>
        {render_attention(state['attention_items'])}
      </section>
    </div>
    <aside>
      <section class="panel">
        <h2>红黄灯</h2>
        <ul class="alerts">{alert_list}</ul>
      </section>
      <section class="panel">
        <h2>下钻</h2>
        <div class="links">
          <a href="/reports/portal_latest.html">完整旧版详情</a>
          <a href="/reports/replay_readiness_latest.md">Replay 验收</a>
          <a href="/reports/auto_upgrade_readiness_latest.md">自动升级闸门</a>
          <a href="/reports/strategy_candidate_governance_latest.md">候选治理</a>
          <a href="/reports/waiting_period_progress_latest.md">等待期推进</a>
          <a href="/reports/paper_real_calibration_plan_latest.md">纸实校准计划</a>
          <a href="/reports/alpha_discovery_latest.html">Alpha发现/GHI研究</a>
          <a href="/reports/waiting_period_optimization_latest.html">等待期优化</a>
          <a href="/reports/market_review_latest.html">市场复盘日报</a>
          <a href="/reports/long_term_skeleton_latest.md">长期目标骨架</a>
          <a href="/reports/strategy_evolution_latest.html">策略进化</a>
          <a href="/reports/research_store_summary_latest.md">研究仓</a>
          <a href="/api/attention">确认事项 API</a>
        </div>
      </section>
    </aside>
  </section>
</main>
</div>
<script>
async function ackItem(itemId, btn) {{
  if (!itemId || !btn) return;
  btn.disabled = true;
  btn.textContent = '确认中';
  try {{
    const resp = await fetch('/api/attention/ack', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{item_id: itemId, user: 'decision_portal'}})
    }});
    const data = await resp.json();
    if (data.ok) {{
      btn.textContent = '已确认';
      const row = btn.closest('tr');
      if (row) row.style.opacity = '0.45';
    }} else {{
      btn.textContent = '失败';
      btn.disabled = false;
      alert(data.error || '确认失败');
    }}
  }} catch (err) {{
    btn.textContent = '网络错误';
    btn.disabled = false;
  }}
}}
async function decideItem(itemId, decision, btn) {{
  if (!itemId || !decision || !btn) return;
  const siblings = btn.closest('.decision-actions')?.querySelectorAll('button') || [];
  siblings.forEach((el) => el.disabled = true);
  const old = btn.textContent;
  btn.textContent = '记录中';
  try {{
    const resp = await fetch('/api/attention/decision', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{item_id: itemId, decision: decision, user: 'decision_portal'}})
    }});
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || '记录失败');
    btn.textContent = data.label || '已记录';
    const row = btn.closest('tr');
    if (row) {{
      row.style.opacity = '0.55';
      const note = row.querySelector('.decision-note');
      if (note) note.textContent = data.effect || '已写入决策台账。';
    }}
  }} catch (err) {{
    btn.textContent = old;
    siblings.forEach((el) => el.disabled = false);
    alert((err && err.message) ? err.message : '记录失败');
  }}
}}
async function refreshReport(btn) {{
  const status = document.getElementById('refreshStatus');
  if (btn) {{
    btn.disabled = true;
    btn.textContent = '刷新中';
  }}
  if (status) status.textContent = '正在刷新报表。不会下单。';
  try {{
    const resp = await fetch('/api/report/refresh', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{user: 'decision_portal'}})
    }});
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || '刷新失败');
    if (status) status.textContent = data.action === 'already_running'
      ? '已有刷新任务在跑，等它完成后页面会自动变新。'
      : '刷新任务已启动，稍等 30-90 秒后自动重载。';
    setTimeout(() => window.location.reload(), 45000);
  }} catch (err) {{
    if (status) status.textContent = '刷新启动失败：' + (err.message || err);
    if (btn) {{
      btn.disabled = false;
      btn.textContent = '刷新报表';
    }}
  }}
}}
function getBacktestParameterSchema() {{
  const el = document.getElementById('backtestParameterSchema');
  if (!el) return {{strategies: {{}}}};
  try {{
    return JSON.parse(el.textContent || '{{}}');
  }} catch (err) {{
    return {{strategies: {{}}}};
  }}
}}
function renderBacktestParamControls() {{
  const form = document.querySelector('.backtest-form');
  const box = document.getElementById('backtestParamControls');
  if (!form || !box) return;
  const schema = getBacktestParameterSchema();
  const strategy = String(form.querySelector('[name="strategy"]')?.value || 'A/v11');
  const rows = (schema.strategies && schema.strategies[strategy]) || [];
  box.innerHTML = rows.map((row) => {{
    const key = String(row.key || '');
    return `<label class="param-control" data-param-key="${{key}}">
      <span><input type="checkbox" name="param_enabled" value="${{key}}"> ${{row.label || key}}</span>
      <input name="param_value_${{key}}" value="" placeholder="${{row.min}} - ${{row.max}}" inputmode="decimal" data-min="${{row.min}}" data-max="${{row.max}}" data-step="${{row.step}}">
      <small>${{key}}；范围 ${{row.min}} - ${{row.max}}。${{row.note || ''}}</small>
    </label>`;
  }}).join('');
  syncBacktestParamsFromControls();
}}
function syncBacktestParamsFromControls() {{
  const form = document.querySelector('.backtest-form');
  if (!form) return {{}};
  const schema = getBacktestParameterSchema();
  const maxParams = Number(schema.max_tuned_parameters || 3);
  const params = {{}};
  const enabled = Array.from(form.querySelectorAll('input[name="param_enabled"]:checked'));
  const status = document.getElementById('backtestStatus');
  if (enabled.length > maxParams) {{
    if (status) status.textContent = '最多只能勾选 ' + maxParams + ' 个参数，避免过拟合。';
    throw new Error('too_many_tuned_parameters');
  }}
  for (const checkbox of enabled) {{
    const key = checkbox.value;
    const input = form.querySelector(`[name="param_value_${{key}}"]`);
    const raw = String(input?.value || '').trim();
    if (!raw) continue;
    const value = Number(raw);
    const min = Number(input.dataset.min);
    const max = Number(input.dataset.max);
    if (!Number.isFinite(value)) throw new Error('参数不是数字：' + key);
    if (Number.isFinite(min) && value < min) throw new Error('参数低于范围：' + key);
    if (Number.isFinite(max) && value > max) throw new Error('参数高于范围：' + key);
    params[key] = value;
  }}
  const textarea = form.querySelector('[name="params"]');
  if (textarea && (enabled.length > 0 || !String(textarea.value || '').trim())) {{
    textarea.value = JSON.stringify(params, null, 2);
  }}
  return params;
}}
function initBacktestParamControls() {{
  const form = document.querySelector('.backtest-form');
  if (!form) return;
  form.querySelector('[name="strategy"]')?.addEventListener('change', renderBacktestParamControls);
  form.addEventListener('input', (evt) => {{
    if (evt.target.closest('.param-control')) {{
      try {{ syncBacktestParamsFromControls(); }} catch (err) {{}}
    }}
  }});
  form.addEventListener('change', (evt) => {{
    if (evt.target.closest('.param-control')) {{
      try {{ syncBacktestParamsFromControls(); }} catch (err) {{}}
    }}
  }});
  renderBacktestParamControls();
}}
function estimateBacktestRuntime(payload) {{
  const symbols = String(payload.symbols || '').split(',').map((x) => x.trim()).filter(Boolean).length || 1;
  const days = Number(payload.period_days || 365);
  const interval = String(payload.interval || '1h');
  const variants = Math.max(1, Number(payload.parameter_variants || 1));
  const intervalFactor = interval === '15m' ? 4 : interval === '30m' ? 2 : interval === '4h' ? 0.35 : 1;
  const load = symbols * Math.max(1, days / 90) * intervalFactor * variants;
  if (load <= 2) return '预计几十秒内';
  if (load <= 12) return '预计 1-3 分钟';
  return '预计 3-8 分钟；币种多、15m、参数变体多会更慢';
}}
async function submitBacktestJob(evt) {{
  evt.preventDefault();
  const form = evt.target;
  const status = document.getElementById('backtestStatus');
  const btn = form.querySelector('button[type="submit"]');
  if (btn) {{
    btn.disabled = true;
    btn.textContent = '创建中';
  }}
  const fd = new FormData(form);
  let params = {{}};
  try {{
    params = syncBacktestParamsFromControls();
  }} catch (err) {{
    if (status) status.textContent = '参数超出范围或数量过多，任务未提交。';
    if (btn) {{
      btn.disabled = false;
      btn.textContent = '创建回测任务';
    }}
    return;
  }}
  const rawParams = String(form.querySelector('[name="params"]')?.value || '').trim();
  if (rawParams && Object.keys(params).length === 0) {{
    try {{
      params = JSON.parse(rawParams);
    }} catch (err) {{
      if (status) status.textContent = '参数 JSON 无效，任务未提交。';
      if (btn) {{
        btn.disabled = false;
        btn.textContent = '创建回测任务';
      }}
      return;
    }}
  }}
  const payload = {{
    user: 'decision_portal',
    strategy: fd.get('strategy'),
    symbols: fd.get('symbols'),
    interval: fd.get('interval'),
    period_days: fd.get('period_days'),
    direction: fd.get('direction'),
    capital_usdt: fd.get('capital_usdt'),
    fee_bps: fd.get('fee_bps'),
    slippage_bps: fd.get('slippage_bps'),
    parameter_variants: fd.get('parameter_variants'),
    params: params
  }};
  const startedAt = Date.now();
  const estimate = estimateBacktestRuntime(payload);
  let progressTimer = null;
  if (status) {{
    status.classList.add('backtest-progress-line');
    status.textContent = '创建中：同步执行 research adapter；' + estimate + '；已耗时 0 秒。当前没有分阶段百分比，不显示假进度。';
    progressTimer = setInterval(() => {{
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);
      status.textContent = '创建中：同步执行 research adapter；' + estimate + '；已耗时 ' + elapsed + ' 秒。只读历史仓，不下单。';
    }}, 1000);
  }}
  try {{
    const resp = await fetch('/api/backtest/jobs', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await resp.json();
    if (!data.ok) throw new Error((data.errors || [data.error || '创建失败']).join('；'));
    if (progressTimer) clearInterval(progressTimer);
    const summary = (data.result && data.result.summary) || {{}};
    const elapsed = Math.floor((Date.now() - startedAt) / 1000);
    if (status) status.textContent = '回测完成：' + data.job_id + '；耗时 ' + elapsed + ' 秒；状态 ' + data.status + '；净收益 ' + (summary.net_profit_usdt ?? '-') + '；交易 ' + (summary.trades ?? 0) + '。结果只做研究建议，不自动改策略。';
    setTimeout(() => window.location.reload(), 5000);
  }} catch (err) {{
    if (progressTimer) clearInterval(progressTimer);
    if (status) status.textContent = '创建失败：' + (err.message || err);
    if (btn) {{
      btn.disabled = false;
      btn.textContent = '创建回测任务';
    }}
  }}
}}
async function deleteBacktestJob(jobId) {{
  if (!jobId || jobId === '-') return;
  if (!confirm('删除这条回测任务记录？只删除任务 JSON，不删除历史K线仓。')) return;
  const status = document.getElementById('backtestStatus');
  if (status) status.textContent = '正在删除回测任务记录：' + jobId;
  try {{
    const resp = await fetch('/api/backtest/job?id=' + encodeURIComponent(jobId), {{method: 'DELETE'}});
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || '删除失败');
    if (status) status.textContent = '已删除：' + jobId + '；剩余任务 ' + (data.remaining_jobs ?? '-') + '。历史K线仓未删除。';
    setTimeout(() => window.location.reload(), 800);
  }} catch (err) {{
    if (status) status.textContent = '删除失败：' + (err.message || err);
  }}
}}
function showPaperStrategy(name) {{
  document.querySelectorAll('.strategy-tab').forEach((el) => {{
    el.classList.toggle('active', el.dataset.strategy === name);
  }});
  document.querySelectorAll('.paper-panel').forEach((el) => {{
    el.classList.toggle('active', el.dataset.strategy === name);
  }});
}}
function togglePositionDetail(id) {{
  const row = document.getElementById(id);
  if (!row) return;
  row.classList.toggle('open');
}}
function startRefreshCountdown() {{
  const el = document.getElementById('refreshCountdown');
  if (!el) return;
  let remain = {REPORT_REFRESH_SECONDS};
  const tick = () => {{
    const minutes = Math.floor(remain / 60);
    const seconds = remain % 60;
    el.textContent = String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
    remain = Math.max(0, remain - 1);
  }};
  tick();
  setInterval(tick, 1000);
}}
initBacktestParamControls();
startRefreshCountdown();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate concise decision portal")
    parser.add_argument("--out-dir", default=str(REPORTS_DIR))
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_text = render_html()
    (out_dir / "index.html").write_text(html_text, encoding="utf-8")
    (out_dir / "decision_portal_latest.html").write_text(html_text, encoding="utf-8")
    print(json.dumps({"status": "ok", "index": str(out_dir / "index.html")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
