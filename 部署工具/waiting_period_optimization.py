"""Build a no-Binance-pressure optimization report for waiting windows.

Reads only local/mirrored runtime files. It never submits queue work, never
requests Binance, and never restarts services.
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from contextlib import closing
from collections import Counter, defaultdict
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
ROOT = SCRIPT_DIR.parent
CST = timezone(timedelta(hours=8))
SAFETY = "read_only_no_binance_request_no_queue_submit_no_service_restart"
STRATEGIES = ("A/v11", "B/v16", "C/v14")
REASON_LABELS = {
    "duplicate_position": "已有同币种/同方向仓位，避免重复开仓",
    "account_state_unavailable": "账户状态不够新，宁可不开仓",
    "central_account_state_unavailable": "中心账户状态不够新，宁可不开仓",
    "scanner_order_disabled": "观察模式关闭下单，只收扫描证据",
    "market_data_unavailable": "行情/K线缓存不足，策略不硬开",
    "kline_unavailable": "K线缓存不足，策略不硬开",
    "execution_preflight": "交易所规则预检不过，提前拦住",
    "exchange_error": "交易所/API 返回失败，需要继续观察",
    "score_low": "分数不够，策略认为优势不明显",
    "threshold_fail": "门槛未过，信号强度不足",
    "confirm_fail": "确认层未通过，二次确认不支持开仓",
    "active_limit": "策略持仓上限已满",
    "cooldown": "策略或币种冷却中",
    "loss_blacklist": "亏损黑名单中，暂不交易",
    "symbol_sl_cooldown": "该币止损后冷却中",
    "no_signal": "扫描过但没有形成可交易信号",
    "has_position": "已有持仓，跳过重复机会",
    "score_gt_max": "分数异常或超过保护上限，跳过",
    "small_live_stage_guard": "小范围观察保护，暂不放大交易",
    "stage_guard_fail": "阶段保护未通过",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def age_text(dt: datetime | None, now: datetime | None = None) -> str:
    if not dt:
        return "无记录"
    now = now or datetime.now(CST)
    seconds = max(0, int((now - dt.astimezone(CST)).total_seconds()))
    if seconds < 90:
        return f"{seconds}s前"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m前"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h前"
    return f"{hours // 24}d前"


def plain_reason(reason: Any, stage: Any = "", layer: Any = "") -> str:
    raw = str(reason or layer or stage or "unknown")
    key = raw.strip().lower()
    if key in REASON_LABELS:
        return REASON_LABELS[key]
    for token, label in REASON_LABELS.items():
        if token in key:
            return label
    if not raw or raw == "unknown":
        return "没有记录具体原因，需要补遥测"
    return raw


def parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def extract_cache_symbols(cache: dict[str, Any]) -> list[str]:
    for key in ("symbols", "top_symbols", "available_symbols"):
        values = cache.get(key)
        if isinstance(values, list):
            out = []
            for item in values:
                if isinstance(item, str):
                    out.append(item.upper())
                elif isinstance(item, dict) and item.get("symbol"):
                    out.append(str(item["symbol"]).upper())
            if out:
                return out
    tickers = cache.get("tickers")
    if isinstance(tickers, list):
        return [str(row.get("symbol", "")).upper() for row in tickers if isinstance(row, dict) and row.get("symbol")]
    return []


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
    return bool(row)


def first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def latest_event_dt(conn: sqlite3.Connection) -> datetime | None:
    latest: datetime | None = None
    for table in ("events", "sentinel_scans"):
        if not table_exists(conn, table):
            continue
        try:
            row = conn.execute(f"select max(ts) from {table}").fetchone()
        except Exception:
            continue
        dt = parse_dt(row[0] if row else None)
        if dt and (latest is None or dt > latest):
            latest = dt
    return latest


def queue_review(runtime_dir: Path, mirror_runtime: Path) -> dict[str, Any]:
    summary = read_json(first_existing(runtime_dir / "binance_api_queue_summary_latest.json", mirror_runtime / "binance_api_queue_summary_latest.json") or Path(""))
    db = first_existing(runtime_dir / "binance_api_queue.sqlite3", mirror_runtime / "binance_api_queue.sqlite3")
    out: dict[str, Any] = {
        "available": False,
        "source": str(db) if db else "summary_json",
        "active_requests": int(summary.get("active_requests") or summary.get("active") or 0),
        "active_cooldowns": int(summary.get("active_cooldowns") or summary.get("cooldowns") or 0),
        "recent_bad": int(summary.get("recent_bad") or 0),
        "latest_rows": [],
    }
    if not db:
        out["available"] = bool(summary)
        return out
    try:
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            if not table_exists(conn, "api_requests"):
                return out
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            out["available"] = True
            out["active_requests"] = int(conn.execute(
                "select count(*) from api_requests where status in ('queued','deferred','leased')"
            ).fetchone()[0])
            if table_exists(conn, "cooldowns"):
                out["active_cooldowns"] = int(conn.execute(
                    "select count(*) from cooldowns where until_ms > ?", (now_ms,)
                ).fetchone()[0])
            out["recent_bad"] = int(conn.execute(
                """
                select count(*) from api_requests
                where rowid > (select coalesce(max(rowid),0) - 80 from api_requests)
                  and (status='failed' or result_status in (418,429) or error like '%-1003%')
                """
            ).fetchone()[0])
            latest_rows = conn.execute(
                "select rowid,label,scope,account,path,status,result_status,error from api_requests order by rowid desc limit 8"
            ).fetchall()
            out["latest_rows"] = [dict(row) for row in latest_rows]
    except Exception as exc:
        out["error"] = str(exc)
    return out


def open_skipped_review(db_path: Path | None, hours: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "available": False,
        "source": str(db_path) if db_path else "",
        "hours": hours,
        "total": 0,
        "by_strategy": {},
        "top_reasons": [],
        "plain_reasons": [],
        "top_strategy_reasons": [],
        "scan_stats_reasons": [],
        "recent_open_failed": 0,
        "recent_opened": 0,
        "latest_activity": "",
    }
    if not db_path or not db_path.exists():
        return out
    since_dt = datetime.now(CST) - timedelta(hours=hours)
    since = since_dt.isoformat()
    since_space = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if not table_exists(conn, "events"):
                return out
            db_latest = latest_event_dt(conn)
            out["db_latest_age"] = age_text(db_latest)
            out["available"] = True
            if not db_latest or db_latest < since_dt:
                out["stale_mirror"] = True
                out["fresh_enough"] = False
                out["note"] = "本地/镜像事件库过期，不能用它判断最近不开仓原因。"
                return out
            out["stale_mirror"] = False
            out["fresh_enough"] = True
            rows = conn.execute(
                """
                select ts, strategy, reason, stage, layer, payload_json
                from events
                where event_type='OPEN_SKIPPED' and (ts >= ? or ts >= ?)
                """,
                (since, since_space),
            ).fetchall()
            out["total"] = len(rows)
            by_strategy: dict[str, int] = defaultdict(int)
            reasons: Counter[str] = Counter()
            plain_reasons: Counter[str] = Counter()
            strategy_reasons: Counter[tuple[str, str]] = Counter()
            latest_dt: datetime | None = None
            for row in rows:
                strategy = str(row["strategy"] or "unknown")
                reason = str(row["reason"] or row["layer"] or row["stage"] or "unknown")
                label = plain_reason(reason, row["stage"], row["layer"])
                by_strategy[strategy] += 1
                reasons[reason] += 1
                plain_reasons[label] += 1
                strategy_reasons[(strategy, reason)] += 1
                dt = parse_dt(row["ts"])
                if dt and (latest_dt is None or dt > latest_dt):
                    latest_dt = dt
            out["by_strategy"] = dict(sorted(by_strategy.items()))
            out["top_reasons"] = [{"reason": reason, "count": count} for reason, count in reasons.most_common(12)]
            out["plain_reasons"] = [{"reason": reason, "count": count} for reason, count in plain_reasons.most_common(12)]
            out["top_strategy_reasons"] = [
                {"strategy": strategy, "reason": reason, "plain_reason": plain_reason(reason), "count": count}
                for (strategy, reason), count in strategy_reasons.most_common(12)
            ]
            out["recent_open_failed"] = int(conn.execute(
                "select count(*) from events where event_type='OPEN_FAILED' and (ts >= ? or ts >= ?)", (since, since_space)
            ).fetchone()[0])
            out["recent_opened"] = int(conn.execute(
                "select count(*) from events where event_type='OPEN' and (ts >= ? or ts >= ?)", (since, since_space)
            ).fetchone()[0])
            scan_rows = conn.execute(
                """
                select ts, strategy, payload_json
                from events
                where event_type='SCAN_STATS' and (ts >= ? or ts >= ?)
                order by ts desc
                limit 300
                """,
                (since, since_space),
            ).fetchall()
            scan_reasons: Counter[str] = Counter()
            for row in scan_rows:
                payload = parse_payload(row["payload_json"])
                for key, value in payload.items():
                    if key in {"strategy", "event", "ts", "capital", "positions", "status", "trade_size_usdt"}:
                        continue
                    if isinstance(value, bool):
                        continue
                    if isinstance(value, (int, float)) and value > 0:
                        scan_reasons[plain_reason(key)] += int(value)
                dt = parse_dt(row["ts"])
                if dt and (latest_dt is None or dt > latest_dt):
                    latest_dt = dt
            out["scan_stats_reasons"] = [
                {"reason": reason, "count": count} for reason, count in scan_reasons.most_common(12)
            ]
            out["latest_activity"] = age_text(latest_dt)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def top100_review(runtime_dir: Path, mirror_runtime: Path) -> dict[str, Any]:
    cache = read_json(first_existing(runtime_dir / "market_data_cache.json", mirror_runtime / "market_data_cache.json") or Path(""))
    generated_at = parse_dt(cache.get("generated_at") or cache.get("updated_at") or cache.get("ts"))
    symbols = extract_cache_symbols(cache)
    count = len(symbols)
    return {
        "target": "Binance trading-volume Top100",
        "configured": {
            "A/v11": "top 100 + spike 100 + sentinel 40",
            "B/v16": "top 100 + sentinel 40",
            "C/v14": "top 100 + sentinel 40",
        },
        "cache_symbols": count,
        "top100_symbols": symbols[:100],
        "cache_age": age_text(generated_at),
        "coverage_hint": "ok" if count >= 100 else "cache_gap",
        "note": "市值Top100需要外部数据源；当前不新增联网源，先用交易量Top100。",
    }


def scan_coverage_review(db_path: Path | None, top100_symbols: list[str], hours: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "available": False,
        "source": str(db_path) if db_path else "",
        "hours": hours,
        "target_count": len(top100_symbols[:100]),
        "by_strategy": {},
        "overall_top100_scanned": 0,
        "overall_top100_pct": 0.0,
        "latest_scan_age": "无记录",
        "note": "实扫覆盖只从本地/镜像 events 与 sentinel_scans 计算；不代表修改扫描频率。",
    }
    if not db_path or not db_path.exists():
        return out
    top100 = {symbol.upper() for symbol in top100_symbols[:100]}
    since_dt = datetime.now(CST) - timedelta(hours=hours)
    since = since_dt.isoformat()
    since_space = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            seen_by_strategy: dict[str, set[str]] = {strategy: set() for strategy in STRATEGIES}
            latest_dt: datetime | None = None
            if table_exists(conn, "events"):
                db_latest = latest_event_dt(conn)
                out["db_latest_age"] = age_text(db_latest)
                if not db_latest or db_latest < since_dt:
                    out["available"] = True
                    out["stale_mirror"] = True
                    out["coverage_status"] = "stale_mirror_unknown"
                    out["note"] = "本地/镜像事件库过期，实扫覆盖不能按 0% 解读。"
                    return out
                out["stale_mirror"] = False
                out["coverage_status"] = "measured"
                rows = conn.execute(
                    """
                    select ts, strategy, symbol, payload_json
                    from events
                    where (ts >= ? or ts >= ?)
                      and symbol is not null
                      and symbol != ''
                      and event_type in ('SIGNAL','OPEN_SKIPPED','OPEN_FAILED','OPEN','SCAN_STATS')
                    """,
                    (since, since_space),
                ).fetchall()
                for row in rows:
                    strategy = str(row["strategy"] or "")
                    symbol = str(row["symbol"] or "").upper()
                    if strategy in seen_by_strategy and symbol:
                        seen_by_strategy[strategy].add(symbol)
                    payload = parse_payload(row["payload_json"])
                    payload_symbol = str(payload.get("symbol") or "").upper()
                    if strategy in seen_by_strategy and payload_symbol:
                        seen_by_strategy[strategy].add(payload_symbol)
                    dt = parse_dt(row["ts"])
                    if dt and (latest_dt is None or dt > latest_dt):
                        latest_dt = dt
            if table_exists(conn, "sentinel_scans"):
                rows = conn.execute(
                    """
                    select ts, strategy, symbol
                    from sentinel_scans
                    where (ts >= ? or ts >= ?) and symbol is not null and symbol != ''
                    """,
                    (since, since_space),
                ).fetchall()
                for row in rows:
                    strategy = str(row["strategy"] or "")
                    symbol = str(row["symbol"] or "").upper()
                    if strategy in seen_by_strategy and symbol:
                        seen_by_strategy[strategy].add(symbol)
                    dt = parse_dt(row["ts"])
                    if dt and (latest_dt is None or dt > latest_dt):
                        latest_dt = dt
            out["available"] = True
            overall_top100_seen: set[str] = set()
            by_strategy = {}
            for strategy in STRATEGIES:
                symbols = seen_by_strategy.get(strategy, set())
                top_seen = symbols & top100 if top100 else set()
                overall_top100_seen |= top_seen
                pct = (len(top_seen) / len(top100) * 100.0) if top100 else 0.0
                by_strategy[strategy] = {
                    "scanned_symbols": len(symbols),
                    "top100_scanned": len(top_seen),
                    "top100_pct": round(pct, 1),
                    "sample_symbols": sorted(list(top_seen))[:12],
                    "gap_count": max(0, len(top100) - len(top_seen)),
                }
            out["by_strategy"] = by_strategy
            out["overall_top100_scanned"] = len(overall_top100_seen)
            out["overall_top100_pct"] = round((len(overall_top100_seen) / len(top100) * 100.0) if top100 else 0.0, 1)
            out["latest_scan_age"] = age_text(latest_dt)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def research_gap_review(runtime_dir: Path, mirror_runtime: Path) -> dict[str, Any]:
    research = read_json(first_existing(runtime_dir / "research_store_summary_latest.json", mirror_runtime / "research_store_summary_latest.json") or Path(""))
    kline = read_json(first_existing(runtime_dir / "research_kline_backfill_latest.json", mirror_runtime / "research_kline_backfill_latest.json") or Path(""))
    depth = read_json(first_existing(runtime_dir / "research_depth_backfill_latest.json", mirror_runtime / "research_depth_backfill_latest.json") or Path(""))
    kline_acceptance = research.get("kline_acceptance") if isinstance(research.get("kline_acceptance"), dict) else {}
    kline_plan = (kline.get("plan") or {}).get("summary") if isinstance(kline.get("plan"), dict) else {}
    depth_plan = (depth.get("plan") or {}).get("summary") if isinstance(depth.get("plan"), dict) else {}
    return {
        "kline_status": kline_acceptance.get("status") or "missing",
        "kline_target_met": bool(kline_acceptance.get("target_met")),
        "kline_missing_intervals": kline_acceptance.get("missing_intervals") or [],
        "kline_gap_intervals": kline_acceptance.get("gap_intervals") or [],
        "planned_kline_requests": int((kline_plan or {}).get("requests") or 0),
        "planned_depth_requests": int((depth_plan or {}).get("requests") or 0),
        "plan_only": True,
        "note": "这里只看缺口和计划，不执行 --submit，不增加 Binance 请求。",
    }


def report_review(runtime_dir: Path, reports_dir: Path) -> dict[str, Any]:
    files = [
        runtime_dir / "live_context_summary_latest.json",
        runtime_dir / "alerts_latest.json",
        runtime_dir / "strategy_evolution_latest.json",
        runtime_dir / "replay_readiness_latest.json",
        reports_dir / "index.html",
        reports_dir / "portal_latest.html",
    ]
    rows = []
    for path in files:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, CST) if path.exists() else None
        rows.append({"file": str(path), "exists": path.exists(), "age": age_text(mtime)})
    return {"files": rows, "missing": sum(1 for row in rows if not row["exists"])}


def live_activity_review(runtime_dir: Path) -> dict[str, Any]:
    live = read_json(runtime_dir / "live_context_summary_latest.json")
    summary = live.get("live_summary") if isinstance(live.get("live_summary"), dict) else {}
    strategies = summary.get("strategies") if isinstance(summary.get("strategies"), dict) else {}
    services = summary.get("services") if isinstance(summary.get("services"), dict) else {}
    rows: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        item = strategies.get(strategy) if isinstance(strategies.get(strategy), dict) else {}
        activity = item.get("latest_activity") if isinstance(item.get("latest_activity"), dict) else {}
        ts = parse_dt(activity.get("ts"))
        rows[strategy] = {
            "latest_activity": activity,
            "activity_age": age_text(ts),
            "service": {
                "A/v11": services.get("crypto-scanner.service"),
                "B/v16": services.get("crypto-scanner-v16.service"),
                "C/v14": services.get("crypto-scanner-v14.service"),
            }.get(strategy, "unknown"),
        }
    return {
        "pulled_at": live.get("pulled_at"),
        "services": services,
        "strategies": rows,
        "attention": summary.get("attention_summary") or {},
        "alerts": summary.get("alert_summary") or {},
    }


def build_payload(root: Path = ROOT, hours: int = 24) -> dict[str, Any]:
    runtime_dir = root / "runtime"
    reports_dir = root / "reports"
    mirror_runtime = root / "server_logs_tencent" / "runtime"
    event_db = first_existing(mirror_runtime / "event_store.sqlite3", runtime_dir / "event_store.sqlite3")
    queue = queue_review(runtime_dir, mirror_runtime)
    skipped = open_skipped_review(event_db, hours)
    gaps = research_gap_review(runtime_dir, mirror_runtime)
    top100 = top100_review(runtime_dir, mirror_runtime)
    scan_coverage = scan_coverage_review(event_db, top100.get("top100_symbols") or [], hours)
    live_activity = live_activity_review(runtime_dir)
    status = "blocked_by_cooldown" if queue.get("active_cooldowns") else "safe_to_optimize_offline"
    readiness = {
        "decision": "hold_frequency" if queue.get("active_cooldowns") or queue.get("recent_bad") else "ready_for_plan_only_data_work",
        "can_raise_frequency": False,
        "can_submit_kline_depth": False,
        "can_restart_for_experiment": False,
        "reason": "有 cooldown/坏请求就只读观察" if queue.get("active_cooldowns") or queue.get("recent_bad") else "队列当前干净；只能推进离线报表、计划、回放骨架",
    }
    actions = [
        "保持 A/B/C 扫描频率 120s、cache/sentinel 300s，不上调频率。",
        "等待期只看 OPEN_SKIPPED、OPEN_FAILED、Top100覆盖、K线/深度缺口、report新鲜度。",
        "Kline/depth 只生成 plan，不运行 --submit。",
        "有 418/429/-1003 或 cooldown 时，不重启 scanner/cache/sentinel。",
    ]
    return {
        "generated_at": datetime.now(CST).isoformat(),
        "status": status,
        "safety": SAFETY,
        "hours": hours,
        "queue": queue,
        "top100": top100,
        "scan_coverage": scan_coverage,
        "open_skipped": skipped,
        "research_gaps": gaps,
        "reports": report_review(runtime_dir, reports_dir),
        "live_activity": live_activity,
        "readiness": readiness,
        "actions": actions,
        "summary": {
            "active_requests": int(queue.get("active_requests") or 0),
            "active_cooldowns": int(queue.get("active_cooldowns") or 0),
            "recent_bad": int(queue.get("recent_bad") or 0),
            "open_skipped": int(skipped.get("total") or 0),
            "open_failed": int(skipped.get("recent_open_failed") or 0),
            "opened": int(skipped.get("recent_opened") or 0),
            "top100_scanned": int(scan_coverage.get("overall_top100_scanned") or 0),
            "top100_pct": float(scan_coverage.get("overall_top100_pct") or 0.0),
            "scan_coverage_status": scan_coverage.get("coverage_status") or "measured",
            "planned_kline_requests": int(gaps.get("planned_kline_requests") or 0),
            "planned_depth_requests": int(gaps.get("planned_depth_requests") or 0),
        },
    }


def md_table(rows: list[list[Any]]) -> list[str]:
    if not rows:
        return []
    header = "| " + " | ".join(str(x) for x in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(str(x) for x in row) + " |" for row in rows[1:]]
    return [header, sep, *body]


def render_md(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    top100 = payload.get("top100") or {}
    coverage = payload.get("scan_coverage") or {}
    skipped = payload.get("open_skipped") or {}
    gaps = payload.get("research_gaps") or {}
    readiness = payload.get("readiness") or {}
    live_activity = payload.get("live_activity") or {}
    measured = (coverage.get("coverage_status") or "measured") == "measured"
    no_open_fresh = bool(skipped.get("fresh_enough", True))
    coverage_text = (
        f"`{summary.get('top100_scanned')}/{coverage.get('target_count')}` (`{summary.get('top100_pct')}%`)"
        if measured
        else f"`未知`（{coverage.get('note') or '镜像数据不够新'}）"
    )
    no_open_text = {
        "skipped": f"`{skipped.get('total')}`" if no_open_fresh else "`未知`",
        "failed": f"`{skipped.get('recent_open_failed')}`" if no_open_fresh else "`未知`",
        "opened": f"`{skipped.get('recent_opened')}`" if no_open_fresh else "`未知`",
        "activity": skipped.get("latest_activity") if no_open_fresh else (skipped.get("note") or "镜像数据不够新"),
    }
    lines = [
        "# 等待期离线优化",
        "",
        f"- 状态: `{payload.get('status')}`",
        f"- 安全标记: `{payload.get('safety')}`",
        f"- 窗口: 最近 `{payload.get('hours')}` 小时",
        f"- 下一步判断: `{readiness.get('decision')}` - {readiness.get('reason')}",
        "",
        "## API / Binance 压力",
        "",
        f"- 队列中请求: `{summary.get('active_requests')}`",
        f"- 当前冷却: `{summary.get('active_cooldowns')}`",
        f"- 近期坏请求: `{summary.get('recent_bad')}`",
        f"- 是否能提频: `否`",
        f"- 是否能提交 Kline/depth: `否`",
        "",
        "## Top100 覆盖",
        "",
        f"- 目标: {top100.get('target')}",
        f"- 缓存币种数: `{top100.get('cache_symbols')}` ({top100.get('cache_age')})",
        f"- 近窗口 Top100 实扫: {coverage_text}",
        f"- 最新扫描记录: {coverage.get('latest_scan_age')}",
        f"- 说明: {top100.get('note')}",
        "",
        "### 当前服务心跳",
        "",
    ]
    live_rows = [["Strategy", "Service", "Latest activity"]]
    for strategy, row in (live_activity.get("strategies") or {}).items():
        live_rows.append([strategy, row.get("service"), row.get("activity_age")])
    lines.extend(md_table(live_rows) or ["No live activity rows."])
    lines.extend([
        "",
        "### 各策略实扫覆盖",
        "",
    ])
    coverage_rows = [["Strategy", "Scanned", "Top100", "Pct", "Gap"]]
    for strategy, row in (coverage.get("by_strategy") or {}).items():
        coverage_rows.append([
            strategy,
            row.get("scanned_symbols"),
            row.get("top100_scanned"),
            f"{row.get('top100_pct')}%",
            row.get("gap_count"),
        ])
    lines.extend(md_table(coverage_rows) or ["No scan coverage rows."])
    lines.extend([
        "",
        "## 为什么没开仓",
        "",
        f"- OPEN_SKIPPED: {no_open_text['skipped']}",
        f"- OPEN_FAILED: {no_open_text['failed']}",
        f"- OPEN: {no_open_text['opened']}",
        f"- 最新开仓/跳过/扫描活动: {no_open_text['activity']}",
        "",
    ])
    rows = [["白话原因", "Count"]] + [[r.get("reason"), r.get("count")] for r in skipped.get("plain_reasons", [])]
    lines.extend(md_table(rows) or ["No reason rows."])
    scan_reason_rows = [["扫描层原因", "Count"]] + [[r.get("reason"), r.get("count")] for r in skipped.get("scan_stats_reasons", [])]
    lines.extend(["", "### 扫描层统计", ""])
    lines.extend(md_table(scan_reason_rows) or ["No scan stats rows."])
    lines.extend([
        "",
        "## Kline / depth 缺口计划",
        "",
        f"- Kline 状态: `{gaps.get('kline_status')}`",
        f"- 计划 Kline 请求数: `{gaps.get('planned_kline_requests')}`",
        f"- 计划 depth 请求数: `{gaps.get('planned_depth_requests')}`",
        "- 提交执行: `disabled`",
        "",
        "## 等待期动作",
        "",
    ])
    lines.extend(f"- {item}" for item in payload.get("actions", []))
    return "\n".join(lines) + "\n"


def render_html(payload: dict[str, Any]) -> str:
    body = html.escape(render_md(payload))
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Waiting Period Optimization</title>
<style>
body{{margin:0;background:#f6f8fb;color:#172033;font:14px/1.6 "Segoe UI",Arial,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px}}
pre{{white-space:pre-wrap;background:#fff;border:1px solid #d7e0ec;border-radius:8px;padding:18px}}
</style></head>
<body><main><pre>{body}</pre></main></body></html>
"""


def write_outputs(runtime_dir: Path, reports_dir: Path, payload: dict[str, Any]) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "waiting_period_optimization_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (reports_dir / "waiting_period_optimization_latest.md").write_text(render_md(payload), encoding="utf-8")
    (reports_dir / "waiting_period_optimization_latest.html").write_text(render_html(payload), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No-Binance-pressure waiting-period optimization report.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--reports-dir", type=Path)
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args(argv)
    payload = build_payload(args.root, args.hours)
    runtime_dir = args.runtime_dir or args.root / "runtime"
    reports_dir = args.reports_dir or args.root / "reports"
    write_outputs(runtime_dir, reports_dir, payload)
    print(json.dumps({"status": payload["status"], "safety": SAFETY, "output": str(runtime_dir / "waiting_period_optimization_latest.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
