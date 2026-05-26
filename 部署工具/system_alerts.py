"""Generate lightweight operational alerts for the command-center page."""

from __future__ import annotations

import argparse
import json
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
PORTAL_REFRESH_STATUS = ROOT / "runtime" / "portal_refresh_latest.json"
STRATEGY_EVOLUTION_LATEST = ROOT / "runtime" / "strategy_evolution_latest.json"
ATTENTION_LATEST = ROOT / "research_memory" / "attention" / "open_items.json"
ALERT_JSON = ROOT / "runtime" / "alerts_latest.json"
ALERT_LOG = ROOT / "logs" / "alerts.jsonl"
ALERT_MD = ROOT / "reports" / "alerts_latest.md"
CST = timezone(timedelta(hours=8))

SERVICES = [
    "crypto-market-data-cache.service",
    "crypto-account-snapshot.service",
    "crypto-scanner.service",
    "crypto-scanner-v14.service",
    "crypto-scanner-v16.service",
    "crypto-market-mover-sentinel.service",
    "crypto-portal-refresh.service",
    "polymarket-monitor.service",
]

TIMERS = [
    "crypto-data-maintenance.timer",
    "crypto-market-review.timer",
    "crypto-counterfactual-open-skips.timer",
    "crypto-strategy-evolution-gate.timer",
]

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


def service_states() -> dict[str, str]:
    return unit_states(SERVICES)


def systemctl_value(unit: str, prop: str) -> str:
    if not sys.platform.startswith("linux"):
        return ""
    proc = subprocess.run(["systemctl", "show", unit, f"-p{prop}", "--value"], capture_output=True, text=True, timeout=5)
    return proc.stdout.strip()


def collect_alerts() -> dict[str, Any]:
    now = datetime.now(CST)
    alerts: list[dict[str, str]] = []
    states = service_states()
    for service, state in states.items():
        if state != "active":
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

    today_name = now.strftime("%Y-%m-%d.jsonl")
    for shard_dir in WATCH_SHARDS:
        path = shard_dir / today_name
        if path.exists() and path.stat().st_size > 512 * 1024 * 1024:
            alerts.append({"level": "warn", "title": "当日日志分片过大", "body": f"{path.relative_to(ROOT)} 已 {path.stat().st_size / 1024**2:.1f}MB"})
    for path in WATCH_TEXT_LOGS:
        if path.exists() and path.stat().st_size > 50 * 1024 * 1024:
            alerts.append({"level": "warn", "title": "文本日志过大", "body": f"{path.relative_to(ROOT)} 已 {path.stat().st_size / 1024**2:.1f}MB"})

    review_result = systemctl_value("crypto-market-review.service", "Result")
    if review_result and review_result not in {"success", ""}:
        alerts.append({"level": "bad", "title": "每日复盘服务失败", "body": f"crypto-market-review.service Result={review_result}"})

    counterfactual_result = systemctl_value("crypto-counterfactual-open-skips.service", "Result")
    if counterfactual_result and counterfactual_result not in {"success", ""}:
        alerts.append({"level": "bad", "title": "OPEN_SKIPPED 反事实评估失败", "body": f"crypto-counterfactual-open-skips.service Result={counterfactual_result}"})

    evolution_result = systemctl_value("crypto-strategy-evolution-gate.service", "Result")
    if evolution_result and evolution_result not in {"success", ""}:
        alerts.append({"level": "bad", "title": "策略进化门禁失败", "body": f"crypto-strategy-evolution-gate.service Result={evolution_result}"})
    if STRATEGY_EVOLUTION_LATEST.exists():
        try:
            payload = json.loads(STRATEGY_EVOLUTION_LATEST.read_text(encoding="utf-8", errors="replace"))
            generated_at = parse_dt(payload.get("generated_at"))
            if not generated_at or (now - generated_at).total_seconds() > 4 * 3600:
                alerts.append({"level": "warn", "title": "策略进化门禁偏旧", "body": f"最新门禁 {generated_at or '无'}"})
        except Exception as exc:
            alerts.append({"level": "warn", "title": "策略进化门禁读取失败", "body": str(exc)})
    else:
        alerts.append({"level": "warn", "title": "策略进化门禁缺失", "body": str(STRATEGY_EVOLUTION_LATEST)})

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

    if ATTENTION_LATEST.exists():
        try:
            attention_payload = json.loads(ATTENTION_LATEST.read_text(encoding="utf-8", errors="replace"))
            generated_at = parse_dt(attention_payload.get("generated_at"))
            if not generated_at or (now - generated_at).total_seconds() > 10 * 60:
                alerts.append({"level": "warn", "title": "持久关注台账偏旧", "body": f"最新台账 {generated_at or '无'}"})
        except Exception as exc:
            alerts.append({"level": "warn", "title": "持久关注台账读取失败", "body": str(exc)})
    else:
        alerts.append({"level": "warn", "title": "持久关注台账缺失", "body": str(ATTENTION_LATEST)})

    freshness_inputs = [p for p in (MARKET_CACHE, ACCOUNT_LATEST, PORTAL_REFRESH_STATUS, STRATEGY_EVOLUTION_LATEST, ATTENTION_LATEST) if p.exists()]
    if PORTAL_HTML.exists() and freshness_inputs:
        portal_mtime = PORTAL_HTML.stat().st_mtime
        newest_input = max(p.stat().st_mtime for p in freshness_inputs)
        if newest_input - portal_mtime > 120:
            alerts.append({"level": "bad", "title": "总入口页面偏旧", "body": f"入口页落后最新数据 {newest_input - portal_mtime:.0f} 秒"})
    elif not PORTAL_HTML.exists():
        alerts.append({"level": "bad", "title": "总入口页面缺失", "body": str(PORTAL_HTML)})

    if PORTAL_REFRESH_STATUS.exists():
        try:
            status = json.loads(PORTAL_REFRESH_STATUS.read_text(encoding="utf-8", errors="replace"))
            if status.get("status") != "ok":
                alerts.append({"level": "bad", "title": "入口页刷新失败", "body": str(status.get("error") or status.get("stderr_tail") or "")[:220]})
        except Exception as exc:
            alerts.append({"level": "warn", "title": "入口刷新状态读取失败", "body": str(exc)})

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
            if not snapshot_ts or (now - snapshot_ts).total_seconds() > 120:
                alerts.append({"level": "warn", "title": "账户快照偏旧", "body": f"最新快照 {snapshot_ts or '无'}"})
            if total_snapshots == 0:
                alerts.append({"level": "warn", "title": "账户快照未入库", "body": "account_snapshots 表暂无记录"})
        except Exception as exc:
            alerts.append({"level": "bad", "title": "SQLite 健康检查失败", "body": str(exc)})
    else:
        alerts.append({"level": "bad", "title": "SQLite 事件库缺失", "body": str(EVENT_STORE_DB)})

    return {
        "ts": now.isoformat(),
        "status": "ok" if not alerts else "bad" if any(a["level"] == "bad" for a in alerts) else "warn",
        "alert_count": len(alerts),
        "alerts": alerts,
        "services": states,
        "timers": timers,
        "disk": disk_payload,
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
