"""Generate a command-center style entry page for AutoTrading reports."""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from collections import Counter
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
REVIEW_DIR = ROOT / "复盘报告"
MEMORY_DIR = ROOT / "research_memory"
EXPERIMENTS_DIR = ROOT / "experiments"
SERVER_MIRROR_DIR = ROOT / "server_logs_tencent"
LOCAL_EVENT_STORE_DB = ROOT / "runtime" / "event_store.sqlite3"
MIRROR_EVENT_STORE_DB = SERVER_MIRROR_DIR / "runtime" / "event_store.sqlite3"
EVENT_STORE_DB = MIRROR_EVENT_STORE_DB if MIRROR_EVENT_STORE_DB.exists() else LOCAL_EVENT_STORE_DB
MARKET_CACHE_PATH = ROOT / "runtime" / "market_data_cache.json"
ALERTS_PATH = ROOT / "runtime" / "alerts_latest.json"
ACCOUNT_SNAPSHOT_PATH = ROOT / "runtime" / "account_snapshot_latest.json"
COUNTERFACTUAL_JSON = REPORTS_DIR / "counterfactual_open_skips_latest.json"
COUNTERFACTUAL_HTML = REPORTS_DIR / "counterfactual_open_skips_latest.html"
STRATEGY_EVOLUTION_JSON = ROOT / "runtime" / "strategy_evolution_latest.json"
STRATEGY_EVOLUTION_HTML = REPORTS_DIR / "strategy_evolution_latest.html"
ATTENTION_JSON = ROOT / "research_memory" / "attention" / "open_items.json"
ATTENTION_HTML = REPORTS_DIR / "decision_attention_latest.html"
STRATEGY_TRUTH_JSON = ROOT / "runtime" / "strategy_truth_latest.json"
STRATEGY_TRUTH_MD = REPORTS_DIR / "strategy_truth_latest.md"
RESEARCH_STORE_JSON = ROOT / "runtime" / "research_store_summary_latest.json"
RESEARCH_STORE_MD = REPORTS_DIR / "research_store_summary_latest.md"
REPLAY_FEATURE_JSON = ROOT / "runtime" / "replay_feature_dataset_latest.json"
REPLAY_FEATURE_MD = REPORTS_DIR / "replay_feature_dataset_latest.md"
REPLAY_GATE_JSON = ROOT / "runtime" / "replay_gate_audit_latest.json"
REPLAY_GATE_MD = REPORTS_DIR / "replay_gate_audit_latest.md"
REPLAY_PARITY_JSON = ROOT / "runtime" / "replay_live_parity_latest.json"
REPLAY_PARITY_MD = REPORTS_DIR / "replay_live_parity_latest.md"
ROLLBACK_WATCH_JSON = ROOT / "runtime" / "rollback_watch_review_latest.json"
ROLLBACK_WATCH_MD = REPORTS_DIR / "rollback_watch_review_latest.md"
A_V11_ROLLOUT_JSON = ROOT / "runtime" / "a_v11_rollout_review_latest.json"
A_V11_ROLLOUT_MD = REPORTS_DIR / "a_v11_rollout_review_latest.md"
B_V16_ROLLOUT_JSON = ROOT / "runtime" / "b_v16_rollout_review_latest.json"
B_V16_ROLLOUT_MD = REPORTS_DIR / "b_v16_rollout_review_latest.md"
SENTINEL_QUALITY_JSON = ROOT / "runtime" / "sentinel_quality_latest.json"
SENTINEL_QUALITY_MD = REPORTS_DIR / "sentinel_quality_latest.md"
CST = timezone(timedelta(hours=8))


def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def read_jsonl_tail(path: Path, limit: int = 500) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def newest(*paths: Path) -> Path | None:
    existing = [p for p in paths if p.exists()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


def latest_file(folder: Path, pattern: str) -> Path | None:
    if not folder.exists():
        return None
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def as_url(path: Path | None) -> str:
    if not path or not path.exists():
        return "#"
    try:
        return path.resolve().as_uri()
    except Exception:
        return "#"


def file_time(path: Path | None) -> str:
    if not path or not path.exists():
        return "未生成"
    return datetime.fromtimestamp(path.stat().st_mtime, CST).strftime("%Y-%m-%d %H:%M")


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt).replace(tzinfo=CST)
        except Exception:
            continue
    return None


def age_text(dt: datetime | None) -> str:
    if not dt:
        return "无记录"
    seconds = max(0, int((datetime.now(CST) - dt).total_seconds()))
    if seconds < 90:
        return f"{seconds} 秒前"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    return f"{hours} 小时前"


def strategy_status() -> list[dict[str, Any]]:
    specs = [
        ("A/v11", SERVER_MIRROR_DIR / "logs" / "system.jsonl", "heartbeats", 120, "15m + 30m", "心跳按若干轮写一次，不代表每轮都上报"),
        ("B/v16", SERVER_MIRROR_DIR / "logs_v16" / "system.jsonl", "SCAN_STATS", 120, "1h 入场，15m 确认", "SCAN_STATS 是每轮扫描结束后写的统计"),
        ("C/v14", SERVER_MIRROR_DIR / "logs_v14" / "system.jsonl", "heartbeat", 120, "1h 入场，15m 确认", "心跳按若干轮写一次，不代表每轮都上报"),
    ]
    out: list[dict[str, Any]] = []
    for name, path, marker, scan_interval_sec, period, cadence_note in specs:
        rows = read_jsonl(path)
        if marker == "SCAN_STATS":
            rows = [r for r in rows if r.get("event") == "SCAN_STATS"]
        elif marker == "heartbeats":
            rows = [r for r in rows if r.get("type") == "HEARTBEAT"]
        times = [parse_dt(r.get("ts") or r.get("time")) for r in rows]
        times = [t for t in times if t]
        latest = times[-1] if times else None
        fresh = latest is not None and (datetime.now(CST) - latest).total_seconds() < 90 * 60
        opened = 0
        signals = None
        if name == "B/v16":
            if rows:
                opened = int(rows[-1].get("opened") or 0)
                signals = sum(int(rows[-1].get(k) or 0) for k in ("no_signal", "score_low", "confirm_fail", "threshold_fail"))
        elif rows:
            opened = 0
            signals = rows[-1].get("signals_found")
        out.append(
            {
                "name": name,
                "ok": fresh,
                "latest": latest,
                "age": age_text(latest),
                "interval": f"{scan_interval_sec} 秒",
                "period": period,
                "cadence_note": cadence_note,
                "opened": opened,
                "signals": signals,
            }
        )
    return out


def parse_number(text: Any) -> float:
    raw = str(text if text is not None else "").strip().replace("%", "").replace("+", "")
    try:
        return float(raw)
    except Exception:
        return 0.0


def table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_signal_quality(path: Path | None) -> dict[str, Any]:
    empty = {
        "path": path,
        "date": "-",
        "strategies": [],
        "sentinel": [],
        "total_pnl": 0.0,
        "total_opens": 0,
        "total_closes": 0,
        "http400": 0,
    }
    if not path or not path.exists():
        return empty
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    out = dict(empty)
    out["http400"] = text.count("HTTP Error 400")
    if lines and lines[0].startswith("#"):
        out["date"] = lines[0].replace("# 三策略信号质量基线 -", "").strip()

    in_overview = False
    in_sentinel = False
    for line in lines:
        if line.startswith("## 一、总览"):
            in_overview = True
            in_sentinel = False
            continue
        if line.startswith("## 三、哨兵扫描追踪"):
            in_overview = False
            in_sentinel = True
            continue
        if line.startswith("###") and in_sentinel:
            in_sentinel = False
            continue
        if line.startswith("## ") and not line.startswith("## 三、哨兵扫描追踪"):
            in_overview = False
            in_sentinel = False
        if not line.startswith("|") or "---" in line:
            continue
        cells = table_cells(line)
        if in_overview and cells and cells[0] not in ("策略", "") and len(cells) >= 9:
            row = {
                "name": cells[0],
                "signals": int(parse_number(cells[1])),
                "opens": int(parse_number(cells[2])),
                "skips": int(parse_number(cells[3])),
                "fails": int(parse_number(cells[4])),
                "closes": int(parse_number(cells[5])),
                "win_rate": cells[6],
                "pnl": parse_number(cells[7]),
                "attributed_pnl": parse_number(cells[8]),
                "hard_rate": cells[9] if len(cells) > 9 else "-",
            }
            out["strategies"].append(row)
        elif in_sentinel and cells and cells[0] not in ("策略", "") and len(cells) >= 5:
            out["sentinel"].append(
                {
                    "name": cells[0],
                    "decisions": int(parse_number(cells[1])),
                    "results": cells[2],
                    "layers": cells[3],
                    "symbols": cells[4],
                }
            )
    out["total_pnl"] = sum(float(r.get("pnl") or 0) for r in out["strategies"])
    out["total_opens"] = sum(int(r.get("opens") or 0) for r in out["strategies"])
    out["total_closes"] = sum(int(r.get("closes") or 0) for r in out["strategies"])
    return out


def counterfactual_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": COUNTERFACTUAL_HTML,
        "age": "无报告",
        "fresh": False,
        "hours": 0,
        "horizon": 60,
        "overall": {},
        "replay_fill": {},
        "fill_liquidity": {},
        "strategies": [],
        "filters": [],
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    strategy_rows = []
    for name in ("A/v11", "B/v16", "C/v14"):
        metrics = (payload.get("strategies") or {}).get(name) or {}
        strategy_rows.append({"name": name, **metrics})
    return {
        "available": True,
        "path": COUNTERFACTUAL_HTML,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "hours": int(payload.get("hours") or 0),
        "horizon": int(payload.get("primary_horizon_minutes") or 60),
        "overall": payload.get("overall") or {},
        "replay_fill": ((payload.get("overall") or {}).get("replay_fill") or {}),
        "fill_liquidity": payload.get("fill_liquidity") or {},
        "strategies": strategy_rows,
        "filters": payload.get("filters") or [],
    }


def strategy_evolution_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": STRATEGY_EVOLUTION_HTML,
        "age": "无门禁",
        "fresh": False,
        "counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0, "REJECT": 0},
        "summary": {},
        "regime_summary": {},
        "expansion_readiness": {},
        "promotion_gate_hardening": {},
        "top": {},
        "decisions": [],
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    decisions = payload.get("decisions") if isinstance(payload.get("decisions"), list) else []
    top = next((d for d in decisions if d.get("priority") in {"P0", "P1", "P2"}), decisions[0] if decisions else {})
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    counts = (summary.get("counts") if isinstance(summary.get("counts"), dict) else {}) or empty["counts"]
    regime_counts: dict[str, int] = {}
    live_window_count = 0
    live_open_failed = 0
    live_close_failed = 0
    quality_counts: dict[str, int] = {}
    for decision in decisions:
        if not isinstance(decision, dict) or not decision.get("approved_full_live"):
            continue
        window = (((decision.get("post_approval_live") or {}).get("windows") or {}).get("24h") or {})
        if not window:
            continue
        live_window_count += 1
        live_open_failed += int(window.get("open_failed") or 0)
        live_close_failed += int(window.get("close_failed") or 0)
        regime = ((window.get("regime") or {}).get("label") or "unknown")
        regime_counts[str(regime)] = regime_counts.get(str(regime), 0) + 1
        quality = ((window.get("quality") or {}).get("label") or "unknown")
        quality_counts[str(quality)] = quality_counts.get(str(quality), 0) + 1
    return {
        "available": True,
        "path": STRATEGY_EVOLUTION_HTML,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "counts": {k: int(counts.get(k) or 0) for k in ("P0", "P1", "P2", "P3", "REJECT")},
        "summary": summary,
        "regime_summary": {
            "window_count": live_window_count,
            "counts": regime_counts,
            "quality_counts": quality_counts,
            "open_failed_24h": live_open_failed,
            "close_failed_24h": live_close_failed,
        },
        "expansion_readiness": summary.get("expansion_readiness") if isinstance(summary.get("expansion_readiness"), dict) else {},
        "promotion_gate_hardening": summary.get("promotion_gate_hardening") if isinstance(summary.get("promotion_gate_hardening"), dict) else {},
        "top": top or {},
        "decisions": decisions,
    }


def evolution_action_alert(evolution: dict[str, Any]) -> dict[str, str]:
    decisions = [d for d in (evolution.get("decisions") or []) if isinstance(d, dict)]
    priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "REJECT": 9}

    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        priority = priority_rank.get(str(item.get("priority") or "P3"), 8)
        evidence = -int(item.get("evidence_score") or 0)
        return priority, evidence

    rollback = sorted(
        [d for d in decisions if str(d.get("status") or "") in {"rollback_required", "rollback_watch"}],
        key=sort_key,
    )
    if rollback:
        top = rollback[0]
        level = "bad" if top.get("status") == "rollback_required" or top.get("priority") == "P0" else "warn"
        reasons = "; ".join(str(x) for x in (top.get("blockers") or [])[:2]) or "未给出阻塞原因"
        return {
            "level": level,
            "title": f"{top.get('priority') or 'P1'} 已放开候选劣化：{top.get('strategy') or '-'}",
            "body": (
                f"{top.get('candidate_id') or '-'} 进入 {top.get('status') or '-'}；"
                f"原因 {reasons}；建议 {top.get('recommended_action') or '-'}。"
            ),
        }

    ready = sorted(
        [
            d for d in decisions
            if str(d.get("status") or "") == "verified_upgrade_ready"
            and str(d.get("priority") or "") in {"P0", "P1"}
        ],
        key=sort_key,
    )
    if ready:
        top = ready[0]
        return {
            "level": "warn",
            "title": f"{top.get('priority') or 'P1'} 已验证更优方案：{top.get('strategy') or '-'}",
            "body": (
                f"{top.get('candidate_id') or '-'}；证据分 {int(top.get('evidence_score') or 0)}，"
                f"风险分 {int(top.get('risk_score') or 0)}；建议 {top.get('recommended_action') or '-'}。"
            ),
        }

    approved = sorted(
        [d for d in decisions if d.get("approved_full_live")],
        key=sort_key,
    )
    if approved:
        watched = sum(1 for d in approved if str(d.get("status") or "") == "full_live_monitoring")
        regime_summary = evolution.get("regime_summary") or {}
        return {
            "level": "ok",
            "title": f"P2 已放开候选观察中：{watched}/{len(approved)} 正常",
            "body": (
                f"24h quality {regime_summary.get('quality_counts') or {}}，"
                f"OPEN_FAILED {int(regime_summary.get('open_failed_24h') or 0)}，"
                f"CLOSE_FAILED {int(regime_summary.get('close_failed_24h') or 0)}。"
            ),
        }

    return {
        "level": "ok" if evolution.get("fresh") else "warn",
        "title": "P2 暂无已验证更优方案",
        "body": f"门禁更新 {evolution.get('age') or '未知'}；无 P0/P1 可批准项或回滚项。",
    }


def rollback_watch_brief(evolution: dict[str, Any]) -> str:
    decisions = [d for d in (evolution.get("decisions") or []) if isinstance(d, dict)]
    watched = [
        d for d in decisions
        if str(d.get("status") or "") in {"rollback_required", "rollback_watch"}
    ]
    if not watched:
        return ""
    parts: list[str] = []
    for d in watched[:4]:
        blockers = "; ".join(str(x) for x in (d.get("blockers") or [])[:2]) or "-"
        window = (((d.get("post_approval_live") or {}).get("windows") or {}).get("24h") or {})
        quality = window.get("quality") or {}
        failed_reasons = ", ".join(
            f"{row.get('reason')}×{int(row.get('count') or 0)}"
            for row in (window.get("open_failed_reasons") or [])[:3]
            if isinstance(row, dict)
        )
        failed_note = f"，失败原因 {failed_reasons}" if failed_reasons else ""
        close_reasons = ", ".join(
            f"{row.get('reason')}x{int(row.get('count') or 0)}"
            for row in (window.get("close_failed_reasons") or [])[:3]
            if isinstance(row, dict)
        )
        close_failed_note = f" close_failed_reasons {close_reasons}" if close_reasons else ""
        metrics = (
            f"24h开仓 {int(window.get('opens') or 0)}，"
            f"平仓 {int((quality.get('closed_samples') or window.get('closes') or 0))}，"
            f"OPEN_FAILED {int(window.get('open_failed') or 0)}，"
            f"CLOSE_FAILED {int(window.get('close_failed') or 0)}; "
            f"强平 {int(window.get('forced_closes') or 0)}，"
            f"扣费后PnL {float(quality.get('realized_pnl_after_cost') or 0):+.2f}"
            f"{failed_note}"
            f"{close_failed_note}"
        )
        parts.append(f"{d.get('strategy') or '-'} {d.get('candidate_id') or '-'}：{blockers}；{metrics}")
    return "回滚观察明细：" + " | ".join(parts)


def attention_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": ATTENTION_HTML,
        "age": "无台账",
        "fresh": False,
        "summary": {"total_visible": 0, "open": 0, "cleared_pending_review": 0, "counts": {}},
        "items": [],
        "top": {},
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    visible = [
        item for item in (payload.get("items") or [])
        if isinstance(item, dict) and item.get("status") in {"open", "cleared_pending_review", "acknowledged"}
    ]
    priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    visible.sort(key=lambda item: (priority_rank.get(str(item.get("priority") or "P3"), 9), str(item.get("title") or "")))
    open_visible = [item for item in visible if item.get("status") == "open"]
    return {
        "available": True,
        "path": ATTENTION_HTML,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 10 * 60),
        "summary": payload.get("summary") or empty["summary"],
        "items": visible,
        "top": open_visible[0] if open_visible else {},
    }


def strategy_truth_summary(path: Path | None) -> dict[str, Any]:
    """Read strategy truth ledger and extract key metrics."""
    empty = {
        "available": False,
        "path": STRATEGY_TRUTH_MD,
        "age": "无台账",
        "fresh": False,
        "summary": {},
        "strategy_stats": {},
        "recovery_stats": {},
        "recovery_review": {},
        "recovery_strategy_exit_evidence": {},
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    return {
        "available": True,
        "path": STRATEGY_TRUTH_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "summary": payload.get("summary") or {},
        "strategy_stats": payload.get("strategy_stats") or {},
        "recovery_stats": payload.get("recovery_stats") or {},
        "recovery_review": payload.get("recovery_review") or {},
        "recovery_strategy_exit_evidence": payload.get("recovery_strategy_exit_evidence") or {},
    }


def research_store_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": RESEARCH_STORE_MD,
        "age": "无研究仓",
        "fresh": False,
        "days": 0,
        "tables": [],
        "strategy_funnel": [],
        "skip_layers": [],
        "sentinel": [],
        "latest_accounts": [],
        "kline_coverage": [],
        "feature_coverage": [],
        "totals": {"events": 0, "signals": 0, "opens": 0, "skipped": 0, "failed": 0},
        "top_skip": {},
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    funnel = payload.get("strategy_funnel") if isinstance(payload.get("strategy_funnel"), list) else []
    skip_layers = payload.get("skip_layers") if isinstance(payload.get("skip_layers"), list) else []
    tables = payload.get("available_tables") if isinstance(payload.get("available_tables"), list) else []
    totals = {
        "events": sum(int(r.get("events") or 0) for r in funnel if isinstance(r, dict)),
        "signals": sum(int(r.get("signals") or 0) for r in funnel if isinstance(r, dict)),
        "opens": sum(int(r.get("opens") or 0) for r in funnel if isinstance(r, dict)),
        "skipped": sum(int(r.get("open_skipped") or 0) for r in funnel if isinstance(r, dict)),
        "failed": sum(int(r.get("open_failed") or 0) for r in funnel if isinstance(r, dict)),
    }
    return {
        "available": True,
        "path": RESEARCH_STORE_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "days": int(payload.get("days") or 0),
        "tables": tables,
        "strategy_funnel": funnel,
        "skip_layers": skip_layers,
        "sentinel": payload.get("sentinel") if isinstance(payload.get("sentinel"), list) else [],
        "latest_accounts": payload.get("latest_accounts") if isinstance(payload.get("latest_accounts"), list) else [],
        "kline_coverage": payload.get("kline_coverage") if isinstance(payload.get("kline_coverage"), list) else [],
        "feature_coverage": payload.get("feature_coverage") if isinstance(payload.get("feature_coverage"), list) else [],
        "totals": totals,
        "top_skip": skip_layers[0] if skip_layers else {},
    }


def replay_feature_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": REPLAY_FEATURE_MD,
        "age": "No replay dataset",
        "fresh": False,
        "summary": {"events": 0, "matched_features": 0, "match_rate": 0},
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "available": True,
        "path": REPLAY_FEATURE_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "summary": summary,
    }


def replay_gate_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": REPLAY_GATE_MD,
        "age": "No replay gate audit",
        "fresh": False,
        "days": 0,
        "summary": {"open_flow_events": 0, "gate_coverage_pct": 0, "unknown_gate": 0, "status": "missing"},
        "strategies": [],
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    strategies = payload.get("strategies") if isinstance(payload.get("strategies"), list) else []
    return {
        "available": True,
        "path": REPLAY_GATE_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "days": int(payload.get("days") or 0),
        "summary": summary,
        "strategies": strategies,
    }


def replay_parity_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": REPLAY_PARITY_MD,
        "age": "No replay/live parity audit",
        "fresh": False,
        "days": 0,
        "summary": {
            "open_flow_rows": 0,
            "gate_cases": 0,
            "exact_case_coverage_pct": 0,
            "observed_gate_coverage_pct": 0,
            "pass_rate_pct": 0,
            "mismatched": 0,
            "errors": 0,
            "status": "missing",
        },
        "strategies": [],
        "mismatch_examples": [],
        "error_examples": [],
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "available": True,
        "path": REPLAY_PARITY_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "days": int(payload.get("days") or 0),
        "summary": summary,
        "strategies": payload.get("strategies") if isinstance(payload.get("strategies"), list) else [],
        "mismatch_examples": payload.get("mismatch_examples") if isinstance(payload.get("mismatch_examples"), list) else [],
        "error_examples": payload.get("error_examples") if isinstance(payload.get("error_examples"), list) else [],
    }


def a_v11_rollout_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": A_V11_ROLLOUT_MD,
        "age": "No A/v11 rollout review",
        "fresh": False,
        "decision": {"priority": "P2", "status": "missing", "recommended_actions": []},
        "windows": {},
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    return {
        "available": True,
        "path": A_V11_ROLLOUT_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "decision": payload.get("decision") if isinstance(payload.get("decision"), dict) else empty["decision"],
        "windows": payload.get("windows") if isinstance(payload.get("windows"), dict) else {},
        "selected_live_parameter": payload.get("selected_live_parameter") or {},
    }


def b_v16_rollout_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": B_V16_ROLLOUT_MD,
        "age": "No B/v16 rollout review",
        "fresh": False,
        "decision": {"priority": "P2", "status": "missing", "recommended_actions": []},
        "windows": {},
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    return {
        "available": True,
        "path": B_V16_ROLLOUT_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "decision": payload.get("decision") if isinstance(payload.get("decision"), dict) else empty["decision"],
        "windows": payload.get("windows") if isinstance(payload.get("windows"), dict) else {},
        "selected_live_parameter": payload.get("selected_live_parameter") or {},
    }


def rollback_watch_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": ROLLBACK_WATCH_MD,
        "age": "No rollback-watch review",
        "fresh": False,
        "summary": {"items": 0, "p0": 0, "p1": 0, "actions": {}},
        "items": [],
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    return {
        "available": True,
        "path": ROLLBACK_WATCH_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else empty["summary"],
        "items": payload.get("items") if isinstance(payload.get("items"), list) else [],
    }


def sentinel_quality_summary(path: Path | None) -> dict[str, Any]:
    empty = {
        "available": False,
        "path": SENTINEL_QUALITY_MD,
        "age": "No sentinel quality review",
        "fresh": False,
        "summary": {},
        "coverage": {},
        "forward_returns": {},
        "watchlist_history": {},
    }
    payload = read_json(path) if path else None
    if not isinstance(payload, dict):
        return empty
    generated_at = parse_dt(payload.get("generated_at"))
    return {
        "available": True,
        "path": SENTINEL_QUALITY_MD,
        "age": age_text(generated_at),
        "fresh": bool(generated_at and (datetime.now(CST) - generated_at).total_seconds() < 4 * 3600),
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        "coverage": payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {},
        "forward_returns": payload.get("forward_returns") if isinstance(payload.get("forward_returns"), dict) else {},
        "watchlist_history": payload.get("watchlist_history") if isinstance(payload.get("watchlist_history"), dict) else {},
    }


def compact_text(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def research_overview(reviews: list[dict[str, Any]], experiments: list[dict[str, Any]]) -> dict[str, Any]:
    approved = [r for r in reviews if r.get("promotion_status") == "approved_candidate"]
    small_live = [r for r in reviews if r.get("promotion_status") == "approved_for_small_live"]
    observe = [r for r in experiments if str(r.get("status") or r.get("state") or "").lower() in {"observe", "running"}]
    manual = [r for r in reviews if "manual" in str(r.get("promotion_status") or r.get("status") or "").lower()]
    top = approved[0] if approved else (reviews[0] if reviews else {})
    return {
        "approved": len(approved),
        "small_live": len(small_live),
        "observe": len(observe),
        "manual": len(manual),
        "top": top.get("candidate_id") or top.get("hypothesis_id") or top.get("experiment_id") or top.get("id") or "-",
        "top_status": top.get("promotion_status") or top.get("status") or "-",
    }


DATA_ROOT = ROOT if (ROOT / "scanner_data").exists() else SERVER_MIRROR_DIR
STRATEGY_FILES = [
    ("A/v11", DATA_ROOT / "scanner_data" / "trades.jsonl", DATA_ROOT / "logs" / "signals.jsonl", DATA_ROOT / "logs" / "decisions.jsonl", DATA_ROOT / "logs" / "system.jsonl"),
    ("B/v16", DATA_ROOT / "scanner_data_v16" / "trades.jsonl", DATA_ROOT / "logs_v16" / "signals.jsonl", DATA_ROOT / "logs_v16" / "decisions.jsonl", DATA_ROOT / "logs_v16" / "system.jsonl"),
    ("C/v14", DATA_ROOT / "scanner_data_v14" / "trades.jsonl", DATA_ROOT / "logs_v14" / "signals.jsonl", DATA_ROOT / "logs_v14" / "decisions.jsonl", DATA_ROOT / "logs_v14" / "system.jsonl"),
]


def row_time(row: dict[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        dt = parse_dt(row.get(key))
        if dt:
            return dt
    return None


def since_start() -> datetime:
    today = datetime.now(CST).date()
    return datetime.combine(today - timedelta(days=1), datetime.min.time(), tzinfo=CST)


def event_store_shadow_summary(decision_summary: list[dict[str, Any]]) -> dict[str, Any]:
    empty = {
        "available": False,
        "db_path": EVENT_STORE_DB,
        "total_events": 0,
        "baseline_runs": 0,
        "latest_id": 0,
        "latest_ts": None,
        "latest_age": "无记录",
        "sources": [],
        "strategy_rows": [],
        "coverage_ok": False,
        "note": "事件库不存在，入口页继续使用 JSONL。",
    }
    if not EVENT_STORE_DB.exists():
        return empty
    try:
        conn = sqlite3.connect(EVENT_STORE_DB)
        conn.row_factory = sqlite3.Row
        total = int(conn.execute("select count(*) from events").fetchone()[0])
        baseline = int(conn.execute("select count(*) from baseline_runs").fetchone()[0])
        latest = conn.execute("select id, ts, source, strategy, event_type, category from events order by id desc limit 1").fetchone()
        latest_candidates = conn.execute("select id, ts from events order by id desc limit 100").fetchall()
        source_rows = conn.execute(
            """
            select source, strategy, event_type, category, count(*) as n, max(id) as latest_id
            from events
            group by source, strategy, event_type, category
            order by latest_id desc
            limit 18
            """
        ).fetchall()
        strategy_rows = conn.execute(
            """
            select
              case
                when source like 'A/v11/%' then 'A/v11'
                when source like 'B/v16/%' then 'B/v16'
                when source like 'C/v14/%' then 'C/v14'
                else coalesce(nullif(strategy, ''), 'unknown')
              end as strategy_name,
              sum(case when source like '%/events' then 1 else 0 end) as events_n,
              sum(case when source like '%/decisions' then 1 else 0 end) as decisions_n,
              sum(case when source like '%/signals' then 1 else 0 end) as signals_n,
              sum(case when source like '%/system' then 1 else 0 end) as system_n,
              max(id) as latest_id
            from events
            group by strategy_name
            order by latest_id desc
            """
        ).fetchall()
    except Exception as exc:
        out = dict(empty)
        out["available"] = False
        out["note"] = f"事件库读取失败：{exc}"
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass

    latest_ts = None
    for candidate in latest_candidates:
        latest_ts = parse_dt(candidate["ts"])
        if latest_ts:
            break
    expected = {"A/v11", "B/v16", "C/v14"}
    rows = []
    for row in strategy_rows:
        name = row["strategy_name"]
        if name not in expected:
            continue
        jsonl = next((r for r in decision_summary if r.get("name") == name), {})
        rows.append(
            {
                "name": name,
                "events": int(row["events_n"] or 0),
                "decisions": int(row["decisions_n"] or 0),
                "signals": int(row["signals_n"] or 0),
                "system": int(row["system_n"] or 0),
                "jsonl_signals": int(jsonl.get("signals") or 0),
                "jsonl_sentinel": int(jsonl.get("sentinel") or 0),
                "latest_id": int(row["latest_id"] or 0),
            }
        )
    covered = {r["name"] for r in rows if r["events"] or r["decisions"] or r["signals"] or r["system"]}
    coverage_ok = expected.issubset(covered)
    note = "SQLite 双写已覆盖 A/B/C，可进入入口页影子对比。" if coverage_ok else "SQLite 还未覆盖全部策略，入口页继续以 JSONL 为准。"
    return {
        "available": True,
        "db_path": EVENT_STORE_DB,
        "total_events": total,
        "baseline_runs": baseline,
        "latest_id": int(latest["id"] or 0) if latest else 0,
        "latest_ts": latest_ts,
        "latest_age": age_text(latest_ts),
        "sources": [dict(r) for r in source_rows],
        "strategy_rows": rows,
        "coverage_ok": coverage_ok,
        "note": note,
    }


def market_cache_summary() -> dict[str, Any]:
    payload = read_json(MARKET_CACHE_PATH)
    if not isinstance(payload, dict):
        return {"ok": False, "note": "行情缓存未生成"}
    ts = parse_dt(payload.get("ts") or payload.get("updated_at") or payload.get("timestamp"))
    age_seconds = (datetime.now(CST) - ts).total_seconds() if ts else None
    symbols = len(payload.get("available_symbols") or [])
    top = len(payload.get("top_symbols") or [])
    spikes = len(payload.get("spike_symbols") or [])
    ok = bool(ts and age_seconds is not None and age_seconds <= 90 and symbols and top)
    return {
        "ok": ok,
        "ts": ts,
        "age_seconds": age_seconds,
        "age": age_text(ts),
        "symbols": symbols,
        "top": top,
        "spikes": spikes,
        "note": f"{symbols} 个可交易标的，Top成交额 {top} 个，突增候选 {spikes} 个。",
    }


def realtime_account_summary() -> dict[str, Any]:
    empty = {
        "available": False,
        "fresh": False,
        "age": "无快照",
        "accounts": [],
        "wallet_usdt": 0.0,
        "available_usdt": 0.0,
        "margin_usdt": 0.0,
        "unrealized_pnl_usdt": 0.0,
        "open_positions": 0,
        "risk_count": 0,
        "sizing_violation_count": 0,
        "sizing_violations": [],
        "note": "尚未写入实时账户快照。",
    }
    snapshot_candidate: dict[str, Any] | None = None
    snapshot = read_json(ACCOUNT_SNAPSHOT_PATH)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("accounts"), list):
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
        accounts = []
        latest_ts = parse_dt(summary.get("ts"))
        sizing_violations = []
        for row in snapshot.get("accounts") or []:
            if not isinstance(row, dict):
                continue
            ts = parse_dt(row.get("ts"))
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
            account_violations = row.get("sizing_violations") or []
            if isinstance(account_violations, list):
                sizing_violations.extend(account_violations)
            accounts.append(
                {
                    "account": row.get("account", ""),
                    "strategy": row.get("strategy") or row.get("account", ""),
                    "version": row.get("version", ""),
                    "wallet_usdt": float(row.get("wallet_usdt") or 0),
                    "available_usdt": float(row.get("available_usdt") or 0),
                    "margin_usdt": float(row.get("margin_usdt") or 0),
                    "unrealized_pnl_usdt": float(row.get("unrealized_pnl_usdt") or 0),
                    "open_positions": int(row.get("open_positions") or 0),
                    "longs": int(row.get("longs") or 0),
                    "shorts": int(row.get("shorts") or 0),
                    "notional_usdt": float(row.get("notional_usdt") or 0),
                    "risk_count": int(row.get("hard_stop_risk_count") or 0),
                    "sizing_violation_count": int(row.get("sizing_violation_count") or 0),
                    "sizing_violations": account_violations if isinstance(account_violations, list) else [],
                    "worst": row.get("worst_position") or {},
                    "best": row.get("best_position") or {},
                    "ts": ts,
                }
            )
        age_seconds = (datetime.now(CST) - latest_ts).total_seconds() if latest_ts else None
        fresh = bool(latest_ts and age_seconds is not None and age_seconds <= 120)
        wallet = float(summary.get("wallet_usdt") or sum(a["wallet_usdt"] for a in accounts))
        available = float(summary.get("available_usdt") or sum(a["available_usdt"] for a in accounts))
        margin = float(summary.get("margin_usdt") or sum(a["margin_usdt"] for a in accounts))
        upnl = float(summary.get("unrealized_pnl_usdt") or sum(a["unrealized_pnl_usdt"] for a in accounts))
        positions = int(summary.get("open_positions") or sum(a["open_positions"] for a in accounts))
        risks = sum(a["risk_count"] for a in accounts)
        sizing_count = sum(a["sizing_violation_count"] for a in accounts)
        snapshot_candidate = {
            "available": True,
            "fresh": fresh,
            "ts": latest_ts,
            "age": age_text(latest_ts),
            "accounts": accounts,
            "wallet_usdt": wallet,
            "available_usdt": available,
            "margin_usdt": margin,
            "unrealized_pnl_usdt": upnl,
            "open_positions": positions,
            "risk_count": risks,
            "sizing_violation_count": sizing_count,
            "sizing_violations": sizing_violations,
            "note": f"实时 JSON 快照覆盖 {len(accounts)} 个账号，当前持仓 {positions}，浮盈亏 {upnl:+.2f} USDT。",
        }
        if fresh:
            return snapshot_candidate
    if not EVENT_STORE_DB.exists():
        if snapshot_candidate:
            snapshot_candidate["note"] = f"{snapshot_candidate.get('note', '')} JSON 快照已过期，且无 SQLite 账户快照可回退。"
            return snapshot_candidate
        return empty
    try:
        conn = sqlite3.connect(EVENT_STORE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select s.*
            from account_snapshots s
            join (
              select account, max(id) as latest_id
              from account_snapshots
              group by account
            ) latest on s.account=latest.account and s.id=latest.latest_id
            order by s.account
            """
        ).fetchall()
    except Exception as exc:
        out = dict(empty)
        out["note"] = f"账户快照读取失败：{exc}"
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not rows:
        if snapshot_candidate:
            snapshot_candidate["note"] = f"{snapshot_candidate.get('note', '')} JSON 快照已过期，SQLite 账户快照为空。"
            return snapshot_candidate
        return empty
    accounts = []
    latest_ts = None
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = {}
        ts = parse_dt(row["ts"])
        if ts and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
        accounts.append(
            {
                "account": row["account"],
                "strategy": payload.get("strategy") or row["account"],
                "version": payload.get("version", ""),
                "wallet_usdt": float(row["wallet_usdt"] or 0),
                "available_usdt": float(row["available_usdt"] or 0),
                "margin_usdt": float(row["margin_usdt"] or 0),
                "unrealized_pnl_usdt": float(row["unrealized_pnl_usdt"] or 0),
                "open_positions": int(row["open_positions"] or 0),
                "longs": int(payload.get("longs") or 0),
                "shorts": int(payload.get("shorts") or 0),
                "notional_usdt": float(payload.get("notional_usdt") or 0),
                "risk_count": int(payload.get("hard_stop_risk_count") or 0),
                "sizing_violation_count": int(payload.get("sizing_violation_count") or 0),
                "sizing_violations": payload.get("sizing_violations") or [],
                "worst": payload.get("worst_position") or {},
                "best": payload.get("best_position") or {},
                "ts": ts,
            }
        )
    age_seconds = (datetime.now(CST) - latest_ts).total_seconds() if latest_ts else None
    fresh = bool(latest_ts and age_seconds is not None and age_seconds <= 120)
    wallet = sum(a["wallet_usdt"] for a in accounts)
    available = sum(a["available_usdt"] for a in accounts)
    margin = sum(a["margin_usdt"] for a in accounts)
    upnl = sum(a["unrealized_pnl_usdt"] for a in accounts)
    positions = sum(a["open_positions"] for a in accounts)
    risks = sum(a["risk_count"] for a in accounts)
    sizing_violations = sum(a["sizing_violation_count"] for a in accounts)
    return {
        "available": True,
        "fresh": fresh,
        "ts": latest_ts,
        "age": age_text(latest_ts),
        "accounts": accounts,
        "wallet_usdt": wallet,
        "available_usdt": available,
        "margin_usdt": margin,
        "unrealized_pnl_usdt": upnl,
        "open_positions": positions,
        "risk_count": risks,
        "sizing_violation_count": sizing_violations,
        "note": f"实时快照覆盖 {len(accounts)} 个账号，当前持仓 {positions}，浮盈亏 {upnl:+.2f} USDT。",
    }


def alert_summary() -> dict[str, Any]:
    payload = read_json(ALERTS_PATH)
    if not isinstance(payload, dict):
        return {"available": False, "status": "unknown", "alert_count": 0, "age": "无巡检", "alerts": []}
    ts = parse_dt(payload.get("ts"))
    return {
        "available": True,
        "status": payload.get("status", "unknown"),
        "alert_count": int(payload.get("alert_count") or 0),
        "age": age_text(ts),
        "alerts": payload.get("alerts") or [],
        "services": payload.get("services") or {},
        "disk": payload.get("disk") or {},
        "timers": payload.get("timers") or {},
        "api_rate_limits": payload.get("api_rate_limits") or {},
    }


def sqlite_strategy_status() -> list[dict[str, Any]]:
    if not EVENT_STORE_DB.exists():
        return []
    specs = {
        "A/v11": ("120 秒", "15m + 30m", "SQLite system 双写"),
        "B/v16": ("120 秒", "1h 入场，15m 确认", "SQLite SCAN_STATS/system 双写"),
        "C/v14": ("120 秒", "1h 入场，15m 确认", "SQLite system 双写"),
    }
    try:
        conn = sqlite3.connect(EVENT_STORE_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select strategy, max(id) as latest_id
            from events
            where source like '%/system'
            group by strategy
            """
        ).fetchall()
        latest_by_strategy = {}
        for row in rows:
            latest = conn.execute(
                "select ts, event_type, payload_json from events where id=?",
                (row["latest_id"],),
            ).fetchone()
            latest_by_strategy[row["strategy"]] = latest
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out = []
    for name, (interval, period, cadence_note) in specs.items():
        row = latest_by_strategy.get(name)
        latest = parse_dt(row["ts"]) if row else None
        payload = {}
        if row:
            try:
                payload = json.loads(row["payload_json"])
            except Exception:
                payload = {}
        fresh = latest is not None and (datetime.now(CST) - latest).total_seconds() < 90 * 60
        signals = None
        opened = 0
        if name == "B/v16" and payload.get("event") == "SCAN_STATS":
            opened = int(payload.get("opened") or 0)
            signals = sum(int(payload.get(k) or 0) for k in ("no_signal", "score_low", "confirm_fail", "threshold_fail"))
        else:
            signals = payload.get("signals_found")
        out.append(
            {
                "name": name,
                "ok": fresh,
                "latest": latest,
                "age": age_text(latest),
                "interval": interval,
                "period": period,
                "cadence_note": cadence_note,
                "opened": opened,
                "signals": signals,
                "source": "sqlite",
            }
        )
    return out


def sqlite_strategy_decision_summary() -> list[dict[str, Any]]:
    if not EVENT_STORE_DB.exists():
        return []
    start = since_start()
    cutoff_day = start.strftime("%Y-%m-%d")
    out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(EVENT_STORE_DB)
        conn.row_factory = sqlite3.Row
        for name, trades_path, _signals_path, _decisions_path, _system_path in STRATEGY_FILES:
            grouped = conn.execute(
                """
                select source, category, event_type, count(*) as n
                from events
                where strategy=? and substr(ts, 1, 10) >= ?
                  and (source like '%/signals' or source like '%/decisions')
                group by source, category, event_type
                """,
                (name, cutoff_day),
            ).fetchall()
            raw_signal_count = sum(int(r["n"] or 0) for r in grouped if str(r["source"]).endswith("/signals"))
            signal_count = raw_signal_count
            if name == "C/v14":
                try:
                    signal_count = int(
                        conn.execute(
                            """
                            select count(*) from events
                            where strategy='C/v14' and source like '%/signals' and substr(ts, 1, 10) >= ?
                              and event_type='SIGNAL'
                              and coalesce(json_extract(payload_json, '$.timeframe'), '')='1h'
                              and abs(coalesce(score, 0)) <= 80
                              and (
                                (lower(side)='long' and abs(coalesce(score, 0)) >= 55)
                                or (lower(side)='short' and abs(coalesce(score, 0)) >= 70)
                              )
                            """,
                            (cutoff_day,),
                        ).fetchone()[0]
                    )
                except Exception:
                    rows = conn.execute(
                        """
                        select side, score, payload_json from events
                        where strategy='C/v14' and source like '%/signals' and substr(ts, 1, 10) >= ?
                          and event_type='SIGNAL'
                        """,
                        (cutoff_day,),
                    ).fetchall()
                    signal_count = 0
                    for row in rows:
                        try:
                            payload = json.loads(row["payload_json"])
                        except Exception:
                            payload = {}
                        if str(payload.get("timeframe") or "").lower() != "1h":
                            continue
                        side = str(row["side"] or payload.get("trade_side") or "").lower()
                        score = abs(float(row["score"] or payload.get("net_score") or 0))
                        if score <= 80 and ((side == "long" and score >= 55) or (side == "short" and score >= 70)):
                            signal_count += 1
            decision_counts = Counter()
            for row in grouped:
                if str(row["source"]).endswith("/decisions"):
                    decision_counts[str(row["category"] or row["event_type"] or "unknown")] += int(row["n"] or 0)
            sentinel_count = sum(v for k, v in decision_counts.items() if k.startswith("sentinel_"))
            http400 = int(
                conn.execute(
                    """
                    select count(*) from events
                    where strategy=? and source like '%/decisions' and substr(ts, 1, 10) >= ?
                      and (reason like '%HTTP Error 400%' or payload_json like '%HTTP Error 400%')
                    """,
                    (name, cutoff_day),
                ).fetchone()[0]
            )
            trade_rows = conn.execute(
                """
                select payload_json from events
                where source=? and substr(ts, 1, 10) >= ?
                order by id desc
                """,
                (f"{name}/trades", cutoff_day),
            ).fetchall()
            trades = []
            for row in trade_rows:
                try:
                    item = json.loads(row["payload_json"])
                except Exception:
                    continue
                if (row_time(item, "exit_time", "entry_time") or datetime.min.replace(tzinfo=CST)) >= start:
                    trades.append(item)
            if not trades:
                trades = [r for r in read_jsonl(trades_path) if (row_time(r, "exit_time", "entry_time") or datetime.min.replace(tzinfo=CST)) >= start]

            pnl = sum(float(r.get("pnl_usd") or 0) for r in trades)
            closed = len(trades)
            wins = sum(1 for r in trades if float(r.get("pnl_usd") or 0) > 0)
            win_rate = (wins / closed * 100) if closed else 0.0
            worst = min(trades, key=lambda r: float(r.get("pnl_usd") or 0), default={})
            best = max(trades, key=lambda r: float(r.get("pnl_usd") or 0), default={})

            opens = decision_counts.get("opened", 0) + decision_counts.get("OPEN", 0)
            confirm_filtered = decision_counts.get("confirm_filtered", 0) + decision_counts.get("confirmation_filtered", 0) + decision_counts.get("confirmation", 0)
            score_rejected = sum(v for k, v in decision_counts.items() if "score" in k and "rejected" in k)
            latest_row = conn.execute(
                "select ts from events where strategy=? order by id desc limit 1",
                (name,),
            ).fetchone()
            last_activity = parse_dt(latest_row["ts"]) if latest_row else None
            last_open_row = conn.execute(
                """
                select ts, symbol, side from events
                where strategy=? and source like '%/decisions'
                  and (event_type='OPEN' or category='opened')
                order by id desc limit 1
                """,
                (name,),
            ).fetchone()
            last_open_time = parse_dt(last_open_row["ts"]) if last_open_row else None
            last_open = "无开仓"
            if last_open_row:
                last_open = f"{last_open_row['symbol']} {last_open_row['side']} / {age_text(last_open_time)}"
            latest_system_payload = {}
            system_row = conn.execute(
                "select payload_json from events where strategy=? and source like '%/system' order by id desc limit 1",
                (name,),
            ).fetchone()
            if system_row:
                try:
                    latest_system_payload = json.loads(system_row["payload_json"])
                except Exception:
                    latest_system_payload = {}

            notes: list[str] = []
            if pnl < 0:
                notes.append("亏损拖累")
            if closed >= 5 and win_rate < 40:
                notes.append("胜率偏低")
            if signal_count > 5000 and opens < 10:
                notes.append("信号过密/转化低")
            if name == "C/v14" and raw_signal_count > max(signal_count * 5, signal_count + 1000):
                notes.append(f"原始候选{raw_signal_count}/入场候选{signal_count}")
            if http400:
                notes.append("哨兵400未清零")
            if not signal_count:
                notes.append("无SQLite信号")
            if last_activity and (datetime.now(CST) - last_activity).total_seconds() > 90 * 60:
                notes.append("SQLite数据偏旧")
            if last_open_time and (datetime.now(CST) - last_open_time).total_seconds() > 48 * 3600:
                notes.append("48h无新开仓")
            if not notes:
                notes.append("SQLite口径正常")

            out.append(
                {
                    "name": name,
                    "pnl": pnl,
                    "closed": closed,
                    "wins": wins,
                    "win_rate": win_rate,
                    "signals": signal_count,
                    "raw_signals": raw_signal_count,
                    "opens": opens,
                    "sentinel": sentinel_count,
                    "http400": http400,
                    "confirm_filtered": confirm_filtered,
                    "score_rejected": score_rejected,
                    "positions": latest_system_payload.get("open_positions", latest_system_payload.get("positions", "-")),
                    "last_activity": last_activity,
                    "age": age_text(last_activity),
                    "last_open_time": last_open_time,
                    "last_open": last_open,
                    "best": f"{best.get('symbol', '-')} {float(best.get('pnl_usd') or 0):+.2f}",
                    "worst": f"{worst.get('symbol', '-')} {float(worst.get('pnl_usd') or 0):+.2f}",
                    "notes": " / ".join(notes),
                    "source": "sqlite",
                }
            )
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def strategy_decision_summary() -> list[dict[str, Any]]:
    start = since_start()
    out: list[dict[str, Any]] = []
    for name, trades_path, signals_path, decisions_path, system_path in STRATEGY_FILES:
        trades = [r for r in read_jsonl(trades_path) if (row_time(r, "exit_time", "entry_time") or datetime.min.replace(tzinfo=CST)) >= start]
        signals = [r for r in read_jsonl(signals_path) if (row_time(r, "ts", "time") or datetime.min.replace(tzinfo=CST)) >= start]
        decisions = [r for r in read_jsonl(decisions_path) if (row_time(r, "time", "ts") or datetime.min.replace(tzinfo=CST)) >= start]
        systems = read_jsonl(system_path)
        latest_system = systems[-1] if systems else {}

        pnl = sum(float(r.get("pnl_usd") or 0) for r in trades)
        closed = len(trades)
        wins = sum(1 for r in trades if float(r.get("pnl_usd") or 0) > 0)
        win_rate = (wins / closed * 100) if closed else 0.0
        worst = min(trades, key=lambda r: float(r.get("pnl_usd") or 0), default={})
        best = max(trades, key=lambda r: float(r.get("pnl_usd") or 0), default={})
        decision_counts = Counter(str(r.get("category") or r.get("status") or r.get("event") or "unknown") for r in decisions)
        sentinel = [r for r in decisions if r.get("sentinel") or str(r.get("category") or "").startswith("sentinel_")]
        http400 = sum(1 for r in decisions if "HTTP Error 400" in str(r.get("reason") or r.get("raw") or ""))
        opens = decision_counts.get("opened", 0) + decision_counts.get("OPEN", 0)
        confirm_filtered = decision_counts.get("confirm_filtered", 0) + decision_counts.get("confirmation_filtered", 0)
        score_rejected = sum(v for k, v in decision_counts.items() if "score" in k and "rejected" in k)
        open_decisions = [
            r for r in decisions
            if str(r.get("category") or r.get("status") or r.get("event") or "").lower() in {"opened", "open"}
        ]
        last_open_decision = max(open_decisions, key=lambda r: row_time(r, "time", "ts") or datetime.min.replace(tzinfo=CST), default={})
        last_open_time = row_time(last_open_decision, "time", "ts") if last_open_decision else None
        last_open = "无开仓"
        if last_open_decision:
            last_open = f"{last_open_decision.get('symbol', '-')} {last_open_decision.get('side', '-')} / {age_text(last_open_time)}"

        latest_signal_time = max([row_time(r, "ts", "time") for r in signals if row_time(r, "ts", "time")] or [None])
        latest_decision_time = max([row_time(r, "time", "ts") for r in decisions if row_time(r, "time", "ts")] or [None])
        last_activity = max([dt for dt in (latest_signal_time, latest_decision_time, row_time(latest_system, "ts", "time")) if dt] or [None])

        notes: list[str] = []
        if pnl < 0:
            notes.append("亏损拖累")
        if closed >= 5 and win_rate < 40:
            notes.append("胜率偏低")
        if len(signals) > 5000 and opens < 10:
            notes.append("信号过密/转化低")
        if http400:
            notes.append("哨兵400未部署清零")
        if not signals:
            notes.append("无信号记录")
        if last_activity and (datetime.now(CST) - last_activity).total_seconds() > 90 * 60:
            notes.append("镜像数据偏旧")
        if last_open_time and (datetime.now(CST) - last_open_time).total_seconds() > 48 * 3600:
            notes.append("48h无新开仓")
        if not notes:
            notes.append("暂无明显异常")

        out.append(
            {
                "name": name,
                "pnl": pnl,
                "closed": closed,
                "wins": wins,
                "win_rate": win_rate,
                "signals": len(signals),
                "opens": opens,
                "sentinel": len(sentinel),
                "http400": http400,
                "confirm_filtered": confirm_filtered,
                "score_rejected": score_rejected,
                "positions": latest_system.get("open_positions", latest_system.get("positions", "-")),
                "last_activity": last_activity,
                "age": age_text(last_activity),
                "last_open_time": last_open_time,
                "last_open": last_open,
                "best": f"{best.get('symbol', '-')} {float(best.get('pnl_usd') or 0):+.2f}",
                "worst": f"{worst.get('symbol', '-')} {float(worst.get('pnl_usd') or 0):+.2f}",
                "notes": " / ".join(notes),
            }
        )
    return out


def function_status_cards(data: dict[str, Any]) -> list[dict[str, str]]:
    now = datetime.now(CST)
    mirror_files = [p for _, _, _, _, p in STRATEGY_FILES if p.exists()]
    mirror_latest = max([datetime.fromtimestamp(p.stat().st_mtime, CST) for p in mirror_files] or [None])
    mirror_old = mirror_latest is None or (now - mirror_latest).total_seconds() > 90 * 60
    scanner_bad = [s["name"] for s in data["strategies"] if not s["ok"]]
    sig = data.get("signal_summary") or {}
    research = data.get("research_summary") or {}
    event_store = data.get("event_store_shadow") or {}
    market_cache = data.get("market_cache") or {}
    realtime_account = data.get("realtime_account") or {}
    alerts = data.get("alerts") or {}
    api_rate_limits = alerts.get("api_rate_limits") or {}
    api_rate_total = int(api_rate_limits.get("total") or 0)
    api_rate_sources = ", ".join(
        f"{name}:{count}"
        for name, count in sorted((api_rate_limits.get("by_service") or {}).items(), key=lambda item: int(item[1]), reverse=True)[:3]
    )
    api_rate_note = (
        f"；API限流 {api_rate_total} 条，来源 {api_rate_sources or '-'}"
        if api_rate_total
        else ""
    )
    counterfactual = data.get("counterfactual") or {}
    research_store = data.get("research_store") or {}
    replay_feature = data.get("replay_feature") or {}
    replay_summary = replay_feature.get("summary") or {}
    replay_gate = data.get("replay_gate") or {}
    replay_gate_summary_data = replay_gate.get("summary") or {}
    replay_parity = data.get("replay_parity") or {}
    replay_parity_summary_data = replay_parity.get("summary") or {}
    rollback_watch = data.get("rollback_watch") or {}
    rollback_watch_summary_data = rollback_watch.get("summary") or {}
    a_v11_rollout = data.get("a_v11_rollout") or {}
    a_v11_rollout_decision = a_v11_rollout.get("decision") or {}
    a_v11_rollout_72h = (a_v11_rollout.get("windows") or {}).get("72h") or {}
    b_v16_rollout = data.get("b_v16_rollout") or {}
    b_v16_rollout_decision = b_v16_rollout.get("decision") or {}
    b_v16_rollout_72h = (b_v16_rollout.get("windows") or {}).get("72h") or {}
    sentinel_quality = data.get("sentinel_quality") or {}
    sentinel_coverage = sentinel_quality.get("coverage") or {}
    sentinel_watchlist = sentinel_quality.get("watchlist_history") or {}
    sentinel_forward_60m = ((sentinel_quality.get("forward_returns") or {}).get("by_horizon") or {}).get("60m") or {}
    sentinel_forward_60m_dir = sentinel_forward_60m.get("directional_after_fee") or {}
    evolution = data.get("strategy_evolution") or {}
    evo_counts = evolution.get("counts") or {}
    attention = data.get("attention") or {}
    attention_summary_data = attention.get("summary") or {}
    attention_counts = attention_summary_data.get("counts") or {}
    cards = [
        {
            "level": "bad" if int(attention_counts.get("P0") or 0) else "warn" if int(attention_counts.get("P1") or 0) else "ok" if attention.get("available") else "warn",
            "name": "持久关注台账",
            "value": (
                f"{int(attention_summary_data.get('open') or 0)} open"
                if attention.get("available")
                else "未生成"
            ),
            "body": (
                f"P0 {int(attention_counts.get('P0') or 0)}，P1 {int(attention_counts.get('P1') or 0)}，P2 {int(attention_counts.get('P2') or 0)}；"
                f"已消失待复核 {int(attention_summary_data.get('cleared_pending_review') or 0)}。"
                if attention.get("available")
                else "关注台账尚未生成，日报滚动后事项可能只存在于历史报告。"
            ),
        },
        {
            "level": "warn" if mirror_old else "ok",
            "name": "数据同步/服务器镜像",
            "value": age_text(mirror_latest),
            "body": "本地镜像已偏旧，当前 SSH banner 超时会影响实时判断。" if mirror_old else "日志镜像新鲜，可用于入口判断。",
        },
        {
            "level": "bad" if scanner_bad else "ok",
            "name": "三策略服务",
            "value": "异常" if scanner_bad else "运行中",
            "body": "过久未更新：" + ", ".join(scanner_bad) if scanner_bad else "A/v11、B/v16、C/v14 均有近期心跳或扫描统计。",
        },
        {
            "level": "warn" if int(sig.get("http400") or 0) else "ok",
            "name": "哨兵链路",
            "value": str(sum(int(r.get("decisions") or 0) for r in sig.get("sentinel", []))),
            "body": f"旧报告仍有 {sig.get('http400')} 条 400；本地已修，待远端部署。" if int(sig.get("http400") or 0) else "哨兵扫描、层级归因和结果追踪正常。",
        },
        {
            "level": "ok" if sentinel_quality.get("fresh") else "warn",
            "name": "哨兵前向收益/覆盖",
            "value": (
                f"{float(sentinel_coverage.get('coverage_pct') or 0):.1f}%"
                if sentinel_quality.get("available")
                else "未生成"
            ),
            "body": (
                f"大行情覆盖 {int(sentinel_coverage.get('covered_big_move_signals') or 0)}/"
                f"{int(sentinel_coverage.get('big_move_signals') or 0)}；"
                f"60m方向扣费后均值 {float(sentinel_forward_60m_dir.get('avg_pct') or 0):+.4f}%，"
                f"胜率 {float(sentinel_forward_60m_dir.get('win_rate_pct') or 0):.1f}%。"
                if sentinel_quality.get("available")
                else "哨兵质量复盘尚未生成 forward return / coverage 审计。"
            ),
        },
        {
            "level": "ok" if int(research.get("small_live") or 0) else "warn" if int(research.get("approved") or 0) else "ok",
            "name": "研究/小仓审批",
            "value": f"小仓{research.get('small_live', 0)} / 待审{research.get('approved', 0)}",
            "body": "B/v16 阶段保护已批准小仓观察。" if int(research.get("small_live") or 0) else "无小仓批准项。" if not int(research.get("approved") or 0) else "仍有候选待人工审批。",
        },
        {
            "level": "warn" if int(evo_counts.get("P0") or 0) or int(evo_counts.get("P1") or 0) else "ok" if evolution.get("fresh") else "warn",
            "name": "策略进化门禁",
            "value": (
                f"P0:{int(evo_counts.get('P0') or 0)} / P1:{int(evo_counts.get('P1') or 0)}"
                if evolution.get("available")
                else "未生成"
            ),
            "body": (
                f"统一门禁更新 {evolution.get('age')}；P2观察 {int(evo_counts.get('P2') or 0)}，拒绝 {int(evo_counts.get('REJECT') or 0)}。"
                if evolution.get("available")
                else "尚未生成统一进化门禁报告。"
            ),
        },
        {
            "level": "ok" if event_store.get("coverage_ok") else "warn",
            "name": "SQLite事件库",
            "value": f"{int(event_store.get('total_events') or 0)} 条",
            "body": event_store.get("note") or "事件库影子对比尚未生成。",
        },
        {
            "level": "ok" if market_cache.get("ok") else "warn",
            "name": "统一行情缓存",
            "value": market_cache.get("age") or "无缓存",
            "body": market_cache.get("note") or "缓存服务未生成可读行情快照。",
        },
        {
            "level": "bad" if int(realtime_account.get("risk_count") or 0) else "ok" if realtime_account.get("fresh") else "warn",
            "name": "实时账户快照",
            "value": realtime_account.get("age") or "无快照",
            "body": realtime_account.get("note") or "账户快照服务尚未写入 SQLite。",
        },
        {
            "level": "ok" if alerts.get("status") == "ok" else "bad" if alerts.get("status") == "bad" else "warn",
            "name": "自动告警",
            "value": f"{int(alerts.get('alert_count') or 0)} 条",
            "body": (
                f"最近巡检 {alerts.get('age', '无')}；磁盘已用 {alerts.get('disk', {}).get('used_pct', '-')}%，剩余 {alerts.get('disk', {}).get('free_gb', '-')}GB{api_rate_note}。"
                if alerts.get("available")
                else "告警巡检尚未生成。"
            ),
        },
        {
            "level": "ok" if counterfactual.get("fresh") else "warn",
            "name": "反事实评估",
            "value": (
                f"{int((counterfactual.get('overall') or {}).get('samples') or 0)} 样本"
                if counterfactual.get("available")
                else "未生成"
            ),
            "body": (
                f"{counterfactual.get('horizon', 60)}m 全量放行模拟 PnL "
                f"{float((counterfactual.get('overall') or {}).get('pnl') or 0):+.2f}；更新 {counterfactual.get('age')}。"
                if counterfactual.get("available")
                else "OPEN_SKIPPED 反事实任务尚无最新报告。"
            ),
        },
        {
            "level": "ok" if research_store.get("fresh") else "warn",
            "name": "研究仓/DuckDB",
            "value": (
                f"{int((research_store.get('totals') or {}).get('events') or 0)} 事件"
                if research_store.get("available")
                else "未生成"
            ),
            "body": (
                f"{research_store.get('days', 0)}日样本：开仓 {int((research_store.get('totals') or {}).get('opens') or 0)}，"
                f"跳过 {int((research_store.get('totals') or {}).get('skipped') or 0)}，失败 {int((research_store.get('totals') or {}).get('failed') or 0)}；"
                f"主否决 {((research_store.get('top_skip') or {}).get('strategy') or '-')}/{((research_store.get('top_skip') or {}).get('gate') or '-')}；"
                f"K线周期 {len(research_store.get('kline_coverage') or [])}，特征周期 {len(research_store.get('feature_coverage') or [])}。"
                if research_store.get("available")
                else "Parquet/DuckDB 样本漏斗尚未生成，进化判断只能看 SQLite/报表口径。"
            ),
        },
        {
            "level": (
                "ok"
                if replay_feature.get("fresh") and float(replay_summary.get("match_rate") or 0) >= 65
                else "warn"
            ),
            "name": "Replay feature align",
            "value": (
                f"{int(replay_summary.get('events') or 0)} events"
                if replay_feature.get("available")
                else "not built"
            ),
            "body": (
                f"matched {int(replay_summary.get('matched_features') or 0)} / "
                f"{int(replay_summary.get('events') or 0)}; "
                f"match {float(replay_summary.get('match_rate') or 0):.1f}%; "
                f"updated {replay_feature.get('age')}."
                if replay_feature.get("available")
                else "Replay/live feature alignment dataset missing; full gate parity still partial."
            ),
        },
        {
            "level": (
                "ok"
                if replay_gate.get("fresh") and float(replay_gate_summary_data.get("gate_coverage_pct") or 0) >= 90
                else "warn"
            ),
            "name": "Replay gate audit",
            "value": (
                f"{float(replay_gate_summary_data.get('gate_coverage_pct') or 0):.1f}%"
                if replay_gate.get("available")
                else "not built"
            ),
            "body": (
                f"open-flow {int(replay_gate_summary_data.get('open_flow_events') or 0)}; "
                f"unknown gate {int(replay_gate_summary_data.get('unknown_gate') or 0)}; "
                f"updated {replay_gate.get('age')}."
                if replay_gate.get("available")
                else "Live events have not been audited through core.replay yet."
            ),
        },
        {
            "level": (
                "bad"
                if int(replay_parity_summary_data.get("mismatched") or 0) or int(replay_parity_summary_data.get("errors") or 0)
                else "ok"
                if replay_parity.get("fresh") and int(replay_parity_summary_data.get("gate_cases") or 0) > 0
                else "warn"
            ),
            "name": "Replay/live parity",
            "value": (
                f"{float(replay_parity_summary_data.get('pass_rate_pct') or 0):.1f}%"
                if replay_parity.get("available") and int(replay_parity_summary_data.get("gate_cases") or 0) > 0
                else "case gap"
                if replay_parity.get("available")
                else "not built"
            ),
            "body": (
                f"exact cases {int(replay_parity_summary_data.get('gate_cases') or 0)}; "
                f"coverage {float(replay_parity_summary_data.get('exact_case_coverage_pct') or 0):.1f}%; "
                f"mismatch {int(replay_parity_summary_data.get('mismatched') or 0)}; "
                f"errors {int(replay_parity_summary_data.get('errors') or 0)}; "
                f"updated {replay_parity.get('age')}."
                if replay_parity.get("available")
                else "Exact same-input gate-case audit missing; replay/live parity cannot be claimed."
            ),
        },
        {
            "level": (
                "bad"
                if int(rollback_watch_summary_data.get("p0") or 0) > 0
                else "warn"
                if int(rollback_watch_summary_data.get("p1") or 0) > 0
                else "ok"
                if rollback_watch.get("fresh")
                else "warn"
            ),
            "name": "Rollback watch",
            "value": (
                f"P0 {int(rollback_watch_summary_data.get('p0') or 0)} / P1 {int(rollback_watch_summary_data.get('p1') or 0)}"
                if rollback_watch.get("available")
                else "not built"
            ),
            "body": (
                f"items {int(rollback_watch_summary_data.get('items') or 0)}; "
                f"worst {rollback_watch_summary_data.get('worst_candidate') or '-'} "
                f"{float(rollback_watch_summary_data.get('worst_pnl_after_cost_24h') or 0):+.2f}; "
                f"close failed {int(rollback_watch_summary_data.get('close_failed_24h') or 0)} "
                f"(resolved {int(rollback_watch_summary_data.get('resolved_close_failed_24h') or 0)}); "
                f"updated {rollback_watch.get('age')}."
                if rollback_watch.get("available")
                else "P1 rollback-watch action matrix missing from report chain."
            ),
        },
        {
            "level": (
                "warn"
                if a_v11_rollout_decision.get("priority") in {"P0", "P1"}
                else "ok"
                if a_v11_rollout.get("fresh")
                else "warn"
            ),
            "name": "A/v11 rollout review",
            "value": (
                f"{a_v11_rollout_decision.get('priority')}/{a_v11_rollout_decision.get('status')}"
                if a_v11_rollout.get("available")
                else "not built"
            ),
            "body": (
                f"72h closed {int(a_v11_rollout_72h.get('closed_samples') or 0)}; "
                f"after-cost PnL {float(a_v11_rollout_72h.get('pnl_after_cost_usdt') or 0):+.2f}; "
                f"updated {a_v11_rollout.get('age')}."
                if a_v11_rollout.get("available")
                else "A/v11 trailing rollout review missing from report chain."
            ),
        },
        {
            "level": (
                "warn"
                if b_v16_rollout_decision.get("priority") in {"P0", "P1"}
                else "ok"
                if b_v16_rollout.get("fresh")
                else "warn"
            ),
            "name": "B/v16 rollout review",
            "value": (
                f"{b_v16_rollout_decision.get('priority')}/{b_v16_rollout_decision.get('status')}"
                if b_v16_rollout.get("available")
                else "not built"
            ),
            "body": (
                f"72h closed {int(b_v16_rollout_72h.get('closed_samples') or 0)}; "
                f"after-cost PnL {float(b_v16_rollout_72h.get('pnl_after_cost_usdt') or 0):+.2f}; "
                f"updated {b_v16_rollout.get('age')}."
                if b_v16_rollout.get("available")
                else "B/v16 full-live rollout review missing from report chain."
            ),
        },
    ]
    return cards


def build_findings(data: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    sig = data.get("signal_summary") or {}
    decision = data.get("decision_summary") or []
    realtime_account = data.get("realtime_account") or {}
    alerts = data.get("alerts") or {}
    counterfactual = data.get("counterfactual") or {}
    evolution = data.get("strategy_evolution") or {}
    attention = data.get("attention") or {}
    total_pnl = float(sig.get("total_pnl") or 0)
    live_pnl = sum(float(r.get("pnl") or 0) for r in decision)
    account_upnl = float(realtime_account.get("unrealized_pnl_usdt") or 0)
    attention_top = attention.get("top") or {}
    if attention.get("available") and attention_top:
        level = "bad" if attention_top.get("priority") == "P0" else "warn" if attention_top.get("priority") == "P1" else "ok"
        findings.append(
            {
                "level": level,
                "title": f"{attention_top.get('priority')} 持久关注：{attention_top.get('title')}",
                "body": f"{attention_top.get('evidence') or '-'}；建议：{attention_top.get('recommended_action') or '-'}",
            }
        )
    truth = data.get("strategy_truth") or {}
    if truth.get("available"):
        for strategy in ["A/v11", "B/v16", "C/v14"]:
            stats = (truth.get("strategy_stats") or {}).get(strategy, {})
            rec = (truth.get("recovery_stats") or {}).get(strategy, {})
            closed = int(stats.get("closed_trades") or 0)
            if closed == 0 and int(rec.get("count") or 0) == 0:
                continue
            pf = stats.get("profit_factor", 0)
            pf_val = float(pf) if pf != "inf" else 999
            net_pnl = float(stats.get("net_pnl_usd") or 0)
            rec_count = int(rec.get("count") or 0)
            rec_upnl = float(rec.get("total_unrealized_pnl") or 0)
            if pf_val < 1 and closed >= 5:
                findings.append({
                    "level": "warn",
                    "title": f"P1 {strategy} 主动策略负期望 PF={pf}",
                    "body": f"已平仓 {closed} 笔，净PnL {net_pnl:+.2f}，胜率 {stats.get('win_rate', 0)}%；恢复仓 {rec_count} 个浮盈 {rec_upnl:+.2f}。真相台账显示主动策略 alpha 不足。",
                })
            elif pf_val >= 2 and closed >= 10:
                findings.append({
                    "level": "ok",
                    "title": f"P2 {strategy} 主动策略正期望 PF={pf}",
                    "body": f"已平仓 {closed} 笔，净PnL {net_pnl:+.2f}，胜率 {stats.get('win_rate', 0)}%；恢复仓 {rec_count} 个浮盈 {rec_upnl:+.2f}。",
                })
            if rec_count > 0:
                findings.append({
                    "level": "ok",
                    "title": f"P2 {strategy} 恢复仓 {rec_count} 个",
                    "body": f"恢复仓浮盈 {rec_upnl:+.2f} USDT，不计入主动策略 alpha。",
                })

    evo_top = evolution.get("top") or {}
    if evolution.get("available") and evo_top:
        priority = str(evo_top.get("priority") or "P3")
        if priority in {"P0", "P1", "P2"}:
            status_text = str(evo_top.get("status") or "")
            title_kind = (
                "策略回滚观察"
                if status_text in {"rollback_required", "rollback_watch"}
                else "策略升级机会"
                if priority in {"P0", "P1"}
                else "策略进化观察"
            )
            findings.append(
                {
                    "level": "warn" if priority in {"P0", "P1"} else "ok",
                    "title": f"{priority} {title_kind}：{evo_top.get('strategy') or '-'}",
                    "body": (
                        f"{evo_top.get('candidate_id') or '-'} 当前 {evo_top.get('status') or '-'}；"
                        f"建议 {evo_top.get('recommended_action') or '-'}；"
                        f"证据分 {int(evo_top.get('evidence_score') or 0)}，风险分 {int(evo_top.get('risk_score') or 0)}。"
                    ),
                }
            )
        regime_summary = evolution.get("regime_summary") or {}
        if int(regime_summary.get("window_count") or 0):
            close_failed = int(regime_summary.get("close_failed_24h") or 0)
            open_failed = int(regime_summary.get("open_failed_24h") or 0)
            findings.append(
                {
                    "level": "bad" if close_failed else "warn" if open_failed >= 5 else "ok",
                    "title": "P2 已放开候选 24h 环境分层",
                    "body": (
                        f"regime={regime_summary.get('counts') or {}}；"
                        f"quality={regime_summary.get('quality_counts') or {}}；"
                        f"OPEN_FAILED={open_failed}，CLOSE_FAILED={close_failed}。"
                    ),
                }
            )
    if realtime_account.get("available"):
        sizing_count = int(realtime_account.get("sizing_violation_count") or 0)
        findings.append(
            {
                "level": "bad" if int(realtime_account.get("risk_count") or 0) or sizing_count else "warn" if not realtime_account.get("fresh") else "ok",
                "title": f"P0 实时账户浮盈亏 {account_upnl:+.2f} USDT",
                "body": f"持仓 {int(realtime_account.get('open_positions') or 0)}，快照 {realtime_account.get('age')}，硬顶风险 {int(realtime_account.get('risk_count') or 0)}，尺寸违规 {sizing_count}。",
            }
        )
    if alerts.get("available") and int(alerts.get("alert_count") or 0):
        first_alert = (alerts.get("alerts") or [{}])[0]
        findings.append(
            {
                "level": "bad" if alerts.get("status") == "bad" else "warn",
                "title": f"P0/P1 自动告警 {int(alerts.get('alert_count') or 0)} 条",
                "body": f"{first_alert.get('title', '系统巡检异常')}：{first_alert.get('body', '')}",
            }
        )
    cf_filters = counterfactual.get("filters") or []
    a_replacement = next(
        (
            row for row in cf_filters
            if row.get("strategy") == "A/v11" and "position_replacement" in str(row.get("filter") or "")
        ),
        None,
    )
    if a_replacement and float(a_replacement.get("pnl") or 0) > 0:
        findings.append(
            {
                "level": "warn",
                "title": f"P1 A/v11 满仓替换错杀 {float(a_replacement.get('pnl') or 0):+.2f} USDT",
                "body": f"{int(a_replacement.get('samples') or 0)} 个被拒样本在 {counterfactual.get('horizon', 60)}m 反事实为正；替换释放规则已进入优先优化。",
            }
        )
    if sig.get("strategies"):
        level = "bad" if total_pnl < 0 else "ok"
        findings.append(
            {
                "level": level,
                "title": f"P1 昨日复盘PnL {total_pnl:+.2f} USDT",
                "body": f"最新复盘周期 {sig.get('date')}：开仓 {sig.get('total_opens')}，平仓 {sig.get('total_closes')}。",
            }
        )
    if decision:
        findings.append(
            {
                "level": "bad" if live_pnl < 0 else "ok",
                "title": f"P1 昨天到当前镜像PnL {live_pnl:+.2f} USDT",
                "body": "来自本地同步的交易日志，覆盖昨日 00:00 到当前镜像时间。",
            }
        )
    b_row = next((row for row in decision if row.get("name") == "B/v16"), None)
    if b_row:
        last_open_time = b_row.get("last_open_time")
        stale_open = bool(last_open_time and (datetime.now(CST) - last_open_time).total_seconds() > 48 * 3600)
        findings.append(
            {
                "level": "warn" if stale_open else "ok",
                "title": "P2 B/v16 最近开仓核对",
                "body": (
                    f"最近开仓：{b_row.get('last_open') or '无记录'}；最后活动 {b_row.get('age')}。"
                    "这张表用事件库口径，不再只看当前仍持有仓位的旧开仓时间。"
                ),
            }
        )
    stale = [s["name"] for s in data["strategies"] if not s["ok"]]
    findings.append(
        {
            "level": "bad" if stale else "ok",
            "title": "P0 服务器策略运行状态" if stale else "P2 服务器策略运行状态",
            "body": "过久未更新：" + ", ".join(stale) if stale else "A/v11、B/v16、C/v14 最近都有镜像心跳或扫描统计，入口判定为运行中。",
        }
    )
    if int(sig.get("http400") or 0):
        findings.append(
            {
                "level": "warn",
                "title": "P1 哨兵逐币 HTTP 400 已定位",
                "body": f"最新报告里还有 {sig.get('http400')} 条旧异常，根因是实盘哨兵币种不一定存在于 testnet 扫描池；扫描前过滤已补上。",
            }
        )
    sentinel_decisions = sum(int(r.get("decisions") or 0) for r in sig.get("sentinel") or [])
    if sentinel_decisions:
        findings.append(
            {
                "level": "ok",
                "title": f"P2 哨兵链路有 {sentinel_decisions} 条可追踪决策",
                "body": "现在可以看见哪些策略扫了哨兵币、在哪层候选、开仓、确认过滤、策略否决或无信号。",
            }
        )
    c_row = next((r for r in sig.get("strategies") or [] if r.get("name") == "C/v14"), None)
    if c_row and int(c_row.get("signals") or 0) > 10000:
        findings.append(
            {
                "level": "warn",
                "title": "P1 C/v14 信号量异常偏大",
                "body": f"C/v14 信号 {c_row.get('signals')}、开仓 {c_row.get('opens')}，首页建议优先看确认层和低分开仓，不要只看总信号数。",
            }
        )
    research = data.get("research_summary") or {}
    if int(research.get("small_live") or 0):
        findings.append(
            {
                "level": "ok",
                "title": "P2 B/v16 阶段保护已批小仓",
                "body": "HYP-2026-05-22-B-v16-reverse_trade-stage-guard 已进入 small live observation，后续重点看硬顶和错过盈利。",
            }
        )
    if int(research.get("approved") or 0):
        findings.append(
            {
                "level": "warn",
                "title": "P2 研究审阅台有待人工确认候选",
                "body": f"{research.get('top')} 当前 {research.get('top_status')}；上线前需要你进审阅台看证据。",
            }
        )
    return findings


def build_executive_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Build the first-screen decision brief for a human decision maker."""
    realtime_account = data.get("realtime_account") or {}
    alerts = data.get("alerts") or {}
    attention = data.get("attention") or {}
    attention_counts = ((attention.get("summary") or {}).get("counts") or {})
    evolution = data.get("strategy_evolution") or {}
    evolution_counts = evolution.get("counts") or {}
    regime_summary = evolution.get("regime_summary") or {}
    expansion = evolution.get("expansion_readiness") or {}
    gate_hardening = evolution.get("promotion_gate_hardening") or {}
    evolution_alert = evolution_action_alert(evolution)
    rollback_brief = rollback_watch_brief(evolution)
    replay_gate = data.get("replay_gate") or {}
    replay_gate_summary_data = replay_gate.get("summary") or {}
    replay_parity = data.get("replay_parity") or {}
    replay_parity_summary_data = replay_parity.get("summary") or {}
    a_v11_rollout = data.get("a_v11_rollout") or {}
    a_v11_rollout_decision = a_v11_rollout.get("decision") or {}
    a_v11_rollout_72h = (a_v11_rollout.get("windows") or {}).get("72h") or {}
    b_v16_rollout = data.get("b_v16_rollout") or {}
    b_v16_rollout_decision = b_v16_rollout.get("decision") or {}
    b_v16_rollout_72h = (b_v16_rollout.get("windows") or {}).get("72h") or {}
    truth = data.get("strategy_truth") or {}
    truth_stats = truth.get("strategy_stats") or {}
    decision_rows = data.get("decision_summary") or []

    p0 = int(attention_counts.get("P0") or 0)
    p1 = int(attention_counts.get("P1") or 0)
    alert_count = int(alerts.get("alert_count") or 0)
    risk_count = int(realtime_account.get("risk_count") or 0)
    sizing_count = int(realtime_account.get("sizing_violation_count") or 0)
    account_upnl = float(realtime_account.get("unrealized_pnl_usdt") or 0)
    positions = int(realtime_account.get("open_positions") or 0)
    service_states = alerts.get("services") if isinstance(alerts.get("services"), dict) else {}
    services_ok = bool(service_states) and all(str(v) == "active" for v in service_states.values())
    if not service_states:
        services_ok = all(s.get("ok") for s in data.get("strategies", []))

    if p0 or alert_count or risk_count or sizing_count:
        level = "bad"
        headline = "需要先处理风险，再讨论策略扩张"
    elif p1:
        level = "warn"
        headline = "系统可运行，但有高优先级策略/关注项待决策"
    else:
        level = "ok"
        headline = "系统可继续运行，重点进入受控扩样和策略进化"

    def strategy_line(name: str) -> dict[str, str]:
        row = next((r for r in decision_rows if r.get("name") == name), {}) or {}
        stats = (truth_stats.get(name) or {}) if isinstance(truth_stats, dict) else {}
        closed = int(stats.get("closed_trades") or row.get("closed") or 0)
        pf = stats.get("profit_factor", "-")
        pnl = float(stats.get("net_pnl_usd") or row.get("pnl") or 0)
        opens = int(row.get("opens") or 0)
        last_open = row.get("last_open") or "无"
        if name == "A/v11":
            decision = "稳态监控"
            reason = "不继续放宽，守住100 USDT尺寸、同币禁叠和替换保护。"
        elif name == "B/v16":
            decision = "观察全量候选"
            reason = "已放开ATR分档止损和85过热封顶，重点看新样本PnL与硬止损率。"
        else:
            decision = "受控扩样"
            reason = "已放宽确认与赛道/周期上限，先看转化率和PF，不再立刻二次放宽。"
        return {
            "name": name,
            "decision": decision,
            "pnl": f"{pnl:+.2f}",
            "pf": str(pf),
            "closed": str(closed),
            "opens": str(opens),
            "last_open": str(last_open),
            "reason": reason,
        }

    strategy_decisions = [strategy_line(name) for name in ("A/v11", "B/v16", "C/v14")]
    evo_p0 = int(evolution_counts.get("P0") or 0)
    evo_p1 = int(evolution_counts.get("P1") or 0)
    actionable_evo = p0 + p1
    evo_top = evolution.get("top") or {}
    if actionable_evo:
        evo_text = (
            f"首页待决策 P0/P1 {actionable_evo}；原始门禁 P0 {evo_p0} / P1 {evo_p1}"
            + (f"，最高项 {evo_top.get('strategy') or '-'} {evo_top.get('candidate_id') or '-'}" if evo_top else "")
        )
    else:
        evo_text = f"暂无需要立即批准的进化项；原始门禁 P0 {evo_p0} / P1 {evo_p1} 仅作下钻审计"

    bullets = [
        f"账户：实时浮盈亏 {account_upnl:+.2f} USDT，持仓 {positions}，硬顶风险 {risk_count}，尺寸违规 {sizing_count}。",
        f"运行：核心服务 {'正常' if services_ok else '需检查'}，自动告警 {alert_count}，持久关注 P0 {p0} / P1 {p1}。",
        f"进化：{evo_text}；只有门禁通过且用户批准的候选才允许进入实盘。",
        f"最高进化提示：{evolution_alert.get('title')}；{evolution_alert.get('body')}",
        rollback_brief,
        (
            f"B/v16 full-live review: {b_v16_rollout_decision.get('priority')}/{b_v16_rollout_decision.get('status')}; "
            f"72h after-cost PnL {float(b_v16_rollout_72h.get('pnl_after_cost_usdt') or 0):+.2f} USDT; "
            f"closed samples {int(b_v16_rollout_72h.get('closed_samples') or 0)}; report-only, no auto rollback or parameter change."
            if b_v16_rollout.get("available")
            else "B/v16 full-live review not built; use strategy evolution summary only."
        ),
        (
            f"A/v11 trailing复盘：{a_v11_rollout_decision.get('priority')}/{a_v11_rollout_decision.get('status')}，"
            f"72h扣费后PnL {float(a_v11_rollout_72h.get('pnl_after_cost_usdt') or 0):+.2f} USDT，"
            f"平仓样本 {int(a_v11_rollout_72h.get('closed_samples') or 0)}；只提示人工复核，不自动改实盘参数。"
            if a_v11_rollout.get("available")
            else "A/v11 trailing复盘：报告尚未生成；不能只靠进化门禁摘要判断是否回滚。"
        ),
        (
            f"Replay门控：live开仓流 {int(replay_gate_summary_data.get('open_flow_events') or 0)} 条，"
            f"gate覆盖 {float(replay_gate_summary_data.get('gate_coverage_pct') or 0):.1f}%，"
            f"未知门控 {int(replay_gate_summary_data.get('unknown_gate') or 0)}；"
            "这是抽纯策略函数前的口径校验。"
            if replay_gate.get("available")
            else "Replay门控：尚无审计输出；统一 replay/live 同路径仍不能验收。"
        ),
        (
            f"Replay/live 同输入：exact gate cases {int(replay_parity_summary_data.get('gate_cases') or 0)}，"
            f"pass {float(replay_parity_summary_data.get('pass_rate_pct') or 0):.1f}%，"
            f"mismatch {int(replay_parity_summary_data.get('mismatched') or 0)}，"
            f"coverage {float(replay_parity_summary_data.get('exact_case_coverage_pct') or 0):.1f}%；"
            "这是 P0-B 是否能声称同路径的硬审计。"
            if replay_parity.get("available")
            else "Replay/live 同输入：尚无 exact gate-case 审计；不能声称同路径完成。"
        ),
        f"环境：已放开候选 24h regime {regime_summary.get('counts') or {}}，quality {regime_summary.get('quality_counts') or {}}，OPEN_FAILED {int(regime_summary.get('open_failed_24h') or 0)}，CLOSE_FAILED {int(regime_summary.get('close_failed_24h') or 0)}。",
        (
            f"扩样成熟度：已放开 {int(expansion.get('approved_count') or 0)} 个，"
            f"成熟 {int(expansion.get('ready_count') or 0)}，继续收样 {int(expansion.get('maturing_count') or 0)}，"
            f"需暂停复核 {int(expansion.get('pause_count') or 0)}，24h样本缺口 {int(expansion.get('missing_samples_24h') or 0)}。"
        ),
        (
            f"门禁硬化：{gate_hardening.get('status') or 'unknown'}；P0/P1候选 {int(gate_hardening.get('priority_items') or 0)}，"
            f"放开后就绪窗口 {int(gate_hardening.get('post_approval_ready_windows') or 0)}；自动升级/回滚关闭。"
        ),
        "扩样决策：不再全局盲目放宽。A/v11保持稳定，B/v16观察已批准全量候选，C/v14维持当前受控扩样窗口。",
    ]
    bullets = [b for b in bullets if b]

    next_actions: list[str] = []
    if level == "bad":
        next_actions.append("先处理P0/告警/尺寸/强平闭环，不新增策略风险。")
    if positions < 8 and not (alert_count or risk_count or sizing_count):
        next_actions.append("允许B/v16与C/v14按当前规则继续收集样本，不额外加仓位尺寸。")
    if actionable_evo:
        next_actions.append("查看策略进化门禁最高项，确认是否进入灰度或回滚。")
    if a_v11_rollout_decision.get("priority") in {"P0", "P1"}:
        next_actions.append("复核A/v11 trailing rollout窗口；没有人工决策前不改参数。")
    if b_v16_rollout_decision.get("priority") in {"P0", "P1"}:
        next_actions.append("Review B/v16 full-live rollout window; do not rollback or narrow parameters without manual approval.")
    if int(expansion.get("pause_count") or 0):
        next_actions.append("先复核已放开候选质量/关闭闭环，暂停进一步扩样。")
    elif int(expansion.get("maturing_count") or 0):
        next_actions.append("继续按当前受控扩样收集24h/72h样本，不新增风险放宽。")
    if replay_gate.get("available") and float(replay_gate_summary_data.get("gate_coverage_pct") or 0) < 90:
        next_actions.append("优先补齐OPEN_SKIPPED/OPEN_FAILED的stage/layer，保证replay能解释live否决。")
    if replay_parity.get("available"):
        if int(replay_parity_summary_data.get("mismatched") or 0) or int(replay_parity_summary_data.get("errors") or 0):
            next_actions.append("先修 replay/live exact gate-case mismatch，再扩大同路径验收范围。")
        elif int(replay_parity_summary_data.get("gate_cases") or 0) == 0:
            next_actions.append("给 live scanner 持久化 strategy_gate_case，补足同输入 parity 样本。")
    next_actions.append("下一阶段优先建设统一replay和Parquet/DuckDB研究仓，而不是继续手调阈值。")

    return {
        "level": level,
        "headline": headline,
        "evolution_alert": evolution_alert,
        "bullets": bullets,
        "next_actions": next_actions[:4],
        "strategy_decisions": strategy_decisions,
    }


def build_data() -> dict[str, Any]:
    market_review = newest(REPORTS_DIR / "market_review_latest.html", REVIEW_DIR / "market_review_latest.html")
    market_review_md = latest_file(REPORTS_DIR, "market_review_*.md")
    account_snapshot = REVIEW_DIR / "account_snapshot_latest.html"
    research_review = REPORTS_DIR / "research_review_latest.html"
    shadow_report = REPORTS_DIR / "shadow_experiments_latest.html"
    signal_quality = REPORTS_DIR / "signal_quality_latest.html"
    counterfactual_html = COUNTERFACTUAL_HTML
    market_snapshot = newest(
        REPORTS_DIR / "market_snapshot_latest.json",
        SERVER_MIRROR_DIR / "reports" / "market_snapshot_latest.json",
        MEMORY_DIR / "snapshots" / "market_snapshot_latest.json",
    )
    snapshot = read_json(market_snapshot) or {}
    moves = snapshot.get("moves") if isinstance(snapshot, dict) else []
    moves = moves if isinstance(moves, list) else []
    top_move = "-"
    if moves:
        top = max(moves, key=lambda r: abs(float(r.get("change_pct") or 0)))
        top_move = f"{top.get('symbol')} {float(top.get('change_pct') or 0):+.2f}%"
    experiments = read_jsonl(EXPERIMENTS_DIR / "results" / "latest.jsonl")
    reviews = read_jsonl(MEMORY_DIR / "promotions" / "reviews_latest.jsonl")
    approved = sum(1 for r in reviews if r.get("promotion_status") == "approved_candidate")
    signal_quality_md = latest_file(REPORTS_DIR, "signal_quality_*.md")
    event_shadow = event_store_shadow_summary([])
    sqlite_strategies = sqlite_strategy_status() if event_shadow.get("coverage_ok") else []
    sqlite_decision_summary = sqlite_strategy_decision_summary() if event_shadow.get("coverage_ok") else []
    jsonl_strategies = [] if sqlite_strategies else strategy_status()
    jsonl_decision_summary = [] if sqlite_decision_summary else strategy_decision_summary()
    data = {
        "market_review": market_review,
        "market_review_md": market_review_md,
        "account_snapshot": account_snapshot,
        "research_review": research_review,
        "shadow_report": shadow_report,
        "signal_quality": signal_quality,
        "signal_quality_md": signal_quality_md,
        "signal_summary": parse_signal_quality(signal_quality_md),
        "market_snapshot": market_snapshot,
        "snapshot": snapshot,
        "moves": moves,
        "top_move": top_move,
        "experiments": experiments,
        "approved": approved,
        "research_summary": research_overview(reviews, experiments),
        "strategies": sqlite_strategies or jsonl_strategies,
        "decision_summary": sqlite_decision_summary or jsonl_decision_summary,
        "jsonl_strategies": jsonl_strategies,
        "jsonl_decision_summary": jsonl_decision_summary,
        "data_source": "sqlite" if sqlite_strategies and sqlite_decision_summary else "jsonl",
        "counterfactual_html": counterfactual_html,
    }
    data["event_store_shadow"] = event_shadow
    data["market_cache"] = market_cache_summary()
    data["realtime_account"] = realtime_account_summary()
    data["alerts"] = alert_summary()
    data["counterfactual"] = counterfactual_summary(COUNTERFACTUAL_JSON)
    data["strategy_evolution"] = strategy_evolution_summary(STRATEGY_EVOLUTION_JSON)
    data["strategy_evolution_html"] = STRATEGY_EVOLUTION_HTML
    data["attention"] = attention_summary(ATTENTION_JSON)
    data["attention_html"] = ATTENTION_HTML
    data["strategy_truth"] = strategy_truth_summary(STRATEGY_TRUTH_JSON)
    data["strategy_truth_html"] = STRATEGY_TRUTH_MD
    data["sentinel_quality"] = sentinel_quality_summary(SENTINEL_QUALITY_JSON)
    data["sentinel_quality_html"] = SENTINEL_QUALITY_MD
    data["research_store"] = research_store_summary(RESEARCH_STORE_JSON)
    data["research_store_html"] = RESEARCH_STORE_MD
    data["replay_feature"] = replay_feature_summary(REPLAY_FEATURE_JSON)
    data["replay_feature_html"] = REPLAY_FEATURE_MD
    data["replay_gate"] = replay_gate_summary(REPLAY_GATE_JSON)
    data["replay_gate_html"] = REPLAY_GATE_MD
    data["replay_parity"] = replay_parity_summary(REPLAY_PARITY_JSON)
    data["replay_parity_html"] = REPLAY_PARITY_MD
    data["rollback_watch"] = rollback_watch_summary(ROLLBACK_WATCH_JSON)
    data["rollback_watch_html"] = ROLLBACK_WATCH_MD
    data["a_v11_rollout"] = a_v11_rollout_summary(A_V11_ROLLOUT_JSON)
    data["a_v11_rollout_html"] = A_V11_ROLLOUT_MD
    data["b_v16_rollout"] = b_v16_rollout_summary(B_V16_ROLLOUT_JSON)
    data["b_v16_rollout_html"] = B_V16_ROLLOUT_MD
    data["function_status"] = function_status_cards(data)
    data["findings"] = build_findings(data)
    data["executive_summary"] = build_executive_summary(data)
    return data


def status_pill(ok: bool, text: str) -> str:
    cls = "ok" if ok else "warn"
    return f'<span class="pill {cls}">{h(text)}</span>'


def route_card(title: str, when: str, why: str, path: Path | None, action: str, accent: str) -> str:
    return f"""
<article class="route-card {accent}">
  <div class="route-head">
    <span>{h(when)}</span>
    <b>{h(file_time(path))}</b>
  </div>
  <h3>{h(title)}</h3>
  <p>{h(why)}</p>
  <a href="{h(as_url(path))}">{h(action)}<span>›</span></a>
</article>
""".strip()


def render_html(out_dir: Path) -> str:
    data = build_data()
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    alert_services = (((data.get("alerts") or {}).get("services") or {}) if isinstance(data.get("alerts"), dict) else {})
    scanner_services = {
        "crypto-scanner.service",
        "crypto-scanner-v16.service",
        "crypto-scanner-v14.service",
    }
    scanner_service_states = {
        name: state for name, state in alert_services.items()
        if name in scanner_services
    }
    all_ok = (
        bool(scanner_service_states)
        and all(str(state) == "active" for state in scanner_service_states.values())
    ) or (not scanner_service_states and all(s["ok"] for s in data["strategies"]))
    account_fresh = data["account_snapshot"].exists()
    binance_ok = account_fresh
    realtime_account = data.get("realtime_account") or {}

    a_interval = next((s["interval"] for s in data["strategies"] if s["name"] == "A/v11"), "暂无")
    b_interval = next((s["interval"] for s in data["strategies"] if s["name"] == "B/v16"), "暂无")
    c_interval = next((s["interval"] for s in data["strategies"] if s["name"] == "C/v14"), "暂无")
    live_pnl = sum(float(r.get("pnl") or 0) for r in data.get("decision_summary", []))
    live_trades = sum(int(r.get("closed") or 0) for r in data.get("decision_summary", []))
    account_upnl = float(realtime_account.get("unrealized_pnl_usdt") or 0)
    account_positions = int(realtime_account.get("open_positions") or 0)
    attention = data.get("attention") or {}
    attention_summary_data = attention.get("summary") or {}
    attention_counts = attention_summary_data.get("counts") or {}
    issues_count = sum(1 for f in data.get("findings", []) if f.get("level") in {"bad", "warn"})
    executive = data.get("executive_summary") or {}
    evolution_alert = executive.get("evolution_alert") or {}
    metrics = [
        ("昨日复盘PnL", f"{data['signal_summary']['total_pnl']:+.2f}", "最新完整复盘周期"),
        (
            "实时账户浮盈亏",
            f"{account_upnl:+.2f}" if realtime_account.get("available") else "暂无",
            f"持仓 {account_positions} / 快照 {realtime_account.get('age', '无')}",
        ),
        (
            "策略运行",
            "正常" if all_ok else "需检查",
            "systemd服务状态" if scanner_service_states else "三套服务心跳/扫描统计",
        ),
        (
            "持久关注",
            f"P0 {int(attention_counts.get('P0') or 0)} / P1 {int(attention_counts.get('P1') or 0)}"
            if attention.get("available") else str(issues_count),
            f"open {int(attention_summary_data.get('open') or 0)} / 待复核 {int(attention_summary_data.get('cleared_pending_review') or 0)}",
        ),
        ("数据口径", "SQLite" if data.get("data_source") == "sqlite" else "JSONL", "首页策略/信号摘要"),
    ]
    metric_html = "".join(
        f"<article><span>{h(k)}</span><b>{h(v)}</b><em>{h(desc)}</em></article>" for k, v, desc in metrics
    )
    executive_bullets = "".join(f"<li>{h(item)}</li>" for item in executive.get("bullets", []))
    executive_actions = "".join(f"<li>{h(item)}</li>" for item in executive.get("next_actions", []))
    evolution_alert_html = f"""
    <div class="priority-alert {h(evolution_alert.get('level', 'warn'))}">
      <b>{h(evolution_alert.get('title', 'P2 暂无最高优先进化事项'))}</b>
      <span>{h(evolution_alert.get('body', '等待策略进化门禁输出。'))}</span>
    </div>
    """.strip()
    executive_strategy_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('name'))}</td>
  <td><b>{h(r.get('decision'))}</b></td>
  <td class="num {'pos' if str(r.get('pnl', '')).startswith('+') else 'neg'}">{h(r.get('pnl'))}</td>
  <td>{h(r.get('pf'))}</td>
  <td>{h(r.get('opens'))}</td>
  <td>{h(r.get('last_open'))}</td>
  <td>{h(r.get('reason'))}</td>
</tr>
""".strip()
        for r in executive.get("strategy_decisions", [])
    ) or '<tr><td colspan="7">暂无策略决策摘要</td></tr>'

    decision_rows = "".join(
        f"""
<tr>
  <td>{h(r['name'])}</td>
  <td class="num {'pos' if r['pnl'] >= 0 else 'neg'}">{r['pnl']:+.2f}</td>
  <td>{h(r['closed'])}</td>
  <td>{r['win_rate']:.1f}%</td>
  <td>{h(r['signals'])}{f"<br><small>原始 {h(r.get('raw_signals'))}</small>" if r.get('raw_signals') and r.get('raw_signals') != r.get('signals') else ""}</td>
  <td>{h(r['opens'])}</td>
  <td>{h(r['positions'])}</td>
  <td>{h(r['sentinel'])}</td>
  <td>{h(r.get('last_open', '无开仓'))}</td>
  <td>{h(r['age'])}</td>
  <td>{h(r['notes'])}</td>
</tr>
""".strip()
        for r in data.get("decision_summary", [])
    ) or '<tr><td colspan="11">暂无策略决策摘要</td></tr>'

    optimize_rows = "".join(
        f"""
<tr>
  <td>{h(r['name'])}</td>
  <td>{h(r['notes'])}</td>
  <td>{h(r['worst'])}</td>
  <td>{h(r['best'])}</td>
  <td>{h(r['confirm_filtered'])}</td>
  <td>{h(r['score_rejected'])}</td>
  <td>{h(r['http400'])}</td>
</tr>
""".strip()
        for r in data.get("decision_summary", [])
    ) or '<tr><td colspan="7">暂无优化摘要</td></tr>'

    function_cards = "".join(
        f"""
<article class="status-card {h(card['level'])}">
  <span>{h(card['name'])}</span>
  <b>{h(card['value'])}</b>
  <em>{h(card['body'])}</em>
</article>
""".strip()
        for card in data.get("function_status", [])
    )

    # Strategy quality board from truth ledger
    truth = data.get("strategy_truth") or {}
    truth_summary = truth.get("summary") or {}
    truth_stats = truth.get("strategy_stats") or {}
    truth_recovery = truth.get("recovery_stats") or {}
    quality_rows_parts = []
    for strategy in ["A/v11", "B/v16", "C/v14"]:
        ts = truth_stats.get(strategy) or {}
        tr = truth_recovery.get(strategy) or {}
        closed = ts.get("closed_trades", 0)
        wr = ts.get("win_rate", 0)
        net_pnl = float(ts.get("net_pnl_usd", 0))
        pf = ts.get("profit_factor", 0)
        payoff = ts.get("payoff_ratio", 0)
        hs = ts.get("hard_stop_count", 0)
        rec_count = tr.get("count", 0)
        rec_upnl = float(tr.get("total_unrealized_pnl", 0))
        quality_rows_parts.append(
            f'<tr><td>{h(strategy)}</td><td>{closed}</td><td>{wr}%</td>'
            f'<td class="num {"pos" if net_pnl >= 0 else "neg"}">{net_pnl:+.2f}</td>'
            f'<td>{pf}</td><td>{payoff}</td><td>{hs}</td><td>{rec_count}</td>'
            f'<td class="num {"pos" if rec_upnl >= 0 else "neg"}">{rec_upnl:+.2f}</td></tr>'
        )
    quality_rows = "".join(quality_rows_parts) or '<tr><td colspan="9">暂无真相台账数据</td></tr>'
    recovery_review = truth.get("recovery_review") or {}
    recovery_strategy_exit = truth.get("recovery_strategy_exit_evidence") or {}
    recovery_risk = recovery_review.get("risk_counts") or {}
    recovery_positions = recovery_review.get("positions") or []
    recovery_review_rows = "".join(
        f"""
<tr>
  <td>{h(pos.get('strategy'))}</td>
  <td>{h(pos.get('symbol'))}</td>
  <td>{h(pos.get('side'))}</td>
  <td>{float(pos.get('age_hours') or 0):.2f}</td>
  <td class="num {'pos' if float(pos.get('unrealized_pnl_usdt') or 0) >= 0 else 'neg'}">{float(pos.get('unrealized_pnl_usdt') or 0):+.2f}</td>
  <td>{float(pos.get('unrealized_pnl_pct_on_margin') or 0):+.2f}%</td>
  <td>{float(pos.get('mfe_pct_on_margin') or 0):+.2f}%</td>
  <td>{float(pos.get('mae_pct_on_margin') or 0):+.2f}%</td>
  <td>{float(pos.get('drawdown_from_mfe_pct_on_margin') or 0):+.2f}%</td>
  <td>{int(pos.get('same_strategy_open_like_count') or 0)}</td>
  <td>{int(pos.get('opposite_open_like_count') or 0)}</td>
  <td>{h(pos.get('signal_shadow_action'))}</td>
  <td>{h(pos.get('strategy_exit_action'))}</td>
  <td>{h(pos.get('recovery_replay_action'))}:{h(pos.get('recovery_replay_exit_reason') or pos.get('recovery_replay_status'))}</td>
  <td>{h(pos.get('risk'))}</td>
  <td>{h(pos.get('shadow_action'))}</td>
</tr>
""".strip()
        for pos in recovery_positions[:10]
    ) or '<tr><td colspan="16">当前无恢复仓</td></tr>'
    recovery_signal = recovery_review.get("signal_counts") or {}
    recovery_replay = truth.get("recovery_bar_replay_evidence") or {}
    recovery_replay_counts = recovery_replay.get("action_counts") or recovery_review.get("replay_counts") or {}
    recovery_total_upnl = float(
        truth_summary.get("total_recovery_unrealized_pnl_usdt")
        or truth_summary.get("total_recovery_unrealized_pnl_usd")
        or 0
    )

    account_rows = "".join(
        f"""
<tr>
  <td>{h(r['strategy'])}</td>
  <td class="num {'pos' if r['unrealized_pnl_usdt'] >= 0 else 'neg'}">{r['unrealized_pnl_usdt']:+.2f}</td>
  <td>{r['open_positions']}</td>
  <td>{r['longs']} 多 / {r['shorts']} 空</td>
  <td>{r['wallet_usdt']:.2f}</td>
  <td>{r['available_usdt']:.2f}</td>
  <td>{r['notional_usdt']:.2f}</td>
  <td>{r['risk_count']}</td>
  <td>{r['sizing_violation_count']}</td>
  <td>{h(age_text(r.get('ts')))}</td>
</tr>
""".strip()
        for r in realtime_account.get("accounts", [])
    ) or '<tr><td colspan="10">暂无实时账户快照</td></tr>'

    event_store = data.get("event_store_shadow") or {}
    event_store_rows = "".join(
        f"""
<tr>
  <td>{h(r['name'])}</td>
  <td>{h(r['events'])}</td>
  <td>{h(r['decisions'])}</td>
  <td>{h(r['signals'])}</td>
  <td>{h(r['system'])}</td>
  <td>{h(r['jsonl_signals'])}</td>
  <td>{h(r['jsonl_sentinel'])}</td>
  <td>{h(r['latest_id'])}</td>
</tr>
""".strip()
        for r in event_store.get("strategy_rows", [])
    ) or '<tr><td colspan="8">暂无 SQLite 策略覆盖数据</td></tr>'
    event_source_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('source'))}</td>
  <td>{h(r.get('strategy'))}</td>
  <td>{h(r.get('event_type'))}</td>
  <td>{h(r.get('category'))}</td>
  <td>{h(r.get('n'))}</td>
</tr>
""".strip()
        for r in event_store.get("sources", [])[:12]
    ) or '<tr><td colspan="5">暂无 SQLite 来源分布</td></tr>'

    pnl_rows = "".join(
        f"""
<tr>
  <td>{h(r['name'])}</td>
  <td>{h(r['signals'])}</td>
  <td>{h(r['opens'])}</td>
  <td>{h(r['closes'])}</td>
  <td>{h(r['win_rate'])}</td>
  <td class="num {'pos' if r['pnl'] >= 0 else 'neg'}">{r['pnl']:+.2f}</td>
  <td>{h(r['hard_rate'])}</td>
</tr>
""".strip()
        for r in data["signal_summary"].get("strategies", [])
    ) or '<tr><td colspan="7">暂无信号质量摘要</td></tr>'

    sentinel_rows = "".join(
        f"""
<tr>
  <td>{h(r['name'])}</td>
  <td>{h(r['decisions'])}</td>
  <td>{h(compact_text(r['results'], 120))}</td>
  <td>{h(compact_text(r['layers'], 80))}</td>
  <td>{h(compact_text(r['symbols'], 90))}</td>
</tr>
""".strip()
        for r in data["signal_summary"].get("sentinel", [])
    ) or '<tr><td colspan="5">暂无哨兵摘要</td></tr>'

    sentinel_quality = data.get("sentinel_quality") or {}
    sentinel_coverage = sentinel_quality.get("coverage") or {}
    sentinel_watchlist = sentinel_quality.get("watchlist_history") or {}
    sentinel_forward_rows = "".join(
        f"""
<tr>
  <td>{h(label)}</td>
  <td>{int((row.get('directional_after_fee') or {}).get('samples') or 0)}</td>
  <td>{float((row.get('raw') or {}).get('avg_pct') or 0):+.4f}%</td>
  <td>{float((row.get('directional_after_fee') or {}).get('avg_pct') or 0):+.4f}%</td>
  <td>{float((row.get('directional_after_fee') or {}).get('win_rate_pct') or 0):.2f}%</td>
</tr>
""".strip()
        for label, row in ((sentinel_quality.get("forward_returns") or {}).get("by_horizon") or {}).items()
    ) or '<tr><td colspan="5">暂无哨兵前向收益审计</td></tr>'
    sentinel_missed_rows = "".join(
        f"""
<tr>
  <td>{h(row.get('symbol'))}</td>
  <td>{float(row.get('change_pct') or 0):+.2f}%</td>
  <td>{float(row.get('velocity_pct') or 0):+.2f}%</td>
  <td>{float(row.get('quote_volume') or 0):.0f}</td>
  <td>{h(str(row.get('ts') or '')[:16])}</td>
</tr>
""".strip()
        for row in (sentinel_coverage.get("missed_examples") or [])[:10]
    ) or '<tr><td colspan="5">暂无未覆盖大行情样例</td></tr>'
    sentinel_attribution_rows = "".join(
        f"""
<tr>
  <td>{h(row.get('label') or row.get('bucket'))}</td>
  <td>{int(row.get('count') or 0)}</td>
  <td>{float(row.get('pct') or 0):.2f}%</td>
  <td>{h(', '.join(str(ex.get('symbol') or '') for ex in (row.get('examples') or [])[:4]))}</td>
</tr>
""".strip()
        for row in ((sentinel_coverage.get("attribution") or {}).get("buckets") or [])[:8]
    ) or '<tr><td colspan="4">暂无大行情归因审计</td></tr>'
    sentinel_not_scanned_rows = "".join(
        f"""
<tr>
  <td>{h(row.get('label') or row.get('bucket'))}</td>
  <td>{int(row.get('count') or 0)}</td>
  <td>{float(row.get('pct_of_big_moves') or 0):.2f}%</td>
  <td>{float(row.get('pct_of_not_scanned') or 0):.2f}%</td>
  <td>{h(', '.join(str(ex.get('symbol') or '') for ex in (row.get('examples') or [])[:4]))}</td>
</tr>
""".strip()
        for row in ((sentinel_coverage.get("attribution") or {}).get("not_scanned_breakdown") or [])[:6]
    ) or '<tr><td colspan="5">暂无未扫描细分</td></tr>'
    sentinel_watchlist_note = (
        f"Watchlist 历史：{int(sentinel_watchlist.get('snapshots') or 0)} 个快照，"
        f"{int(sentinel_watchlist.get('unique_symbols') or 0)} 个去重币种，"
        f"{h(str(sentinel_watchlist.get('first_ts') or '')[:16])} ~ {h(str(sentinel_watchlist.get('last_ts') or '')[:16])}。"
        if sentinel_watchlist.get("available")
        else "Watchlist 历史：暂无 durable snapshot；本次上线后开始采集，用于后续区分未进 watchlist 与镜像/扫描缺口。"
    )

    counterfactual = data.get("counterfactual") or {}
    cf_overall = counterfactual.get("overall") or {}
    cf_fill = counterfactual.get("replay_fill") or (cf_overall.get("replay_fill") or {})
    cf_liquidity = counterfactual.get("fill_liquidity") or {}
    cf_liquidity_enabled = (
        cf_liquidity.get("max_fill_quantity") is not None
        or cf_liquidity.get("max_fill_notional_usdt") is not None
        or cf_liquidity.get("allow_partial_fill") is False
    )
    cf_liquidity_note = (
        "流动性限制："
        f"max qty {h(cf_liquidity.get('max_fill_quantity') if cf_liquidity.get('max_fill_quantity') is not None else '-')}, "
        f"max notional {h(cf_liquidity.get('max_fill_notional_usdt') if cf_liquidity.get('max_fill_notional_usdt') is not None else '-')} USDT, "
        f"partial {'allowed' if cf_liquidity.get('allow_partial_fill', True) else 'rejected'}。"
        if cf_liquidity_enabled
        else "流动性限制未启用：默认按目标仓位完整成交。"
    )
    counterfactual_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('name'))}</td>
  <td>{int(r.get('samples') or 0)}</td>
  <td>{float(r.get('win_rate') or 0):.2f}%</td>
  <td class="num {'pos' if float(r.get('pnl') or 0) >= 0 else 'neg'}">{float(r.get('pnl') or 0):+.2f}</td>
  <td>{float(r.get('avg_mfe') or 0):.2f}%</td>
  <td>{float(r.get('avg_mae') or 0):.2f}%</td>
</tr>
""".strip()
        for r in counterfactual.get("strategies", [])
    ) or '<tr><td colspan="6">暂无反事实样本</td></tr>'
    counterfactual_filter_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('strategy'))}</td>
  <td>{h(r.get('filter'))}</td>
  <td>{int(r.get('samples') or 0)}</td>
  <td>{float(r.get('win_rate') or 0):.2f}%</td>
  <td class="num {'pos' if float(r.get('pnl') or 0) >= 0 else 'neg'}">{float(r.get('pnl') or 0):+.2f}</td>
</tr>
""".strip()
        for r in counterfactual.get("filters", [])[:6]
    ) or '<tr><td colspan="5">暂无过滤层评估</td></tr>'
    counterfactual_fill_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('exit_model'))}</td>
  <td>{int(r.get('samples') or 0)}</td>
  <td>{float(r.get('win_rate') or 0):.2f}%</td>
  <td class="num {'pos' if float(r.get('gross_pnl_usdt') or 0) >= 0 else 'neg'}">{float(r.get('gross_pnl_usdt') or 0):+.2f}</td>
  <td>{float(r.get('fee_usdt') or 0):.2f}</td>
  <td>{float(r.get('slippage_usdt') or 0):.2f}</td>
  <td class="num {'pos' if float(r.get('net_pnl_usdt') or 0) >= 0 else 'neg'}">{float(r.get('net_pnl_usdt') or 0):+.2f}</td>
  <td>{int(r.get('partial_fill_count') or 0)}</td>
  <td>{float(r.get('avg_fill_ratio') or 0):.3f}</td>
  <td>{float(r.get('avg_bars_held') or 0):.2f}</td>
</tr>
""".strip()
        for r in (cf_fill.get("by_exit_model") or [])[:6]
    ) or '<tr><td colspan="10">暂无 replay/fill 出场模型汇总</td></tr>'
    cf_exit_reasons = "；".join(
        f"{row.get('name')}={int(row.get('count') or 0)}"
        for row in (cf_fill.get("exit_reason_counts") or [])[:5]
    ) or "暂无"

    research_store = data.get("research_store") or {}
    research_funnel_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('strategy'))}</td>
  <td>{int(r.get('events') or 0)}</td>
  <td>{int(r.get('signals') or 0)}</td>
  <td>{int(r.get('opens') or 0)}</td>
  <td>{int(r.get('open_skipped') or 0)}</td>
  <td>{int(r.get('open_failed') or 0)}</td>
  <td>{h(r.get('latest_ts') or '-')}</td>
</tr>
""".strip()
        for r in (research_store.get("strategy_funnel") or [])[:6]
    ) or '<tr><td colspan="7">暂无研究仓漏斗</td></tr>'
    research_skip_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('strategy'))}</td>
  <td>{h(r.get('gate'))}</td>
  <td>{int(r.get('n') or 0)}</td>
</tr>
""".strip()
        for r in (research_store.get("skip_layers") or [])[:8]
    ) or '<tr><td colspan="3">暂无研究仓否决层</td></tr>'
    research_kline_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('interval'))}</td>
  <td>{int(r.get('bars') or 0)}</td>
  <td>{int(r.get('symbols') or 0)}</td>
  <td>{h(r.get('latest_bar') or '-')}</td>
</tr>
""".strip()
        for r in (research_store.get("kline_coverage") or [])[:6]
    ) or '<tr><td colspan="4">暂无 K线研究分区</td></tr>'
    research_feature_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('interval'))}</td>
  <td>{int(r.get('rows') or 0)}</td>
  <td>{int(r.get('symbols') or 0)}</td>
  <td>{float(r.get('avg_abs_return_1_pct') or 0):.4f}%</td>
  <td>{float(r.get('avg_range_pct') or 0):.4f}%</td>
  <td>{h(r.get('latest_bar') or '-')}</td>
</tr>
""".strip()
        for r in (research_store.get("feature_coverage") or [])[:6]
    ) or '<tr><td colspan="6">暂无特征研究分区</td></tr>'
    replay_gate = data.get("replay_gate") or {}
    replay_gate_summary_data = replay_gate.get("summary") or {}
    replay_gate_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('strategy'))}</td>
  <td>{int(r.get('open_flow_events') or 0)}</td>
  <td>{int(r.get('accepted_opens') or 0)}</td>
  <td>{int(r.get('signals') or 0)}</td>
  <td>{int(r.get('rejected') or 0)}</td>
  <td>{int(r.get('execution_failed') or 0)}</td>
  <td>{int(r.get('unknown_gate') or 0)}</td>
  <td>{float(r.get('gate_coverage_pct') or 0):.1f}%</td>
  <td>{h(', '.join(f"{g.get('name')}:{g.get('count')}" for g in (r.get('top_gates') or [])[:5]) or '-')}</td>
</tr>
""".strip()
        for r in (replay_gate.get("strategies") or [])
    ) or '<tr><td colspan="9">暂无 replay gate 审计</td></tr>'
    replay_parity = data.get("replay_parity") or {}
    replay_parity_summary_data = replay_parity.get("summary") or {}
    replay_parity_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('strategy'))}</td>
  <td>{int(r.get('open_flow_rows') or 0)}</td>
  <td>{int(r.get('rows_with_exact_cases') or 0)}</td>
  <td>{int(r.get('missing_case_rows') or 0)}</td>
  <td>{int(r.get('gate_cases') or 0)}</td>
  <td>{int(r.get('passed') or 0)}</td>
  <td>{int(r.get('mismatched') or 0)}</td>
  <td>{int(r.get('errors') or 0)}</td>
  <td>{float(r.get('pass_rate_pct') or 0):.1f}%</td>
  <td>{h(', '.join(f"{g.get('name')}:{g.get('count')}" for g in (r.get('top_gates') or [])[:5]) or '-')}</td>
</tr>
""".strip()
        for r in (replay_parity.get("strategies") or [])
    ) or '<tr><td colspan="10">暂无 replay/live 同输入审计</td></tr>'

    a_v11_rollout = data.get("a_v11_rollout") or {}
    a_v11_rollout_decision = a_v11_rollout.get("decision") or {}
    a_v11_rollout_windows = a_v11_rollout.get("windows") or {}
    a_v11_rollout_rows = "".join(
        f"""
<tr>
  <td>{h(name)}</td>
  <td>{int(row.get('opens') or 0)}</td>
  <td>{int(row.get('closed_samples') or 0)}</td>
  <td>{int(row.get('forced_closes') or 0)}</td>
  <td>{int(row.get('open_failed') or 0)}</td>
  <td class="num {'pos' if float(row.get('realized_pnl_usdt') or 0) >= 0 else 'neg'}">{float(row.get('realized_pnl_usdt') or 0):+.2f}</td>
  <td>{float(row.get('estimated_cost_usdt') or 0):.2f}</td>
  <td class="num {'pos' if float(row.get('pnl_after_cost_usdt') or 0) >= 0 else 'neg'}">{float(row.get('pnl_after_cost_usdt') or 0):+.2f}</td>
  <td>{float(row.get('forced_close_rate') or 0):.1%}</td>
</tr>
""".strip()
        for name, row in a_v11_rollout_windows.items()
    ) or '<tr><td colspan="9">暂无 A/v11 rollout 复盘</td></tr>'

    b_v16_rollout = data.get("b_v16_rollout") or {}
    b_v16_rollout_decision = b_v16_rollout.get("decision") or {}
    b_v16_rollout_windows = b_v16_rollout.get("windows") or {}
    b_v16_rollout_rows = "".join(
        f"""
<tr>
  <td>{h(name)}</td>
  <td>{int(row.get('opens') or 0)}</td>
  <td>{int(row.get('closed_samples') or 0)}</td>
  <td>{int(row.get('forced_closes') or 0)}</td>
  <td>{int(row.get('open_failed') or 0)}</td>
  <td class="num {'pos' if float(row.get('realized_pnl_usdt') or 0) >= 0 else 'neg'}">{float(row.get('realized_pnl_usdt') or 0):+.2f}</td>
  <td>{float(row.get('estimated_cost_usdt') or 0):.2f}</td>
  <td class="num {'pos' if float(row.get('pnl_after_cost_usdt') or 0) >= 0 else 'neg'}">{float(row.get('pnl_after_cost_usdt') or 0):+.2f}</td>
  <td>{float(row.get('forced_close_rate') or 0):.1%}</td>
</tr>
""".strip()
        for name, row in b_v16_rollout_windows.items()
    ) or '<tr><td colspan="9">No B/v16 rollout review</td></tr>'

    evolution = data.get("strategy_evolution") or {}
    evolution_counts = evolution.get("counts") or {}
    gate_hardening = evolution.get("promotion_gate_hardening") or {}
    evolution_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('priority'))}</td>
  <td>{h(r.get('status'))}</td>
  <td>{h(r.get('strategy'))}</td>
  <td>{h(r.get('candidate_id'))}</td>
  <td>{int(r.get('evidence_score') or 0)}</td>
  <td>{int(r.get('risk_score') or 0)}</td>
  <td>{h(r.get('recommended_action'))}</td>
  <td>{h('; '.join(r.get('blockers') or []) or '-')}</td>
</tr>
""".strip()
        for r in (evolution.get("decisions") or [])[:8]
    ) or '<tr><td colspan="8">暂无策略进化门禁结论</td></tr>'

    attention_rows = "".join(
        f"""
<tr>
  <td>{h(r.get('priority'))}</td>
  <td>{h(r.get('status'))}</td>
  <td>{h(r.get('category'))}</td>
  <td>{h(r.get('title'))}</td>
  <td>{h(compact_text(r.get('evidence') or '-', 130))}</td>
  <td>{h(compact_text(r.get('recommended_action') or '-', 130))}</td>
  <td><button class="ack-btn" data-id="{h(r.get('item_id'))}" onclick="ackItem(this)">确认</button></td>
</tr>
""".strip()
        for r in (attention.get("items") or [])[:8]
    ) or '<tr><td colspan="7">暂无持久关注事项</td></tr>'

    findings_html = "".join(
        f"""
<article class="finding {h(item['level'])}">
  <b>{h(item['title'])}</b>
  <span>{h(item['body'])}</span>
</article>
""".strip()
        for item in data.get("findings", [])[:6]
    )

    routes = [
        route_card(
            "盘中先看",
            "现在是否安全",
            "看账户、持仓、浮盈亏和硬止损风险。只想确认有没有危险，先点这里。",
            data["account_snapshot"],
            "看账号快照",
            "green",
        ),
        route_card(
            "每日复盘",
            "复盘行情",
            "看市场变化、交易明细、策略卡点和大行情捕捉结果。",
            data["market_review"],
            "看每日复盘",
            "red",
        ),
        route_card(
            "信号到底少不少",
            "检查过滤",
            "看信号、跳过、确认失败、阈值失败。简单说，就是看机会在哪一关没过去。",
            data["signal_quality"],
            "看信号质量",
            "slate",
        ),
        route_card(
            "否决是否错杀",
            "反事实评估",
            "看 OPEN_SKIPPED 若放行后的模拟收益，判断哪一层应保留、放宽或重做。",
            data["counterfactual_html"],
            "看反事实评估",
            "amber",
        ),
        route_card(
            "样本是否够用",
            "研究仓漏斗",
            "看 DuckDB/Parquet 汇总后的开仓、跳过、失败、哨兵贡献，用来判断策略进化样本是否足够。",
            data["research_store_html"],
            "看研究仓摘要",
            "blue",
        ),
        route_card(
            "是否有更优方案",
            "统一进化门禁",
            "看系统是否已经把候选、影子实验、反事实、账户风险合成可决策升级机会。",
            data["strategy_evolution_html"],
            "看进化门禁",
            "amber",
        ),
        route_card(
            "长期关注事项",
            "不会随日报消失",
            "看所有还没被你确认关闭的问题、机会和异常，包含已消失但待复核的事项。",
            data["attention_html"],
            "看关注台账",
            "red",
        ),
        route_card(
            "想改策略前",
            "先别直接上实盘",
            "看影子实验。影子实验就是先用历史/同步数据试跑一下，不影响真实交易。",
            data["shadow_report"],
            "看影子实验",
            "amber",
        ),
        route_card(
            "有候选改动时",
            "人工审批",
            "看哪些候选通过了门槛。门槛就是系统给改动设的最低要求。",
            data["research_review"],
            "看研究审阅台",
            "blue",
        ),
    ]

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoTrading 指挥台</title>
<style>
:root {{
  --bg:#f3f6fb; --panel:#fff; --text:#172033; --muted:#667085; --line:#d7e0ec;
  --green:#16a34a; --red:#dc2626; --blue:#2563eb; --amber:#b45309; --slate:#475569;
  --soft-green:#ecfdf3; --soft-red:#fff1f2; --soft-blue:#eff6ff; --soft-amber:#fffbeb; --soft-slate:#f1f5f9;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif; }}
a {{ color:inherit; }}
.shell {{ max-width:1440px; margin:0 auto; padding:24px; }}
.hero {{ display:grid; grid-template-columns:1fr auto; gap:20px; align-items:end; padding:8px 0 18px; border-bottom:1px solid var(--line); }}
h1 {{ margin:0; font-size:30px; letter-spacing:0; }}
.hero p {{ margin:8px 0 0; color:var(--muted); line-height:1.65; }}
.time {{ color:var(--muted); font-size:13px; white-space:nowrap; }}
.summary {{ display:grid; grid-template-columns:1.1fr .9fr; gap:14px; margin:18px 0; }}
.brief,.next,.panel,.route-card,.metrics article {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
.brief {{ padding:18px; border-left:5px solid var(--green); }}
.brief.warn {{ border-left-color:var(--amber); }}
.brief h2,.next h2,.panel h2 {{ margin:0 0 10px; font-size:18px; }}
.brief p,.next p {{ margin:8px 0; color:#344054; line-height:1.7; }}
.next {{ padding:18px; }}
.next ol {{ margin:0; padding-left:20px; color:#344054; line-height:1.8; }}
.metrics {{ display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin:18px 0; }}
.metrics article {{ padding:14px; min-height:92px; }}
.metrics span,.metrics em {{ display:block; color:var(--muted); font-size:12px; font-style:normal; }}
.metrics b {{ display:block; margin:8px 0; font-size:24px; color:#0f172a; }}
.decision-brief {{ background:var(--panel); border:1px solid var(--line); border-left:6px solid var(--green); border-radius:8px; padding:18px; margin:18px 0; }}
.decision-brief.warn {{ border-left-color:var(--amber); }}
.decision-brief.bad {{ border-left-color:var(--red); }}
.decision-grid {{ display:grid; grid-template-columns:1fr .86fr; gap:16px; }}
.decision-brief h2 {{ margin:0 0 8px; font-size:21px; }}
.decision-brief h3 {{ margin:14px 0 8px; font-size:14px; color:#334155; }}
.decision-brief ul,.decision-brief ol {{ margin:8px 0 0; padding-left:20px; color:#344054; line-height:1.65; }}
.decision-brief table {{ margin-top:14px; min-width:760px; }}
.priority-alert {{ margin:0 0 14px; border:1px solid var(--line); border-left:6px solid var(--slate); border-radius:8px; padding:12px 14px; background:#f8fafc; }}
.priority-alert.ok {{ border-left-color:var(--green); background:var(--soft-green); }}
.priority-alert.warn {{ border-left-color:var(--amber); background:var(--soft-amber); }}
.priority-alert.bad {{ border-left-color:var(--red); background:#fef2f2; }}
.priority-alert b,.priority-alert span {{ display:block; }}
.priority-alert span {{ margin-top:5px; color:#344054; line-height:1.55; }}
.section {{ margin-top:22px; }}
.section-head {{ display:flex; justify-content:space-between; align-items:end; gap:12px; margin-bottom:10px; }}
.section h2 {{ margin:0; font-size:18px; }}
.section small {{ color:var(--muted); }}
.routes {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
.route-card {{ padding:16px; min-height:230px; display:flex; flex-direction:column; border-top:4px solid var(--blue); }}
.route-card.green {{ border-top-color:var(--green); }}
.route-card.red {{ border-top-color:var(--red); }}
.route-card.amber {{ border-top-color:var(--amber); }}
.route-card.slate {{ border-top-color:var(--slate); }}
.route-head {{ display:flex; justify-content:space-between; gap:10px; color:var(--muted); font-size:12px; }}
.route-card h3 {{ margin:14px 0 8px; font-size:20px; }}
.route-card p {{ margin:0; color:#344054; line-height:1.6; }}
.route-card a {{ margin-top:auto; display:flex; justify-content:space-between; align-items:center; text-decoration:none; border:1px solid var(--line); border-radius:8px; padding:10px 12px; font-weight:800; background:#f8fafc; }}
.route-card a:hover {{ background:#fff; border-color:#94a3b8; }}
.panel {{ padding:16px; overflow:auto; }}
.status-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }}
.status-card {{ background:var(--panel); border:1px solid var(--line); border-top:4px solid var(--slate); border-radius:8px; padding:14px; min-height:118px; }}
.status-card.ok {{ border-top-color:var(--green); }}
.status-card.warn {{ border-top-color:var(--amber); }}
.status-card.bad {{ border-top-color:var(--red); }}
.status-card span,.status-card em {{ display:block; color:var(--muted); font-size:12px; font-style:normal; line-height:1.5; }}
.status-card b {{ display:block; margin:8px 0; font-size:22px; color:#0f172a; }}
.findings {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:18px 0; }}
.finding {{ background:var(--panel); border:1px solid var(--line); border-left:5px solid var(--slate); border-radius:8px; padding:13px; min-height:96px; }}
.finding.ok {{ border-left-color:var(--green); }}
.finding.warn {{ border-left-color:var(--amber); }}
.finding.bad {{ border-left-color:var(--red); }}
.finding b,.finding span {{ display:block; }}
.finding span {{ margin-top:7px; color:#475467; line-height:1.55; font-size:13px; }}
table {{ width:100%; border-collapse:collapse; min-width:820px; }}
.subtable {{ margin-top:14px; }}
th,td {{ padding:10px 11px; border-bottom:1px solid #e7edf6; text-align:left; font-size:13px; }}
th {{ background:#f1f5f9; color:#334155; }}
.num {{ font-weight:800; }}
.num.pos {{ color:#15803d; }}
.num.neg {{ color:#b91c1c; }}
.pill {{ display:inline-flex; border-radius:999px; padding:4px 8px; font-size:12px; font-weight:800; }}
.pill.ok {{ color:#166534; background:var(--soft-green); }}
.pill.warn {{ color:#92400e; background:var(--soft-amber); }}
@media (max-width:1180px) {{ .routes {{ grid-template-columns:repeat(2,1fr); }} .metrics,.findings,.status-grid {{ grid-template-columns:repeat(2,1fr); }} .summary,.decision-grid {{ grid-template-columns:1fr; }} }}
@media (max-width:700px) {{ .shell {{ padding:14px; }} .hero,.routes,.metrics,.findings,.status-grid {{ grid-template-columns:1fr; }} .time {{ margin-top:8px; }} }}
.ack-btn {{ padding:4px 10px; border:none; border-radius:4px; background:#6366f1; color:#fff; font-size:12px; cursor:pointer; }}
.ack-btn:hover {{ background:#4f46e5; }}
.ack-btn:disabled {{ background:#94a3b8; cursor:default; }}
</style>
</head>
<body>
<main class="shell">
  <section class="hero">
    <div>
      <h1>AutoTrading 指挥台</h1>
      <p>决策视角：盈亏、策略信号、服务器与哨兵健康、可进化优化点先暴露；详情报告只作为下钻。</p>
    </div>
    <div class="time">生成时间 {h(now)}</div>
  </section>

  <section class="metrics">{metric_html}</section>

  <section class="decision-brief {h(executive.get('level', 'warn'))}">
    {evolution_alert_html}
    <div class="decision-grid">
      <div>
        <h2>{h(executive.get('headline', '等待决策摘要'))}</h2>
        <ul>{executive_bullets}</ul>
      </div>
      <div>
        <h3>下一步</h3>
        <ol>{executive_actions}</ol>
      </div>
    </div>
    <table>
      <thead><tr><th>策略</th><th>当前决策</th><th>主动PnL</th><th>PF</th><th>开仓</th><th>最近开仓</th><th>判断</th></tr></thead>
      <tbody>{executive_strategy_rows}</tbody>
    </table>
  </section>

  <section class="section">
    <div class="section-head"><h2>当前总结</h2><small>昨天 00:00 到当前镜像 + 最新完整复盘。</small></div>
    <div class="findings">{findings_html}</div>
  </section>

  <section class="section panel">
    <h2>持久关注台账</h2>
    <p class="note">这不是日报摘要。只要没有被你确认关闭，事项会保留在 `research_memory/attention/open_items.json`，即使每日复盘滚动也不会丢。当前 open {int(attention_summary_data.get('open') or 0)}，已消失待复核 {int(attention_summary_data.get('cleared_pending_review') or 0)}；更新 {h(attention.get('age', '无台账'))}。</p>
    <table>
      <thead><tr><th>优先级</th><th>状态</th><th>分类</th><th>标题</th><th>证据</th><th>建议</th><th>操作</th></tr></thead>
      <tbody>{attention_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>策略进化门禁</h2>
    <p class="note">统一裁判口径：只有这里判定为 P0/P1 的候选，才会作为“更优方案”进入首页最高优先级。当前统计：P0 {int(evolution_counts.get('P0') or 0)}，P1 {int(evolution_counts.get('P1') or 0)}，P2 {int(evolution_counts.get('P2') or 0)}，拒绝 {int(evolution_counts.get('REJECT') or 0)}；更新 {h(evolution.get('age', '无门禁'))}。</p>
    <p class="note">Phase 8门禁硬化：{h(gate_hardening.get('status') or 'unknown')}；P0/P1候选 {int(gate_hardening.get('priority_items') or 0)}；放开后就绪窗口 {int(gate_hardening.get('post_approval_ready_windows') or 0)}；自动升级/回滚关闭。</p>
    <table>
      <thead><tr><th>优先级</th><th>状态</th><th>策略</th><th>候选</th><th>证据分</th><th>风险分</th><th>建议动作</th><th>关键阻塞</th></tr></thead>
      <tbody>{evolution_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>A/v11 trailing rollout复盘</h2>
    <p class="note">只读复盘已批准 trailing-pullback 上线后的 24h/72h/168h 真实窗口；当前结论 {h(a_v11_rollout_decision.get('priority', '-'))}/{h(a_v11_rollout_decision.get('status', '-'))}，更新 {h(a_v11_rollout.get('age'))}。这里不自动回滚，也不改实盘阈值。</p>
    <table>
      <thead><tr><th>窗口</th><th>开仓</th><th>平仓</th><th>强平</th><th>开仓失败</th><th>已实现PnL</th><th>估算成本</th><th>扣费后PnL</th><th>强平率</th></tr></thead>
      <tbody>{a_v11_rollout_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>B/v16 full-live rollout review</h2>
    <p class="note">Read-only review for approved ATR stop bands + score cap 85 after 24h/72h/168h live windows; current result {h(b_v16_rollout_decision.get('priority', '-'))}/{h(b_v16_rollout_decision.get('status', '-'))}, updated {h(b_v16_rollout.get('age'))}. No automatic rollback and no live parameter change.</p>
    <table>
      <thead><tr><th>Window</th><th>Opens</th><th>Closed</th><th>Forced</th><th>Open failed</th><th>Realized PnL</th><th>Cost</th><th>After cost</th><th>Forced rate</th></tr></thead>
      <tbody>{b_v16_rollout_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>实时账户盈亏</h2>
    <p class="note">来自 SQLite `account_snapshots` 最新快照；这是当前持仓和浮盈亏口径，优先级高于复盘交易日志。</p>
    <table>
      <thead><tr><th>账号/策略</th><th>浮盈亏</th><th>持仓</th><th>方向</th><th>wallet</th><th>available</th><th>名义仓位</th><th>硬顶风险</th><th>尺寸违规</th><th>快照</th></tr></thead>
      <tbody>{account_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>策略质量看板</h2>
    <p class="note">真相台账：主动策略 PnL（剔除恢复仓）vs 恢复仓 PnL。数据来源 {h(truth.get('age', '无台账'))}。</p>
    <table>
      <thead><tr><th>策略</th><th>已平仓</th><th>胜率</th><th>净PnL</th><th>PF</th><th>盈亏比</th><th>硬顶</th><th>恢复仓</th><th>恢复仓浮盈</th></tr></thead>
      <tbody>{quality_rows}</tbody>
    </table>
    <p class="note">主动策略累计 PnL: <b class="{'pos' if float(truth_summary.get('total_active_pnl_usd', 0)) >= 0 else 'neg'}">{float(truth_summary.get('total_active_pnl_usd', 0)):+.2f}</b> USDT；恢复仓未实现 PnL: <b class="{'pos' if recovery_total_upnl >= 0 else 'neg'}">{recovery_total_upnl:+.2f}</b> USDT。</p>
    <p class="note">恢复仓独立审查：review={int(recovery_risk.get('review') or 0)}，watch={int(recovery_risk.get('watch') or 0)}，none={int(recovery_risk.get('none') or 0)}；同策略重开支持={int(recovery_signal.get('same_strategy_reopen_supported') or 0)}，反向信号复核={int(recovery_signal.get('opposite_signal_review') or 0)}；策略退出证据 manual={int(recovery_strategy_exit.get('manual_review_positions') or 0)}，watch={int(recovery_strategy_exit.get('watch_positions') or 0)}，hold-bias={int(recovery_strategy_exit.get('hold_bias_positions') or 0)}；本地K线replay exit-review={int(recovery_replay_counts.get('bar_replay_exit_manual_review') or 0)}，data-gap={int(recovery_replay_counts.get('replay_data_gap') or 0)}；只读 shadow，不自动平仓。</p>
    <table>
      <thead><tr><th>策略</th><th>币种</th><th>方向</th><th>年龄h</th><th>浮盈</th><th>浮盈/保证金</th><th>MFE</th><th>MAE</th><th>MFE回撤</th><th>同向重开</th><th>反向信号</th><th>信号动作</th><th>策略退出证据</th><th>Bar replay</th><th>风险</th><th>Shadow动作</th></tr></thead>
      <tbody>{recovery_review_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>策略决策总览</h2>
    <p class="note">这是你最关心的表：每套策略昨天到当前镜像的盈亏、信号是否还在产生、是否开仓、哨兵是否参与、有没有明显异常。</p>
    <table>
      <thead><tr><th>策略</th><th>PnL</th><th>平仓</th><th>胜率</th><th>入场候选</th><th>开仓事件</th><th>持仓</th><th>哨兵决策</th><th>最近开仓</th><th>最后活动</th><th>判断</th></tr></thead>
      <tbody>{decision_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>OPEN_SKIPPED 反事实评估</h2>
    <p class="note">最近 {h(counterfactual.get('hours', '-'))} 小时；以 {h(counterfactual.get('horizon', 60))} 分钟模拟结果判断否决是否错杀。整体若放行：样本 {h(cf_overall.get('samples', '-'))}，胜率 {float(cf_overall.get('win_rate') or 0):.2f}%，PnL <span class="num {'pos' if float(cf_overall.get('pnl') or 0) >= 0 else 'neg'}">{float(cf_overall.get('pnl') or 0):+.2f}</span> USDT；fill net <span class="num {'pos' if float(cf_fill.get('net_pnl_usdt') or 0) >= 0 else 'neg'}">{float(cf_fill.get('net_pnl_usdt') or 0):+.2f}</span>，fee {float(cf_fill.get('fee_usdt') or 0):.2f}，partial {int(cf_fill.get('partial_fill_count') or 0)}，avg fill {float(cf_fill.get('avg_fill_ratio') or 0):.3f}，exit reasons {h(cf_exit_reasons)}；{cf_liquidity_note} 更新 {h(counterfactual.get('age'))}。</p>
    <table>
      <thead><tr><th>策略</th><th>被拒样本</th><th>若放行胜率</th><th>若放行PnL</th><th>平均MFE</th><th>平均MAE</th></tr></thead>
      <tbody>{counterfactual_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>出场模型</th><th>样本</th><th>胜率</th><th>Gross</th><th>Fee</th><th>Slippage</th><th>Net</th><th>Partial</th><th>Avg fill</th><th>Avg bars</th></tr></thead>
      <tbody>{counterfactual_fill_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>策略</th><th>主要否决层</th><th>样本</th><th>若放行胜率</th><th>若放行PnL</th></tr></thead>
      <tbody>{counterfactual_filter_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>研究仓样本漏斗</h2>
    <p class="note">DuckDB/Parquet 口径；最近 {h(research_store.get('days', '-'))} 日，更新 {h(research_store.get('age'))}。先看样本是否足够，再决定是否继续放宽或收紧策略。</p>
    <table>
      <thead><tr><th>策略</th><th>事件</th><th>信号</th><th>开仓</th><th>跳过</th><th>失败</th><th>最新时间</th></tr></thead>
      <tbody>{research_funnel_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>策略</th><th>主要否决层</th><th>数量</th></tr></thead>
      <tbody>{research_skip_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>周期</th><th>K线bars</th><th>币种数</th><th>最新K线</th></tr></thead>
      <tbody>{research_kline_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>周期</th><th>特征行</th><th>币种数</th><th>平均1bar波动</th><th>平均振幅</th><th>最新K线</th></tr></thead>
      <tbody>{research_feature_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>Replay / Live 门控审计</h2>
    <p class="note">把最近 live 事件库的 SIGNAL、OPEN、OPEN_SKIPPED、OPEN_FAILED 统一送入 `core.replay` 分类。覆盖率越高，说明后续把 A/B/C 抽成纯策略门控函数时，历史样本越能复现当前实盘判断。</p>
    <p class="note">窗口 {int(replay_gate.get('days') or 0)} 日；open-flow {int(replay_gate_summary_data.get('open_flow_events') or 0)} 条；gate覆盖 {float(replay_gate_summary_data.get('gate_coverage_pct') or 0):.1f}%；未知门控 {int(replay_gate_summary_data.get('unknown_gate') or 0)}；更新 {h(replay_gate.get('age'))}。</p>
    <table>
      <thead><tr><th>策略</th><th>开仓流</th><th>OPEN</th><th>SIGNAL</th><th>否决</th><th>失败</th><th>未知门控</th><th>覆盖率</th><th>主要 gate</th></tr></thead>
      <tbody>{replay_gate_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>Replay/live 同输入审计</h2>
    <p class="note">Exact parity 只使用事件 payload 里持久化的 `strategy_gate_case(s)`。没有 exact case 的 live 行计为缺口，不从 stage/layer 反推，避免把观测归因误当同输入一致。</p>
    <p class="note">窗口 {int(replay_parity.get('days') or 0)} 日；open-flow {int(replay_parity_summary_data.get('open_flow_rows') or 0)} 条；exact rows {int(replay_parity_summary_data.get('rows_with_exact_cases') or 0)}；cases {int(replay_parity_summary_data.get('gate_cases') or 0)}；pass {float(replay_parity_summary_data.get('pass_rate_pct') or 0):.1f}%；mismatch {int(replay_parity_summary_data.get('mismatched') or 0)}；errors {int(replay_parity_summary_data.get('errors') or 0)}；exact覆盖 {float(replay_parity_summary_data.get('exact_case_coverage_pct') or 0):.1f}%；observed覆盖 {float(replay_parity_summary_data.get('observed_gate_coverage_pct') or 0):.1f}%；更新 {h(replay_parity.get('age'))}。</p>
    <table>
      <thead><tr><th>策略</th><th>开仓流</th><th>Exact rows</th><th>缺 case</th><th>Cases</th><th>Pass</th><th>Mismatch</th><th>Errors</th><th>Pass rate</th><th>主要 exact gate</th></tr></thead>
      <tbody>{replay_parity_rows}</tbody>
    </table>
  </section>

  <section class="section">
    <div class="section-head"><h2>功能运行状态</h2><small>服务器、策略、哨兵、研究审批的健康状态。</small></div>
    <div class="status-grid">{function_cards}</div>
  </section>

  <section class="section panel">
    <h2>SQLite 事件库覆盖</h2>
    <p class="note">主入口已使用 SQLite 聚合口径；JSONL 仅在事件库不可用时回退。这里展示各策略持续写入覆盖情况。</p>
    <p class="note">事件库：{h(event_store.get('db_path'))}；总事件 {h(event_store.get('total_events'))}；最新写入 {h(event_store.get('latest_age'))}；基线 {h(event_store.get('baseline_runs'))} 次。{h(event_store.get('note'))}</p>
    <table>
      <thead><tr><th>策略</th><th>SQLite events</th><th>SQLite decisions</th><th>SQLite signals</th><th>SQLite system</th><th>回退信号</th><th>回退哨兵</th><th>最新ID</th></tr></thead>
      <tbody>{event_store_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>来源</th><th>策略</th><th>事件类型</th><th>分类</th><th>数量</th></tr></thead>
      <tbody>{event_source_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>策略信号与可优化点</h2>
    <p class="note">亏损、低转化、确认过滤、分数否决、哨兵400会在这里集中暴露。这里有问题，再点信号质量或研究审阅台。</p>
    <table>
      <thead><tr><th>策略</th><th>优化/异常点</th><th>最差交易</th><th>最好交易</th><th>确认过滤</th><th>分数否决</th><th>哨兵400</th></tr></thead>
      <tbody>{optimize_rows}</tbody>
    </table>
  </section>

  <section class="section panel">
    <h2>账号盈亏与信号摘要</h2>
    <p class="note">周期：{h(data['signal_summary'].get('date'))}。这里是复盘口径，不替代实时账户权益；实时风险仍以“账号快照”为准。</p>
    <table>
      <thead><tr><th>策略</th><th>信号</th><th>开仓</th><th>平仓</th><th>胜率</th><th>全量PnL</th><th>硬顶率</th></tr></thead>
      <tbody>{pnl_rows}</tbody>
    </table>
  </section>

  <section class="section">
    <div class="section-head"><h2>详情入口</h2><small>有兴趣再下钻。</small></div>
    <div class="routes">{''.join(routes)}</div>
  </section>

  <section class="section panel">
    <h2>哨兵链路摘要</h2>
    <p class="note">哨兵只负责发现异动币并交给策略。下面能看见每套策略扫了多少哨兵信号，以及主要在哪个层面被开仓、过滤或否决。</p>
    <table>
      <thead><tr><th>策略</th><th>哨兵决策数</th><th>结果分布</th><th>层级分布</th><th>代表币种</th></tr></thead>
      <tbody>{sentinel_rows}</tbody>
    </table>
    <p class="note">N6 审计：大行情覆盖 {int(sentinel_coverage.get('covered_big_move_signals') or 0)}/{int(sentinel_coverage.get('big_move_signals') or 0)}，覆盖率 {float(sentinel_coverage.get('coverage_pct') or 0):.2f}%；更新 {h(sentinel_quality.get('age'))}。</p>
    <p class="note">{sentinel_watchlist_note}</p>
    <table class="subtable">
      <thead><tr><th>窗口</th><th>样本</th><th>原始均值</th><th>方向扣费后均值</th><th>方向胜率</th></tr></thead>
      <tbody>{sentinel_forward_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>未覆盖样例</th><th>涨跌幅</th><th>加速度</th><th>成交额</th><th>时间</th></tr></thead>
      <tbody>{sentinel_missed_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>大行情归因</th><th>数量</th><th>占比</th><th>样例</th></tr></thead>
      <tbody>{sentinel_attribution_rows}</tbody>
    </table>
    <table class="subtable">
      <thead><tr><th>未扫描细分</th><th>数量</th><th>占全部</th><th>占未扫描</th><th>样例</th></tr></thead>
      <tbody>{sentinel_not_scanned_rows}</tbody>
    </table>
  </section>

</main>
<script>
const API_BASE = 'http://39.105.156.210:8090';
async function ackItem(btn) {{
  const itemId = btn.dataset.id;
  if (!itemId) return;
  btn.disabled = true;
  btn.textContent = '处理中...';
  try {{
    const resp = await fetch(API_BASE + '/api/attention/ack', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{item_id: itemId, user: 'portal'}})
    }});
    const data = await resp.json();
    if (data.ok) {{
      btn.textContent = '已确认';
      btn.style.background = '#16a34a';
      const row = btn.closest('tr');
      if (row) {{
        const statusCell = row.children[1];
        if (statusCell) statusCell.textContent = 'acknowledged';
      }}
    }} else {{
      btn.textContent = '失败';
      btn.style.background = '#dc2626';
      console.error('Ack failed:', data.error);
    }}
  }} catch (e) {{
    btn.textContent = '网络错误';
    btn.style.background = '#dc2626';
    console.error('Network error:', e);
  }}
}}
</script>
</body>
</html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 AutoTrading 指挥台 HTML")
    parser.add_argument("--out-dir", default=str(REPORTS_DIR))
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_text = render_html(out_dir)
    latest = out_dir / "portal_latest.html"
    latest.write_text(html_text, encoding="utf-8")
    index = out_dir / "index.html"
    index.write_text(html_text, encoding="utf-8")
    print(f"总入口已生成: {latest}")
    print(f"Index: {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
