"""Generate a concise online decision portal for the operator.

This is the first screen. It only reads existing local/runtime artifacts and
never calls Binance. The older full portal remains available as
``portal_latest.html`` for drilldown.
"""

from __future__ import annotations

import argparse
import html
import json
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
LOCAL_DB = RUNTIME_DIR / "event_store.sqlite3"
MIRROR_DB = ROOT / "server_logs_tencent" / "runtime" / "event_store.sqlite3"
EVENT_DB = MIRROR_DB if MIRROR_DB.exists() else LOCAL_DB
CST = timezone(timedelta(hours=8))


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


def plain_level(level: str) -> str:
    return level if level in {"good", "warn", "bad", "muted"} else "muted"


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
                      sum(case when event_type like '%CLOSE_FAILED%' or category like '%close_failed%' then 1 else 0 end) as close_failed
                    from events
                    where strategy=? and substr(ts,1,10) >= ?
                    """,
                    (name, since),
                ).fetchone()
                strategies.append({
                    "name": name,
                    "latest": parse_dt(latest["ts"]) if latest else None,
                    "opens": int(counts["opens"] or 0) if counts else 0,
                    "closes": int(counts["closes"] or 0) if counts else 0,
                    "open_failed": int(counts["open_failed"] or 0) if counts else 0,
                    "close_failed": int(counts["close_failed"] or 0) if counts else 0,
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


def strategy_rows(event: dict[str, Any], alerts: dict[str, Any]) -> list[dict[str, str]]:
    services = alerts.get("services") if isinstance(alerts.get("services"), dict) else {}
    service_map = {
        "A/v11": "crypto-scanner.service",
        "B/v16": "crypto-scanner-v16.service",
        "C/v14": "crypto-scanner-v14.service",
    }
    by_name = {row["name"]: row for row in event.get("strategies") or [] if isinstance(row, dict)}
    rows = []
    for name in ("A/v11", "B/v16", "C/v14"):
        item = by_name.get(name, {})
        service = services.get(service_map[name], "unknown")
        bad = int(item.get("open_failed") or 0) + int(item.get("close_failed") or 0)
        level = "bad" if service != "active" or bad else "good"
        rows.append({
            "level": level,
            "name": name,
            "service": "运行中" if service == "active" else f"异常({service})",
            "age": age_text(item.get("latest")),
            "opens": str(item.get("opens", 0)),
            "closes": str(item.get("closes", 0)),
            "bad": str(bad),
        })
    return rows


def attention_items(limit: int = 8) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = read_json(ATTENTION_JSON)
    items = [
        item for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("status") in {"open", "cleared_pending_review"}
    ]
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    items.sort(key=lambda item: (rank.get(str(item.get("priority") or "P3"), 9), str(item.get("title") or "")))
    return payload.get("summary") or {}, items[:limit]


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
    alerts = read_first_json(RUNTIME_DIR / "alerts_latest.json", MIRROR_RUNTIME_DIR / "alerts_latest.json")
    account = read_first_json(RUNTIME_DIR / "account_snapshot_latest.json", MIRROR_RUNTIME_DIR / "account_snapshot_latest.json")
    evolution = read_first_json(RUNTIME_DIR / "strategy_evolution_latest.json", MIRROR_RUNTIME_DIR / "strategy_evolution_latest.json")
    replay = read_first_json(RUNTIME_DIR / "replay_readiness_latest.json", MIRROR_RUNTIME_DIR / "replay_readiness_latest.json")
    parity = read_first_json(RUNTIME_DIR / "replay_live_parity_latest.json", MIRROR_RUNTIME_DIR / "replay_live_parity_latest.json")
    skeleton = read_first_json(RUNTIME_DIR / "long_term_skeleton_latest.json", MIRROR_RUNTIME_DIR / "long_term_skeleton_latest.json")
    research = read_first_json(RUNTIME_DIR / "research_store_summary_latest.json", MIRROR_RUNTIME_DIR / "research_store_summary_latest.json")
    kline = read_first_json(RUNTIME_DIR / "research_kline_backfill_latest.json", MIRROR_RUNTIME_DIR / "research_kline_backfill_latest.json")
    depth = read_first_json(RUNTIME_DIR / "research_depth_backfill_latest.json", MIRROR_RUNTIME_DIR / "research_depth_backfill_latest.json")
    q = queue_summary()
    ev = event_summary()
    att_summary, att_items = attention_items()
    account_summary = account.get("summary") if isinstance(account.get("summary"), dict) else {}
    stale = account_summary.get("stale_accounts") if isinstance(account_summary.get("stale_accounts"), list) else []
    active_alerts = [
        a for a in alerts.get("alerts", [])
        if isinstance(a, dict) and a.get("level") in {"bad", "warn"}
    ]
    bad_alerts = [a for a in active_alerts if a.get("level") == "bad"]
    overall = "good"
    if bad_alerts or q.get("cooldowns") or q.get("active"):
        overall = "bad"
    elif active_alerts or stale:
        overall = "warn"
    return {
        "generated_at": datetime.now(CST),
        "overall": overall,
        "alerts": alerts,
        "account": account,
        "account_summary": account_summary,
        "evolution": evolution,
        "replay": replay,
        "parity": parity,
        "skeleton": skeleton,
        "research": research,
        "kline": kline,
        "depth": depth,
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


def render_badges(state: dict[str, Any]) -> str:
    q = state["queue"]
    account = state["account_summary"]
    alerts = state["alerts"]
    skeleton_summary = (state["skeleton"].get("summary") or {}) if isinstance(state["skeleton"], dict) else {}
    items = [
        ("三策略", "运行中", "good"),
        ("API队列", f"等待{q.get('active', 0)} / 冷却{q.get('cooldowns', 0)}", "bad" if q.get("active") or q.get("cooldowns") else "good"),
        ("持仓", str(account.get("open_positions", 0)), "good"),
        ("浮盈亏", f"{number(account.get('unrealized_pnl_usdt'))} USDT", "good"),
        ("告警", str(alerts.get("alert_count", 0)), "bad" if alerts.get("status") == "bad" else "warn" if alerts.get("status") == "warn" else "good"),
        ("长期骨架", f"{skeleton_summary.get('ready_bones', 0)}/{skeleton_summary.get('total_bones', 0)}", "good"),
    ]
    return "".join(
        f'<article class="metric {plain_level(level)}"><span>{h(label)}</span><b>{h(value)}</b></article>'
        for label, value, level in items
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
  <td>{h(row['bad'])}</td>
</tr>
""".strip()
        for row in rows
    )
    return f"""
<table>
  <thead><tr><th>策略</th><th>服务</th><th>最新数据</th><th>24h开仓</th><th>24h平仓</th><th>失败</th></tr></thead>
  <tbody>{body}</tbody>
</table>
"""


def render_attention(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<p class="empty">暂无需要你确认的事项。</p>'
    rows = []
    for item in items:
        item_id = str(item.get("item_id") or "")
        rows.append(
            f"""
<tr data-item="{h(item_id)}">
  <td><b>{h(item.get('priority'))}</b></td>
  <td>{h(item.get('title'))}<small>{h(item.get('evidence') or '')}</small></td>
  <td>{h(item.get('recommended_action') or '看完后可确认')}</td>
  <td><button class="icon-btn" onclick="ackItem('{h(item_id)}', this)">确认</button></td>
</tr>
""".strip()
        )
    return f"""
<table>
  <thead><tr><th>级别</th><th>事项</th><th>建议</th><th></th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
"""


def render_cards(state: dict[str, Any]) -> str:
    account = state["account_summary"]
    evolution = state["evolution"].get("summary") or {}
    expansion = evolution.get("expansion_readiness") if isinstance(evolution.get("expansion_readiness"), dict) else {}
    replay = state["replay"]
    replay_summary = replay.get("summary") if isinstance(replay.get("summary"), dict) else {}
    parity_summary = state["parity"].get("summary") if isinstance(state["parity"].get("summary"), dict) else {}
    research = state["research"]
    kline_acceptance = research.get("kline_acceptance") if isinstance(research.get("kline_acceptance"), dict) else {}
    stale = account.get("stale_accounts") if isinstance(account.get("stale_accounts"), list) else []
    rows = [
        ("账户资金", f"wallet {number(account.get('wallet_usdt'))} / available {number(account.get('available_usdt'))}", "B/C 若显示等待 user-stream，不代表该去手动查余额。"),
        ("策略升级样本", f"成熟 {expansion.get('ready_count', 0)} / 收样 {expansion.get('maturing_count', 0)}", f"24h 样本缺口 {expansion.get('missing_samples_24h', 0)}。先收自然交易，不盲目扩。"),
        ("Replay验收", str(replay.get("status") or "missing"), f"ready {replay_summary.get('ready_components', 0)}/{replay_summary.get('total_components', 0)}，下一步 {replay.get('next_action') or '-'}。"),
        ("同输入审计", f"{float(parity_summary.get('pass_rate_pct') or 0):.1f}% pass", f"exact cases {parity_summary.get('gate_cases', 0)}，mismatch {parity_summary.get('mismatched', 0)}。"),
        ("K线/深度", str(kline_acceptance.get("status") or "missing"), "30天 K线和深度样本是后续回测升级的主燃料。"),
        ("服务器清理", f"{state['cleanup']['total_mb']} MB", "当前只列计划。删除/归档由维护任务做，保留回滚证据。"),
    ]
    if stale:
        rows.insert(1, ("账户刷新", "等待 user-stream", "B/C stale 空账户是控风控模式，不强拉 signed REST。"))
    return "".join(
        f'<article class="info"><span>{h(title)}</span><b>{h(value)}</b><p>{h(body)}</p></article>'
        for title, value, body in rows
    )


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


def render_html() -> str:
    state = build_state()
    strategies = strategy_rows(state["event"], state["alerts"])
    generated = state["generated_at"].strftime("%Y-%m-%d %H:%M:%S")
    alerts = state["alerts"].get("alerts") if isinstance(state["alerts"].get("alerts"), list) else []
    alert_list = "".join(
        f'<li class="{plain_level(a.get("level"))}"><b>{h(a.get("title"))}</b><span>{h(a.get("body"))}</span></li>'
        for a in alerts[:6] if isinstance(a, dict)
    ) or '<li class="good"><b>无红灯</b><span>当前没有需要立即停机的告警。</span></li>'
    event = state["event"]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>AutoTrading 决策入口</title>
<style>
:root {{
  --bg:#f5f7fb; --panel:#ffffff; --ink:#182033; --muted:#667085;
  --line:#d9e0ea; --good:#16a34a; --warn:#d97706; --bad:#dc2626;
  --cyan:#0891b2; --blue:#2563eb;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.55 "Segoe UI", Arial, sans-serif; }}
.wrap {{ max-width:1280px; margin:0 auto; padding:24px; }}
header {{ display:grid; grid-template-columns:1fr auto; gap:16px; align-items:end; margin-bottom:18px; }}
h1 {{ margin:0; font-size:30px; letter-spacing:0; }}
.sub {{ color:var(--muted); margin-top:6px; }}
.status {{ padding:10px 14px; border-radius:6px; font-weight:700; color:#fff; }}
.status.good {{ background:var(--good); }} .status.warn {{ background:var(--warn); }} .status.bad {{ background:var(--bad); }}
.metrics {{ display:grid; grid-template-columns:repeat(6, minmax(0, 1fr)); gap:10px; margin:14px 0 18px; }}
.metric,.panel,.info {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:0 1px 2px rgba(16,24,40,.04); }}
.metric {{ padding:14px; min-height:84px; }}
.metric span,.info span {{ color:var(--muted); display:block; font-size:12px; }}
.metric b {{ display:block; font-size:22px; margin-top:8px; }}
.metric.good {{ border-top:4px solid var(--good); }} .metric.warn {{ border-top:4px solid var(--warn); }} .metric.bad {{ border-top:4px solid var(--bad); }}
.grid {{ display:grid; grid-template-columns:1.25fr .75fr; gap:14px; align-items:start; }}
.panel {{ padding:16px; margin-bottom:14px; }}
.panel h2 {{ margin:0 0 10px; font-size:18px; }}
.cards {{ display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:10px; }}
.info {{ padding:14px; min-height:112px; }}
.info b {{ display:block; font-size:18px; margin:6px 0; }}
.info p {{ margin:0; color:var(--muted); }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ color:var(--muted); font-size:12px; font-weight:700; }}
td small {{ display:block; color:var(--muted); margin-top:4px; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:8px; background:var(--muted); }}
.dot.good {{ background:var(--good); }} .dot.warn {{ background:var(--warn); }} .dot.bad {{ background:var(--bad); }}
.alerts {{ list-style:none; padding:0; margin:0; display:grid; gap:8px; }}
.alerts li {{ border-left:4px solid var(--line); padding:9px 10px; background:#f8fafc; border-radius:6px; }}
.alerts li.good {{ border-color:var(--good); }} .alerts li.warn {{ border-color:var(--warn); }} .alerts li.bad {{ border-color:var(--bad); }}
.alerts b,.alerts span {{ display:block; }} .alerts span {{ color:var(--muted); margin-top:2px; }}
.icon-btn {{ border:0; background:var(--blue); color:white; border-radius:6px; padding:8px 12px; cursor:pointer; font-weight:700; }}
.icon-btn:disabled {{ opacity:.65; cursor:default; }}
.links {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }}
.links a {{ color:var(--blue); background:#eff6ff; border:1px solid #bfdbfe; padding:7px 10px; border-radius:6px; text-decoration:none; }}
.empty {{ color:var(--muted); }}
@media (max-width: 980px) {{ .metrics,.cards,.grid {{ grid-template-columns:1fr; }} header {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<main class="wrap">
  <header>
    <div>
      <h1>AutoTrading 决策入口</h1>
      <div class="sub">更新 {h(generated)}。线上首页 5 分钟自动刷新；这里只读现有数据，不请求 Binance。</div>
    </div>
    <div class="status {plain_level(state['overall'])}">{h(status_text(state))}</div>
  </header>
  <section class="metrics">{render_badges(state)}</section>
  <section class="grid">
    <div>
      <section class="panel">
        <h2>三策略现在是否正常</h2>
        {render_strategy_table(strategies)}
      </section>
      <section class="panel">
        <h2>你需要确认的事项</h2>
        {render_attention(state['attention_items'])}
      </section>
      <section class="panel">
        <h2>服务器清理计划</h2>
        <p class="empty">先列候选，不直接删除。目标是清掉旧噪音，保留回滚、审计、重置 receipt。</p>
        {render_cleanup(state)}
      </section>
    </div>
    <aside>
      <section class="panel">
        <h2>今日重点</h2>
        <div class="cards">{render_cards(state)}</div>
      </section>
      <section class="panel">
        <h2>红黄灯</h2>
        <ul class="alerts">{alert_list}</ul>
      </section>
      <section class="panel">
        <h2>下钻</h2>
        <div class="links">
          <a href="/reports/portal_latest.html">完整旧版详情</a>
          <a href="/reports/replay_readiness_latest.md">Replay 验收</a>
          <a href="/reports/long_term_skeleton_latest.md">长期目标骨架</a>
          <a href="/reports/strategy_evolution_latest.html">策略进化</a>
          <a href="/reports/research_store_summary_latest.md">研究仓</a>
          <a href="/api/attention">确认事项 API</a>
        </div>
      </section>
    </aside>
  </section>
</main>
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
