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
ACCOUNT_STATE_RECOVERY_TTL_SEC = 14400.0
REASON_LABELS = {
    "duplicate_position": "已有同币种/同方向仓位，避免重复开仓",
    "account_state_unavailable": "开仓前账户资料暂时不够新；恢复期会用已验证快照/用户流补新，不该长期挡住信号",
    "central_account_state_unavailable": "中心账户资料暂时不够新；先修刷新链路，不把有效信号当没机会",
    "scanner_order_disabled": "观察模式关闭下单，只收扫描证据",
    "market_data_unavailable": "行情/K线缓存不足，策略不硬开",
    "kline_unavailable": "K线缓存不足，策略不硬开",
    "execution_preflight": "交易所规则预检不过，提前拦住",
    "exchange_error": "交易所/API 返回失败，需要继续观察",
    "open_submitted_unconfirmed": "订单已提交但未确认成仓，等待回执/核对",
    "open_unfilled": "交易所收到开仓请求但未返回成交数量",
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


def account_state_candidates(*paths: Path) -> list[tuple[float, Path, dict[str, Any]]]:
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for path in paths:
        if not path.exists():
            continue
        payload = read_json(path)
        rows = payload.get("accounts") if isinstance(payload.get("accounts"), list) else []
        if not rows:
            continue
        newest_ts = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            dt = parse_dt(row.get("ts") or row.get("snapshot_ts"))
            if dt:
                newest_ts = max(newest_ts, dt.timestamp())
        if newest_ts <= 0:
            try:
                newest_ts = path.stat().st_mtime
            except Exception:
                newest_ts = 0.0
        candidates.append((newest_ts, path, payload))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


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
            cooldown_table = "api_cooldowns" if table_exists(conn, "api_cooldowns") else "cooldowns" if table_exists(conn, "cooldowns") else ""
            if cooldown_table:
                out["active_cooldowns"] = int(conn.execute(
                    f"select count(*) from {cooldown_table} where until_ms > ?", (now_ms,)
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


def apply_alert_queue_fallback(queue: dict[str, Any], runtime_dir: Path, mirror_runtime: Path) -> dict[str, Any]:
    if queue.get("available") and (queue.get("active_cooldowns") or queue.get("recent_bad")):
        return queue
    alerts = read_json(first_existing(runtime_dir / "alerts_latest.json", mirror_runtime / "alerts_latest.json") or Path(""))
    rows = alerts.get("alerts") if isinstance(alerts.get("alerts"), list) else []
    has_rate_limit = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = f"{row.get('title') or ''} {row.get('body') or ''}".lower()
        if "binance api限流" in text or "418" in text or "429" in text or "-1003" in text or "cooldown" in text:
            has_rate_limit = True
            break
    if not has_rate_limit:
        return queue
    out = dict(queue)
    out["alert_rate_limit_fallback"] = True
    out["available"] = bool(out.get("available"))
    out["active_cooldowns"] = max(1, int(out.get("active_cooldowns") or 0))
    out["recent_bad"] = max(1, int(out.get("recent_bad") or 0))
    out["note"] = "队列明细未拉回或不可读，但新鲜告警显示 Binance 限流/冷却；按有冷却处理。"
    return out


def account_state_review(runtime_dir: Path, mirror_runtime: Path) -> dict[str, Any]:
    """Explain whether account-state freshness can block pre-entry gates.

    This is read-only. It does not refresh account state or call Binance.
    """
    candidates = account_state_candidates(
        runtime_dir / "account_state_latest.json",
        mirror_runtime / "account_state_latest.json",
        runtime_dir / "account_snapshot_latest.json",
        mirror_runtime / "account_snapshot_latest.json",
    )
    path = candidates[0][1] if candidates else None
    payload = candidates[0][2] if candidates else {}
    pre_entry_ttl = ACCOUNT_STATE_RECOVERY_TTL_SEC
    confirm_ttl = 15.0
    out: dict[str, Any] = {
        "available": False,
        "source": str(path) if path else "",
        "pre_entry_ttl_sec": int(pre_entry_ttl),
        "post_submit_confirm_ttl_sec": int(confirm_ttl),
        "pre_entry_blocking": True,
        "status": "missing",
        "plain_status": "没有账户资料文件；如果策略运行，会被账户风控挡住。",
        "accounts": [],
    }
    rows = payload.get("accounts") if isinstance(payload.get("accounts"), list) else []
    if not rows:
        return out
    out["available"] = True
    blocking = False
    accounts = []
    now = datetime.now(CST)
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        strategy = str(raw.get("strategy") or f"{raw.get('account') or raw.get('key')}/{raw.get('version') or ''}").strip("/")
        stale = bool(raw.get("stale"))
        ts = parse_dt(raw.get("ts") or raw.get("snapshot_ts"))
        age_sec = (now - ts.astimezone(CST)).total_seconds() if ts else None
        fresh_for_entry = bool(ts) and not stale and age_sec is not None and 0 <= age_sec <= pre_entry_ttl
        if not fresh_for_entry:
            blocking = True
        accounts.append({
            "strategy": strategy or "unknown",
            "age": age_text(ts),
            "age_sec": int(age_sec) if age_sec is not None else None,
            "stale": stale,
            "fresh_for_entry": fresh_for_entry,
            "open_positions": int(raw.get("open_positions") or 0),
            "snapshot_error": str(raw.get("snapshot_error") or ""),
        })
    out["accounts"] = accounts
    out["pre_entry_blocking"] = blocking
    if blocking:
        out["status"] = "blocking_pre_entry"
        out["plain_status"] = "有账户资料过旧或标记 stale；会挡住开仓前风控，需要等用户流/快照链路恢复。"
    else:
        out["status"] = "fresh_for_pre_entry"
        out["plain_status"] = "账户资料在恢复期 TTL 内；当前不应因为“账户状态不够新”挡住开仓信号。"
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
        "open_failed_plain_reasons": [],
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
            failed_rows = conn.execute(
                """
                select reason, stage, layer
                from events
                where event_type='OPEN_FAILED' and (ts >= ? or ts >= ?)
                """,
                (since, since_space),
            ).fetchall()
            failed_plain_reasons: Counter[str] = Counter()
            for row in failed_rows:
                failed_plain_reasons[plain_reason(row["reason"], row["stage"], row["layer"])] += 1
            out["open_failed_plain_reasons"] = [
                {"reason": reason, "count": count}
                for reason, count in failed_plain_reasons.most_common(12)
            ]
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
    queue = apply_alert_queue_fallback(queue_review(runtime_dir, mirror_runtime), runtime_dir, mirror_runtime)
    skipped = open_skipped_review(event_db, hours)
    gaps = research_gap_review(runtime_dir, mirror_runtime)
    top100 = top100_review(runtime_dir, mirror_runtime)
    scan_coverage = scan_coverage_review(event_db, top100.get("top100_symbols") or [], hours)
    live_activity = live_activity_review(runtime_dir)
    account_state = account_state_review(runtime_dir, mirror_runtime)
    active_cooldowns = int(queue.get("active_cooldowns") or 0)
    active_requests = int(queue.get("active_requests") or 0)
    recent_bad = int(queue.get("recent_bad") or 0)
    account_blocking = bool(account_state.get("pre_entry_blocking"))
    if active_cooldowns:
        status = "blocked_by_cooldown"
    elif active_requests:
        status = "blocked_by_queue"
    elif account_blocking:
        status = "blocked_by_account_state"
    elif recent_bad:
        status = "cooldown_clear_recent_bad_history"
    else:
        status = "safe_to_optimize_offline"
    blocking_now = bool(active_cooldowns or active_requests or account_blocking)
    if active_cooldowns:
        readiness_reason = "当前有 Binance 冷却；先停止恢复和扩量，只做离线检查。"
    elif active_requests:
        readiness_reason = "队列里还有未完成请求；先等清空，再判断下一层恢复。"
    elif account_blocking:
        readiness_reason = "账户资料仍会挡开仓；先修复账户状态/用户流，不恢复订单、不提频。"
    elif recent_bad:
        readiness_reason = "当前冷却和队列已清；历史坏请求只作风险提示，仍按分阶段恢复计划推进。"
    else:
        readiness_reason = "队列当前干净；只能推进离线报表、计划、回放骨架。"
    readiness = {
        "decision": "hold_frequency" if blocking_now else "ready_for_plan_only_data_work",
        "can_raise_frequency": False,
        "can_submit_kline_depth": False,
        "can_restart_for_experiment": False,
        "reason": readiness_reason,
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
        "account_state": account_state,
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
            "account_state_blocking": account_blocking,
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
    account_state = payload.get("account_state") or {}
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
        f"- 账户资料是否挡开仓: `{'是' if account_state.get('pre_entry_blocking') else '否'}` - {account_state.get('plain_status')}",
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
        "### 账户状态新鲜度",
        "",
        f"- 开仓前恢复期 TTL: `{account_state.get('pre_entry_ttl_sec')}` 秒",
        f"- 下单后成仓确认 TTL: `{account_state.get('post_submit_confirm_ttl_sec')}` 秒",
        f"- 判断: {account_state.get('plain_status')}",
        "",
    ])
    account_rows = [["Strategy", "Age", "Fresh for entry", "Open positions", "Error"]]
    for row in account_state.get("accounts", []):
        account_rows.append([
            row.get("strategy"),
            row.get("age"),
            "yes" if row.get("fresh_for_entry") else "no",
            row.get("open_positions"),
            row.get("snapshot_error") or "-",
        ])
    lines.extend(md_table(account_rows) or ["No account-state rows."])
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


def level_for_pct(value: Any) -> str:
    try:
        pct = float(value)
    except Exception:
        pct = 0.0
    if pct >= 80:
        return "good"
    if pct >= 35:
        return "warn"
    return "bad"


def pct_width(value: Any) -> str:
    try:
        pct = max(0.0, min(100.0, float(value)))
    except Exception:
        pct = 0.0
    return f"{pct:.1f}%"


def render_html(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    top100 = payload.get("top100") if isinstance(payload.get("top100"), dict) else {}
    coverage = payload.get("scan_coverage") if isinstance(payload.get("scan_coverage"), dict) else {}
    skipped = payload.get("open_skipped") if isinstance(payload.get("open_skipped"), dict) else {}
    gaps = payload.get("research_gaps") if isinstance(payload.get("research_gaps"), dict) else {}
    readiness = payload.get("readiness") if isinstance(payload.get("readiness"), dict) else {}
    live = payload.get("live_activity") if isinstance(payload.get("live_activity"), dict) else {}
    account_state = payload.get("account_state") if isinstance(payload.get("account_state"), dict) else {}
    measured = (coverage.get("coverage_status") or "measured") == "measured"
    top_pct = float(summary.get("top100_pct") or 0.0)
    generated = parse_dt(payload.get("generated_at"))
    coverage_label = (
        f"{summary.get('top100_scanned', 0)}/{coverage.get('target_count', 100)}"
        if measured
        else "未知"
    )
    reason_rows = "".join(
        f"<tr><td>{html.escape(str(row.get('reason') or ''))}</td><td>{html.escape(str(row.get('count') or 0))}</td></tr>"
        for row in skipped.get("scan_stats_reasons", [])
        if isinstance(row, dict)
    ) or '<tr><td colspan="2">暂无新鲜原因行；继续等扫描自然产出。</td></tr>'
    live_rows = "".join(
        f"""
<tr>
  <td>{html.escape(str(strategy))}</td>
  <td><span class="pill {('good' if row.get('service') == 'active' else 'warn')}">{html.escape(str(row.get('service') or 'unknown'))}</span></td>
  <td>{html.escape(str(row.get('activity_age') or '无记录'))}</td>
</tr>
""".strip()
        for strategy, row in (live.get("strategies") or {}).items()
        if isinstance(row, dict)
    ) or '<tr><td colspan="3">暂无 live heartbeat。</td></tr>'
    account_rows = "".join(
        f"""
<tr>
  <td>{html.escape(str(row.get('strategy') or 'unknown'))}</td>
  <td>{html.escape(str(row.get('age') or '无记录'))}</td>
  <td><span class="pill {('good' if row.get('fresh_for_entry') else 'bad')}">{'不挡开仓' if row.get('fresh_for_entry') else '会挡开仓'}</span></td>
  <td>{html.escape(str(row.get('open_positions', 0)))}</td>
</tr>
""".strip()
        for row in account_state.get("accounts", [])
        if isinstance(row, dict)
    ) or '<tr><td colspan="4">暂无账户资料。</td></tr>'
    coverage_rows = "".join(
        f"""
<tr>
  <td>{html.escape(str(strategy))}</td>
  <td>{html.escape(str(row.get('scanned_symbols', 0)))}</td>
  <td>{html.escape(str(row.get('top100_scanned', 0)))}/100</td>
  <td><div class="bar"><i class="{level_for_pct(row.get('top100_pct'))}" style="width:{pct_width(row.get('top100_pct'))}"></i></div><small>{html.escape(str(row.get('top100_pct', 0)))}%</small></td>
</tr>
""".strip()
        for strategy, row in (coverage.get("by_strategy") or {}).items()
        if isinstance(row, dict)
    ) or '<tr><td colspan="4">覆盖数据未知；镜像未新鲜时不显示假 0%。</td></tr>'
    action_rows = "".join(f"<li>{html.escape(str(item))}</li>" for item in payload.get("actions", []))
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>等待期离线优化</title>
<style>
:root{{
  --bg:#101418; --panel:#f8fafc; --panel2:#ffffff; --ink:#101828; --muted:#667085;
  --line:#d7dee8; --green:#16a34a; --amber:#d97706; --red:#dc2626; --cyan:#0891b2; --violet:#7c3aed;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:linear-gradient(180deg,#101418 0,#17202a 320px,#eef2f7 321px);color:var(--ink);font:14px/1.55 "Segoe UI",Arial,sans-serif}}
main{{max-width:1220px;margin:0 auto;padding:26px}}
.hero{{color:white;display:grid;grid-template-columns:1fr auto;gap:18px;align-items:end;margin-bottom:18px}}
h1{{margin:0;font-size:32px;letter-spacing:0}} .sub{{color:#cbd5e1;margin-top:6px}}
.badge{{border:1px solid rgba(255,255,255,.24);background:rgba(255,255,255,.08);padding:10px 14px;border-radius:8px;font-weight:800}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:14px}}
.card,.panel{{background:var(--panel2);border:1px solid var(--line);border-radius:8px;box-shadow:0 12px 30px rgba(15,23,42,.08)}}
.card{{padding:15px;min-height:108px}} .card span,.panel small{{color:var(--muted);display:block;font-size:12px}}
.card b{{display:block;font-size:24px;margin:7px 0 2px}} .card.good{{border-top:4px solid var(--green)}} .card.warn{{border-top:4px solid var(--amber)}} .card.bad{{border-top:4px solid var(--red)}} .card.info{{border-top:4px solid var(--cyan)}}
.layout{{display:grid;grid-template-columns:1.15fr .85fr;gap:14px;align-items:start}}
.panel{{padding:16px;margin-bottom:14px}} h2{{font-size:18px;margin:0 0 12px}}
table{{width:100%;border-collapse:collapse}} th,td{{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}} th{{font-size:12px;color:var(--muted)}}
.bar{{height:10px;background:#e5e7eb;border-radius:999px;overflow:hidden;min-width:120px}} .bar i{{display:block;height:100%;border-radius:999px}} .bar .good{{background:var(--green)}} .bar .warn{{background:var(--amber)}} .bar .bad{{background:var(--red)}}
.pill{{display:inline-block;border-radius:999px;padding:4px 9px;font-weight:800;font-size:12px;background:#e5e7eb;color:#334155}} .pill.good{{background:#dcfce7;color:#166534}} .pill.warn{{background:#fef3c7;color:#92400e}} .pill.bad{{background:#fee2e2;color:#991b1b}}
.gate{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}} .gate div{{border:1px solid var(--line);border-radius:8px;padding:11px;background:#f8fafc}} .gate b{{display:block;font-size:16px}}
ul{{margin:0;padding-left:18px}} li{{margin:6px 0}} .note{{color:var(--muted);margin:0}}
@media(max-width:900px){{.hero,.layout,.grid,.gate{{grid-template-columns:1fr}}}}
</style></head>
<body><main>
  <section class="hero">
    <div>
      <h1>等待期离线优化</h1>
      <div class="sub">生成 {html.escape(age_text(generated))}。只读本地/镜像数据，不请求 Binance，不提交队列，不重启服务。</div>
    </div>
    <div class="badge">{html.escape(str(readiness.get("decision") or payload.get("status") or "unknown"))}</div>
  </section>
  <section class="grid">
    <article class="card {('good' if not summary.get('active_cooldowns') else 'bad')}"><span>当前冷却</span><b>{html.escape(str(summary.get('active_cooldowns', 0)))}</b><small>必须为 0 才考虑放开</small></article>
    <article class="card {('good' if not summary.get('active_requests') else 'warn')}"><span>队列等待</span><b>{html.escape(str(summary.get('active_requests', 0)))}</b><small>清空后再看下一层</small></article>
    <article class="card {('bad' if account_state.get('pre_entry_blocking') else 'good')}"><span>账户资料挡开仓</span><b>{'是' if account_state.get('pre_entry_blocking') else '否'}</b><small>{html.escape(str(account_state.get('status') or 'missing'))}</small></article>
    <article class="card {level_for_pct(top_pct) if measured else 'warn'}"><span>Top100 实扫</span><b>{html.escape(coverage_label)}</b><small>{html.escape(str(top_pct if measured else coverage.get('coverage_status') or 'unknown'))}{'%' if measured else ''}</small></article>
  </section>
  <section class="layout">
    <div>
      <section class="panel">
        <h2>小放开闸门</h2>
        <div class="gate">
          <div><span>提扫描频率</span><b>{'禁止' if not readiness.get('can_raise_frequency') else '可考虑'}</b><small>频率是最直接 API 压力</small></div>
          <div><span>Kline/depth submit</span><b>{'禁止' if not readiness.get('can_submit_kline_depth') else '可考虑'}</b><small>先等稳定窗口</small></div>
          <div><span>实验性重启</span><b>{'禁止' if not readiness.get('can_restart_for_experiment') else '可考虑'}</b><small>只做必要最小动作</small></div>
        </div>
        <p class="note">{html.escape(str(readiness.get('reason') or '继续观察'))}</p>
      </section>
      <section class="panel">
        <h2>Top100 覆盖矩阵</h2>
        <table><thead><tr><th>策略</th><th>实扫币种</th><th>Top100</th><th>进度</th></tr></thead><tbody>{coverage_rows}</tbody></table>
      </section>
      <section class="panel">
        <h2>为什么还没开仓</h2>
        <table><thead><tr><th>白话原因</th><th>次数</th></tr></thead><tbody>{reason_rows}</tbody></table>
      </section>
    </div>
    <aside>
      <section class="panel">
        <h2>实时心跳</h2>
        <table><thead><tr><th>策略</th><th>服务</th><th>最新活动</th></tr></thead><tbody>{live_rows}</tbody></table>
      </section>
      <section class="panel">
        <h2>账户状态新鲜度</h2>
        <p class="note">{html.escape(str(account_state.get('plain_status') or '缺少账户状态'))}</p>
        <table><thead><tr><th>策略</th><th>年龄</th><th>开仓前</th><th>持仓</th></tr></thead><tbody>{account_rows}</tbody></table>
      </section>
      <section class="panel">
        <h2>安全边界</h2>
        <ul>{action_rows}</ul>
      </section>
      <section class="panel">
        <h2>数据源</h2>
        <p class="note">Top100 缓存：{html.escape(str(top100.get('cache_symbols', 0)))} 个，{html.escape(str(top100.get('cache_age') or '无记录'))}。覆盖源：{html.escape(str(coverage.get('source') or '无'))}。</p>
      </section>
    </aside>
  </section>
</main></body></html>
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
