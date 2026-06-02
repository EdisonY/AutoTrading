"""Generate lightweight operational alerts for the command-center page."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
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
EVENT_STORE_DB = ROOT / "runtime" / "event_store.sqlite3"
MARKET_CACHE = ROOT / "runtime" / "market_data_cache.json"
PORTAL_HTML = ROOT / "reports" / "portal_latest.html"
ACCOUNT_LATEST = ROOT / "runtime" / "account_snapshot_latest.json"
ACCOUNT_ERROR_LATEST = ROOT / "runtime" / "account_snapshot_error_latest.json"
BINANCE_API_GUARD_STATE = ROOT / "runtime" / "binance_api_guard_state.json"
PORTAL_REFRESH_STATUS = ROOT / "runtime" / "portal_refresh_latest.json"
STRATEGY_EVOLUTION_LATEST = ROOT / "runtime" / "strategy_evolution_latest.json"
ATTENTION_LATEST = ROOT / "research_memory" / "attention" / "open_items.json"
ALERT_JSON = ROOT / "runtime" / "alerts_latest.json"
ALERT_LOG = ROOT / "logs" / "alerts.jsonl"
ALERT_MD = ROOT / "reports" / "alerts_latest.md"
CST = timezone(timedelta(hours=8))
ATTENTION_STALE_SECONDS = 150 * 60
API_RATE_LIMIT_WINDOW_MINUTES = 30
ACCOUNT_SNAPSHOT_EXPECTED_INTERVAL_SECONDS = 900
ACCOUNT_SNAPSHOT_STALE_GRACE_SECONDS = 120
BINANCE_BAN_UNTIL_RE = re.compile(r"banned until\s+(\d{12,})", re.IGNORECASE)
API_RATE_LIMIT_MARKERS = ("HTTP 418", "HTTP 429", "-1003", "Way too many requests", "Too many requests")

SERVICES = [
    "crypto-market-data-cache.service",
    "crypto-account-snapshot.service",
    "crypto-scanner.service",
    "crypto-scanner-v14.service",
    "crypto-scanner-v16.service",
    "crypto-market-mover-sentinel.service",
]

TIMERS = [
    "crypto-data-maintenance.timer",
]
ACCOUNT_RESUME_TIMER = "crypto-account-snapshot-resume.timer"

WATCH_SHARDS = [
    ROOT / "logs" / "decisions",
    ROOT / "logs_v14" / "decisions",
    ROOT / "logs_v14" / "signals",
    ROOT / "logs_v16" / "decisions",
    ROOT / "scanner_data" / "events",
    ROOT / "scanner_data_v14" / "events",
    ROOT / "scanner_data_v16" / "events",
]

WATCH_TEXT_LOGS = [
    ROOT / "logs" / "scanner_stderr.log",
    ROOT / "logs" / "scanner_stdout.log",
    ROOT / "logs_v14" / "stderr.log",
    ROOT / "logs_v14" / "stdout.log",
    ROOT / "logs_v16" / "stderr.log",
    ROOT / "logs_v16" / "stdout.log",
]


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


def unit_states(units: list[str]) -> dict[str, str]:
    if not sys.platform.startswith("linux"):
        return {}
    states: dict[str, str] = {}
    for unit in units:
        proc = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)
        states[unit] = proc.stdout.strip() or proc.stderr.strip() or "unknown"
    return states


def read_account_error() -> dict[str, Any]:
    if not ACCOUNT_ERROR_LATEST.exists():
        return {}
    try:
        payload = json.loads(ACCOUNT_ERROR_LATEST.read_text(encoding="utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def account_retry_at(payload: dict[str, Any]) -> datetime | None:
    parsed = parse_dt(payload.get("retry_at"))
    if parsed:
        return parsed
    return retry_at_from_text(str(payload.get("error") or ""))


def retry_at_from_text(text: str) -> datetime | None:
    match = BINANCE_BAN_UNTIL_RE.search(text or "")
    if not match:
        return None
    try:
        raw_ms = int(match.group(1))
    except ValueError:
        return None
    return datetime.fromtimestamp(raw_ms / 1000, CST)


def compact_log_line(text: str, limit: int = 260) -> str:
    clean = " ".join((text or "").strip().split())
    if len(clean) <= limit:
        return clean
    head = max(80, limit // 2 - 5)
    tail = max(80, limit - head - 5)
    return f"{clean[:head]} ... {clean[-tail:]}"


def service_states() -> dict[str, str]:
    return unit_states(SERVICES)


def systemctl_value(unit: str, prop: str) -> str:
    if not sys.platform.startswith("linux"):
        return ""
    proc = subprocess.run(["systemctl", "show", unit, f"-p{prop}", "--value"], capture_output=True, text=True, timeout=5)
    return proc.stdout.strip()


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            parts = rest.strip().split()
            if not parts:
                continue
            values[key] = int(parts[0])
    except Exception:
        return {}
    return values


def recent_oom_lines() -> list[str]:
    if not sys.platform.startswith("linux"):
        return []
    try:
        proc = subprocess.run(
            ["journalctl", "-k", "--since", "6 hours ago", "--no-pager", "-n", "240"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    lines = []
    for line in (proc.stdout or "").splitlines():
        lower = line.lower()
        if "out of memory" in lower or "oom-kill" in lower or "killed process" in lower:
            lines.append(line.strip())
    return lines[-5:]


def recent_api_rate_limits(now: datetime) -> dict[str, Any]:
    empty = {"total": 0, "by_service": {}, "latest": "", "latest_ts": None, "ban_until": None}
    if not sys.platform.startswith("linux"):
        return empty
    by_service: dict[str, int] = {}
    latest = ""
    latest_ts: datetime | None = None
    ban_until: datetime | None = None
    since = f"{API_RATE_LIMIT_WINDOW_MINUTES} minutes ago"
    for service in SERVICES:
        try:
            proc = subprocess.run(
                ["journalctl", "-u", service, "--since", since, "--no-pager", "-n", "260"],
                capture_output=True,
                text=True,
                timeout=4,
            )
        except Exception:
            continue
        count = 0
        for line in (proc.stdout or "").splitlines():
            if not any(marker in line for marker in API_RATE_LIMIT_MARKERS):
                continue
            count += 1
            latest = compact_log_line(line)
            maybe_retry = retry_at_from_text(line)
            if maybe_retry and (ban_until is None or maybe_retry > ban_until):
                ban_until = maybe_retry
            parts = line.split()
            if len(parts) >= 3:
                raw_ts = f"{now.year}-{parts[0]}-{parts[1]} {parts[2]}"
                parsed = parse_dt(raw_ts)
                if parsed:
                    latest_ts = parsed
        if count:
            by_service[service] = count
    total = sum(by_service.values())
    return {
        "total": total,
        "by_service": by_service,
        "latest": latest,
        "latest_ts": latest_ts.isoformat() if latest_ts else None,
        "ban_until": ban_until.isoformat() if ban_until else None,
    }


def read_binance_api_guard(now: datetime) -> dict[str, Any]:
    if not BINANCE_API_GUARD_STATE.exists():
        return {}
    try:
        payload = json.loads(BINANCE_API_GUARD_STATE.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return {"error": str(exc)}
    banned_until_ms = int(payload.get("banned_until_ms") or 0)
    banned_until = datetime.fromtimestamp(banned_until_ms / 1000, CST) if banned_until_ms else None
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    recent = payload.get("recent_requests") if isinstance(payload.get("recent_requests"), list) else []
    recent_public = payload.get("recent_public_requests") if isinstance(payload.get("recent_public_requests"), list) else []
    recent_counts: dict[str, int] = {}
    priority_counts: dict[str, int] = {}
    for item in recent:
        key = f"{item.get('account')}:{item.get('method')}:{item.get('path')}"
        recent_counts[key] = recent_counts.get(key, 0) + 1
        priority = str(item.get("priority") or "normal")
        priority_counts[priority] = priority_counts.get(priority, 0) + 1
    recent_public_counts: dict[str, int] = {}
    for item in recent_public:
        key = f"{item.get('label')}:{item.get('path')}"
        recent_public_counts[key] = recent_public_counts.get(key, 0) + 1
    return {
        "updated_at_ms": payload.get("updated_at_ms"),
        "last_account": payload.get("last_account"),
        "last_method": payload.get("last_method"),
        "last_path": payload.get("last_path"),
        "last_priority": payload.get("last_priority") or "",
        "last_status": payload.get("last_status"),
        "last_error_at_ms": payload.get("last_error_at_ms"),
        "last_error_account": payload.get("last_error_account") or "",
        "last_error_method": payload.get("last_error_method") or "",
        "last_error_path": payload.get("last_error_path") or "",
        "last_error_status": payload.get("last_error_status") or "",
        "banned_until": banned_until.isoformat() if banned_until else "",
        "in_cooldown": bool(banned_until and banned_until > now),
        "rolling_count_60s": int(payload.get("rolling_count_60s") or len(recent)),
        "rolling_account_count_60s": int(payload.get("rolling_account_count_60s") or 0),
        "rolling_limited_account": payload.get("rolling_limited_account") or "",
        "rolling_limited_priority": payload.get("rolling_limited_priority") or "",
        "max_requests_per_min": int(payload.get("max_requests_per_min") or 0),
        "max_account_requests_per_min": int(payload.get("max_account_requests_per_min") or 0),
        "trade_priority_reserve_per_min": int(payload.get("trade_priority_reserve_per_min") or 0),
        "normal_priority_limit_per_min": int(payload.get("normal_priority_limit_per_min") or 0),
        "priority_counts_60s": priority_counts,
        "public_rolling_count_60s": int(payload.get("public_rolling_count_60s") or len(recent_public)),
        "public_max_requests_per_min": int(payload.get("public_max_requests_per_min") or 0),
        "top_public_paths_60s": sorted(
            ({"name": str(name), "count": int(count or 0)} for name, count in recent_public_counts.items()),
            key=lambda item: item["count"],
            reverse=True,
        )[:6],
        "top_paths_60s": sorted(
            ({"name": str(name), "count": int(count or 0)} for name, count in recent_counts.items()),
            key=lambda item: item["count"],
            reverse=True,
        )[:6],
        "top_paths": sorted(
            ({"name": str(name), "count": int(count or 0)} for name, count in stats.items()),
            key=lambda item: item["count"],
            reverse=True,
        )[:6],
    }


def recent_failed_close_alerts(now: datetime) -> list[dict[str, str]]:
    if not EVENT_STORE_DB.exists():
        return []
    try:
        con = sqlite3.connect(EVENT_STORE_DB)
        rows = con.execute(
            """
            select ts, strategy, symbol, side, event_type, reason, payload_json
            from events
            where event_type in ('FORCED_CLOSE_FAILED', 'CLOSE_FAILED', 'CLOSE_CONFIRM_FAILED', 'OPEN_SIZING_MISMATCH_FAILED')
            order by id desc
            limit 200
            """
        ).fetchall()
        con.close()
    except Exception:
        return []
    live_positions: set[tuple[str, str, str]] = set()
    live_symbols: set[tuple[str, str]] = set()
    if ACCOUNT_LATEST.exists():
        try:
            account_payload = json.loads(ACCOUNT_LATEST.read_text(encoding="utf-8", errors="replace"))
            for account in account_payload.get("accounts") or []:
                strategy = str(account.get("strategy") or f"{account.get('account')}/{account.get('version')}" or "")
                for pos in account.get("positions") or []:
                    symbol = str(pos.get("symbol") or "").upper()
                    side = str(pos.get("side") or "").upper()
                    if symbol:
                        live_symbols.add((strategy, symbol))
                        live_positions.add((strategy, symbol, side))
        except Exception:
            live_positions = set()
            live_symbols = set()
    recent = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for raw_ts, strategy, symbol, side, event_type, reason, payload_json in rows:
        dt = parse_dt(raw_ts)
        if not dt or (now - dt).total_seconds() > 2 * 3600:
            continue
        strategy_key = str(strategy or "")
        symbol_key = str(symbol or "").upper()
        side_key = str(side or "").upper()
        if live_symbols:
            if side_key and (strategy_key, symbol_key, side_key) not in live_positions:
                continue
            if not side_key and (strategy_key, symbol_key) not in live_symbols:
                continue
        detail = reason
        try:
            payload = json.loads(payload_json or "{}")
            raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
            raw_event = payload.get("raw_event") if isinstance(payload.get("raw_event"), dict) else {}
            detail = (
                payload.get("failure_reason")
                or raw.get("failure_reason")
                or raw_event.get("failure_reason")
                or payload.get("reason")
                or detail
            )
        except Exception:
            pass
        key = (str(raw_ts), str(strategy), str(symbol), str(side), str(event_type))
        if key in seen:
            continue
        seen.add(key)
        recent.append(f"{strategy or '-'} {symbol} {side} {event_type} {detail}"[:180])
    if not recent:
        return []
    return [{
        "level": "bad",
        "title": "强平/平仓/仓位尺寸确认失败",
        "body": f"近2小时 {len(recent)} 条；最新: {recent[0]}",
    }]


def collect_alerts() -> dict[str, Any]:
    now = datetime.now(CST)
    alerts: list[dict[str, str]] = []
    states = service_states()
    account_error_payload = read_account_error()
    account_retry = account_retry_at(account_error_payload)
    account_resume_timer_state = unit_states([ACCOUNT_RESUME_TIMER]).get(ACCOUNT_RESUME_TIMER, "")
    api_rate_limits = recent_api_rate_limits(now)
    api_guard = read_binance_api_guard(now)
    account_in_cooldown = bool(
        (account_retry and account_retry > now)
        or account_resume_timer_state == "active"
    )
    for service, state in states.items():
        if state != "active":
            if service == "crypto-account-snapshot.service" and account_in_cooldown:
                resume_text = account_retry.isoformat() if account_retry else "resume timer active"
                alerts.append({
                    "level": "warn",
                    "title": "账户快照API冷却中",
                    "body": f"systemd 状态为 {state}；为避免 Binance 418 继续延长，暂停到 {resume_text} 后恢复。",
                })
            else:
                alerts.append({"level": "bad", "title": f"服务异常：{service}", "body": f"systemd 状态为 {state}"})

    timers = unit_states(TIMERS)
    for timer, state in timers.items():
        if state != "active":
            alerts.append({"level": "bad", "title": f"定时任务未运行：{timer}", "body": f"systemd 状态为 {state}"})

    maintenance_result = systemctl_value("crypto-data-maintenance.service", "Result")
    if maintenance_result and maintenance_result not in {"success", ""}:
        alerts.append({"level": "bad", "title": "数据维护任务失败", "body": f"crypto-data-maintenance.service Result={maintenance_result}"})

    disk_payload: dict[str, Any] = {}
    try:
        disk = shutil.disk_usage(ROOT)
        used_pct = (disk.used / disk.total * 100) if disk.total else 0.0
        disk_payload = {
            "total_gb": round(disk.total / 1024**3, 2),
            "used_gb": round(disk.used / 1024**3, 2),
            "free_gb": round(disk.free / 1024**3, 2),
            "used_pct": round(used_pct, 1),
        }
        if used_pct >= 85:
            alerts.append({"level": "bad", "title": "磁盘使用率过高", "body": f"/opt 所在分区已用 {used_pct:.1f}%，剩余 {disk.free / 1024**3:.1f}GB"})
        elif used_pct >= 70:
            alerts.append({"level": "warn", "title": "磁盘使用率偏高", "body": f"/opt 所在分区已用 {used_pct:.1f}%，剩余 {disk.free / 1024**3:.1f}GB"})
    except Exception as exc:
        alerts.append({"level": "warn", "title": "磁盘容量检查失败", "body": str(exc)})

    memory_payload: dict[str, Any] = {}
    meminfo = read_meminfo()
    if meminfo:
        mem_total = meminfo.get("MemTotal", 0)
        mem_available = meminfo.get("MemAvailable", 0)
        swap_total = meminfo.get("SwapTotal", 0)
        swap_free = meminfo.get("SwapFree", 0)
        memory_payload = {
            "mem_total_mb": round(mem_total / 1024, 1),
            "mem_available_mb": round(mem_available / 1024, 1),
            "swap_total_mb": round(swap_total / 1024, 1),
            "swap_used_mb": round((swap_total - swap_free) / 1024, 1),
        }
        if swap_total <= 0:
            alerts.append({"level": "warn", "title": "服务器未启用 swap", "body": "小内存节点无 swap，研究任务/OOM 时可能拖垮 SSH。"})
        if mem_available and mem_available < 250 * 1024:
            alerts.append({"level": "bad", "title": "可用内存过低", "body": f"MemAvailable 约 {mem_available / 1024:.0f}MB"})
        if swap_total > 0 and swap_free / swap_total < 0.2:
            alerts.append({"level": "warn", "title": "swap 使用率偏高", "body": f"swap 剩余 {swap_free / 1024:.0f}MB / {swap_total / 1024:.0f}MB"})
    oom_lines = recent_oom_lines()
    if oom_lines:
        alerts.append({"level": "bad", "title": "近期发生 OOM", "body": oom_lines[-1][-220:]})
        memory_payload["recent_oom_count"] = len(oom_lines)

    today_name = now.strftime("%Y-%m-%d.jsonl")
    for shard_dir in WATCH_SHARDS:
        path = shard_dir / today_name
        if path.exists() and path.stat().st_size > 512 * 1024 * 1024:
            alerts.append({"level": "warn", "title": "当日日志分片过大", "body": f"{path.relative_to(ROOT)} 已 {path.stat().st_size / 1024**2:.1f}MB"})
    for path in WATCH_TEXT_LOGS:
        if path.exists() and path.stat().st_size > 50 * 1024 * 1024:
            alerts.append({"level": "warn", "title": "文本日志过大", "body": f"{path.relative_to(ROOT)} 已 {path.stat().st_size / 1024**2:.1f}MB"})

    # NOTE: portal-refresh, counterfactual, evolution-gate, market-review checks
    # have been migrated to Aliyun analysis node. See FUTURE_EXECUTION_PLAN.md Phase 0.5.

    if MARKET_CACHE.exists():
        try:
            cache = json.loads(MARKET_CACHE.read_text(encoding="utf-8", errors="replace"))
            unix_ts = float(cache.get("unix_ts") or 0)
            age = time.time() - unix_ts if unix_ts else 999999
            if age > 90:
                alerts.append({"level": "warn", "title": "行情缓存偏旧", "body": f"缓存年龄 {age:.0f} 秒"})
        except Exception as exc:
            alerts.append({"level": "warn", "title": "行情缓存读取失败", "body": str(exc)})
    else:
        alerts.append({"level": "warn", "title": "行情缓存缺失", "body": str(MARKET_CACHE)})

    if ACCOUNT_LATEST.exists():
        try:
            account_payload = json.loads(ACCOUNT_LATEST.read_text(encoding="utf-8", errors="replace"))
            for account in account_payload.get("accounts") or []:
                count = int(account.get("sizing_violation_count") or 0)
                if count <= 0:
                    continue
                examples = []
                for pos in (account.get("sizing_violations") or [])[:3]:
                    examples.append(
                        f"{pos.get('symbol')} {pos.get('side')} qty={float(pos.get('qty') or 0):g} "
                        f"margin={float(pos.get('margin') or 0):.2f}"
                    )
                alerts.append({
                    "level": "bad",
                    "title": f"{account.get('strategy') or account.get('account')} 仓位保证金不符合规则",
                    "body": f"{count} 个持仓偏离目标保证金100 USDT：" + "；".join(examples),
                })
        except Exception as exc:
            alerts.append({"level": "warn", "title": "账户仓位尺寸检查失败", "body": str(exc)})

    if account_error_payload:
        try:
            err_ts = parse_dt(account_error_payload.get("ts"))
            err_text = str(account_error_payload.get("error") or "")
            if err_ts and (now - err_ts).total_seconds() <= 30 * 60:
                cooling = bool((account_retry and account_retry > now) or account_resume_timer_state == "active")
                level = "warn" if cooling else ("bad" if ("418" in err_text or "-1003" in err_text or "Way too many" in err_text) else "warn")
                title = "账户快照API冷却中" if cooling else "账户快照采集失败"
                retry_note = f"；retry_at={account_retry.isoformat()}" if account_retry else ""
                alerts.append({
                    "level": level,
                    "title": title,
                    "body": f"{err_ts.isoformat()}: {err_text[:220]}{retry_note}",
                })
        except Exception as exc:
            alerts.append({"level": "warn", "title": "账户快照错误记录读取失败", "body": str(exc)})

    if int(api_rate_limits.get("total") or 0) > 0:
        by_service = api_rate_limits.get("by_service") or {}
        offenders = sorted(by_service.items(), key=lambda item: int(item[1]), reverse=True)
        offender_text = "，".join(f"{name}:{count}" for name, count in offenders[:4])
        only_account_snapshot = set(by_service) <= {"crypto-account-snapshot.service"}
        level = "warn" if account_in_cooldown and only_account_snapshot else "bad"
        ban_until = api_rate_limits.get("ban_until") or (account_retry.isoformat() if account_retry else "")
        ban_note = f"；封禁到 {ban_until}" if ban_until else ""
        latest_note = f"；最新 {str(api_rate_limits.get('latest') or '')[:180]}" if api_rate_limits.get("latest") else ""
        alerts.append({
            "level": level,
            "title": "Binance API限流/封禁",
            "body": (
                f"近{API_RATE_LIMIT_WINDOW_MINUTES}分钟 {int(api_rate_limits.get('total') or 0)} 条；"
                f"来源 {offender_text or '-'}{ban_note}{latest_note}"
            ),
        })

    if api_guard.get("in_cooldown"):
        public_top = api_guard.get("top_public_paths_60s") if isinstance(api_guard.get("top_public_paths_60s"), list) else []
        signed_top = api_guard.get("top_paths_60s") if isinstance(api_guard.get("top_paths_60s"), list) else []
        public_note = ""
        if public_top:
            public_note = "；public 60s top " + "，".join(
                f"{item.get('name')}:{item.get('count')}" for item in public_top[:3]
            )
        elif signed_top:
            public_note = "；signed 60s top " + "，".join(
                f"{item.get('name')}:{item.get('count')}" for item in signed_top[:3]
            )
        alerts.append({
            "level": "warn",
            "title": "Binance API全局闸门冷却中",
            "body": (
                f"全进程共享保护已暂停 Binance REST 到 {api_guard.get('banned_until')}；"
                f"最近 {api_guard.get('last_error_account') or api_guard.get('last_account') or '-'} "
                f"{api_guard.get('last_error_method') or api_guard.get('last_method') or ''} "
                f"{api_guard.get('last_error_path') or api_guard.get('last_path') or ''}"
                f"{public_note}"
            ),
        })
    elif api_guard.get("error"):
        alerts.append({"level": "warn", "title": "Binance API闸门状态读取失败", "body": str(api_guard.get("error"))})

    if ATTENTION_LATEST.exists():
        try:
            attention_payload = json.loads(ATTENTION_LATEST.read_text(encoding="utf-8", errors="replace"))
            generated_at = parse_dt(attention_payload.get("generated_at"))
            if not generated_at or (now - generated_at).total_seconds() > ATTENTION_STALE_SECONDS:
                alerts.append({"level": "warn", "title": "持久关注台账偏旧", "body": f"最新台账 {generated_at or '无'}"})
        except Exception as exc:
            alerts.append({"level": "warn", "title": "持久关注台账读取失败", "body": str(exc)})
    else:
        alerts.append({"level": "warn", "title": "持久关注台账缺失", "body": str(ATTENTION_LATEST)})

    # NOTE: portal freshness checks migrated to Aliyun analysis node.

    if EVENT_STORE_DB.exists():
        try:
            con = sqlite3.connect(EVENT_STORE_DB)
            latest_event_rows = con.execute(
                "select ts from events where source not like '%/trades' order by id desc limit 5000"
            ).fetchall()
            latest_snapshot = con.execute("select ts from account_snapshots order by id desc limit 1").fetchone()
            total_snapshots = int(con.execute("select count(*) from account_snapshots").fetchone()[0])
            con.close()
            parsed_events = [dt for (raw_ts,) in latest_event_rows if (dt := parse_dt(raw_ts))]
            event_ts = max(parsed_events) if parsed_events else None
            snapshot_ts = parse_dt(latest_snapshot[0]) if latest_snapshot else None
            if not event_ts or (now - event_ts).total_seconds() > 900:
                alerts.append({"level": "warn", "title": "策略事件写入偏旧", "body": f"最新非交易事件 {event_ts or '无'}"})
            stale_threshold = (
                2 * 3600
                if account_in_cooldown
                else ACCOUNT_SNAPSHOT_EXPECTED_INTERVAL_SECONDS * 2 + ACCOUNT_SNAPSHOT_STALE_GRACE_SECONDS
            )
            if not snapshot_ts or (now - snapshot_ts).total_seconds() > stale_threshold:
                title = "账户快照冷却中，使用最后有效快照" if account_in_cooldown else "账户快照偏旧"
                alerts.append({"level": "warn", "title": title, "body": f"最新快照 {snapshot_ts or '无'}"})
            if total_snapshots == 0:
                alerts.append({"level": "warn", "title": "账户快照未入库", "body": "account_snapshots 表暂无记录"})
        except Exception as exc:
            alerts.append({"level": "bad", "title": "SQLite 健康检查失败", "body": str(exc)})
    else:
        alerts.append({"level": "bad", "title": "SQLite 事件库缺失", "body": str(EVENT_STORE_DB)})

    alerts.extend(recent_failed_close_alerts(now))

    return {
        "ts": now.isoformat(),
        "status": "ok" if not alerts else "bad" if any(a["level"] == "bad" for a in alerts) else "warn",
        "alert_count": len(alerts),
        "alerts": alerts,
        "services": states,
        "timers": timers,
        "disk": disk_payload,
        "memory": memory_payload,
        "api_rate_limits": api_rate_limits,
        "api_guard": api_guard,
    }


def write_outputs(payload: dict[str, Any]) -> None:
    ALERT_JSON.parent.mkdir(parents=True, exist_ok=True)
    ALERT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    ALERT_MD.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# 系统自动告警", "", f"- 时间: {payload['ts']}", f"- 状态: {payload['status']}", f"- 告警数: {payload['alert_count']}", ""]
    if payload["alerts"]:
        for alert in payload["alerts"]:
            lines.append(f"- [{alert['level']}] {alert['title']}: {alert['body']}")
    else:
        lines.append("- 当前无告警。")
    ALERT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="系统自动告警巡检")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        payload = collect_alerts()
        write_outputs(payload)
        print(json.dumps({"ts": payload["ts"], "status": payload["status"], "alert_count": payload["alert_count"]}, ensure_ascii=False), flush=True)
        if args.once:
            return 0
        time.sleep(max(20, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
