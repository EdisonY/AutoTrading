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


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
    return bool(row)


def first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


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
        "top_strategy_reasons": [],
        "recent_open_failed": 0,
    }
    if not db_path or not db_path.exists():
        return out
    since = (datetime.now(CST) - timedelta(hours=hours)).isoformat()
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if not table_exists(conn, "events"):
                return out
            out["available"] = True
            rows = conn.execute(
                """
                select strategy, reason, stage, layer, payload_json
                from events
                where event_type='OPEN_SKIPPED' and ts >= ?
                """,
                (since,),
            ).fetchall()
            out["total"] = len(rows)
            by_strategy: dict[str, int] = defaultdict(int)
            reasons: Counter[str] = Counter()
            strategy_reasons: Counter[tuple[str, str]] = Counter()
            for row in rows:
                strategy = str(row["strategy"] or "unknown")
                reason = str(row["reason"] or row["layer"] or row["stage"] or "unknown")
                by_strategy[strategy] += 1
                reasons[reason] += 1
                strategy_reasons[(strategy, reason)] += 1
            out["by_strategy"] = dict(sorted(by_strategy.items()))
            out["top_reasons"] = [{"reason": reason, "count": count} for reason, count in reasons.most_common(12)]
            out["top_strategy_reasons"] = [
                {"strategy": strategy, "reason": reason, "count": count}
                for (strategy, reason), count in strategy_reasons.most_common(12)
            ]
            out["recent_open_failed"] = int(conn.execute(
                "select count(*) from events where event_type='OPEN_FAILED' and ts >= ?", (since,)
            ).fetchone()[0])
    except Exception as exc:
        out["error"] = str(exc)
    return out


def top100_review(runtime_dir: Path, mirror_runtime: Path) -> dict[str, Any]:
    cache = read_json(first_existing(runtime_dir / "market_data_cache.json", mirror_runtime / "market_data_cache.json") or Path(""))
    generated_at = parse_dt(cache.get("generated_at") or cache.get("updated_at") or cache.get("ts"))
    symbols = cache.get("symbols")
    if not isinstance(symbols, list):
        symbols = cache.get("top_symbols") if isinstance(cache.get("top_symbols"), list) else []
    count = len(symbols)
    return {
        "target": "Binance trading-volume Top100",
        "configured": {
            "A/v11": "top 100 + spike 100 + sentinel 40",
            "B/v16": "top 100 + sentinel 40",
            "C/v14": "top 100 + sentinel 40",
        },
        "cache_symbols": count,
        "cache_age": age_text(generated_at),
        "coverage_hint": "ok" if count >= 100 else "cache_gap",
        "note": "市值Top100需要外部数据源；当前不新增联网源，先用交易量Top100。",
    }


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


def build_payload(root: Path = ROOT, hours: int = 24) -> dict[str, Any]:
    runtime_dir = root / "runtime"
    reports_dir = root / "reports"
    mirror_runtime = root / "server_logs_tencent" / "runtime"
    event_db = first_existing(mirror_runtime / "event_store.sqlite3", runtime_dir / "event_store.sqlite3")
    queue = queue_review(runtime_dir, mirror_runtime)
    skipped = open_skipped_review(event_db, hours)
    gaps = research_gap_review(runtime_dir, mirror_runtime)
    status = "blocked_by_cooldown" if queue.get("active_cooldowns") else "safe_to_optimize_offline"
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
        "top100": top100_review(runtime_dir, mirror_runtime),
        "open_skipped": skipped,
        "research_gaps": gaps,
        "reports": report_review(runtime_dir, reports_dir),
        "actions": actions,
        "summary": {
            "active_requests": int(queue.get("active_requests") or 0),
            "active_cooldowns": int(queue.get("active_cooldowns") or 0),
            "recent_bad": int(queue.get("recent_bad") or 0),
            "open_skipped": int(skipped.get("total") or 0),
            "open_failed": int(skipped.get("recent_open_failed") or 0),
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
    skipped = payload.get("open_skipped") or {}
    gaps = payload.get("research_gaps") or {}
    lines = [
        "# Waiting Period Optimization",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Safety: `{payload.get('safety')}`",
        f"- Window: last `{payload.get('hours')}` hours",
        "",
        "## API / Binance pressure",
        "",
        f"- Active queue requests: `{summary.get('active_requests')}`",
        f"- Active cooldowns: `{summary.get('active_cooldowns')}`",
        f"- Recent bad rows: `{summary.get('recent_bad')}`",
        "",
        "## Top100 coverage",
        "",
        f"- Target: {top100.get('target')}",
        f"- Cache symbols: `{top100.get('cache_symbols')}` ({top100.get('cache_age')})",
        f"- Note: {top100.get('note')}",
        "",
        "## No-open reasons",
        "",
        f"- OPEN_SKIPPED: `{skipped.get('total')}`",
        f"- OPEN_FAILED: `{skipped.get('recent_open_failed')}`",
        "",
    ]
    rows = [["Reason", "Count"]] + [[r.get("reason"), r.get("count")] for r in skipped.get("top_reasons", [])]
    lines.extend(md_table(rows) or ["No reason rows."])
    lines.extend([
        "",
        "## Kline / depth gap plan",
        "",
        f"- Kline status: `{gaps.get('kline_status')}`",
        f"- Planned Kline requests: `{gaps.get('planned_kline_requests')}`",
        f"- Planned depth requests: `{gaps.get('planned_depth_requests')}`",
        "- Submit: `disabled`",
        "",
        "## Waiting actions",
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
