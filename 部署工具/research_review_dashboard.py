"""Render a human-friendly research review dashboard.

The page is static HTML on purpose: it is safe for Aliyun shadow review, easy
to archive, and does not introduce a new service next to live trading.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
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
if (SCRIPT_DIR / "core").exists():
    ROOT = SCRIPT_DIR
else:
    ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))
PORTAL_URL = "file:///F:/AutoTrading/reports/index.html"


def back_button() -> str:
    return f'<a class="back-link" href="{PORTAL_URL}">返回总入口</a>'
DEFAULT_MEMORY_DIR = ROOT / "research_memory"
DEFAULT_EXPERIMENT_RESULTS = ROOT / "experiments" / "results" / "latest.jsonl"
DEFAULT_OUT_DIR = ROOT / "reports"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def latest_file(folder: Path, pattern: str) -> Path | None:
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return files[0] if files else None


def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def num(value: Any, digits: int = 2) -> float:
    try:
        return round(float(value), digits)
    except Exception:
        return 0.0


def signed(value: Any, suffix: str = "") -> str:
    v = num(value)
    return f"{v:+.2f}{suffix}"


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def fmt_price(value: Any) -> str:
    try:
        v = float(value)
    except Exception:
        return "-"
    text = f"{v:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def parse_case_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    if "+" in text:
        text = text.split("+", 1)[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt)
        except Exception:
            continue
    return None


def duration_label(start: Any, end: Any) -> str:
    s = parse_case_time(start)
    e = parse_case_time(end)
    if not s or not e or e < s:
        return "-"
    minutes = int((e - s).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours}小时{rest}分" if rest else f"{hours}小时"


def side_from_case(case: dict[str, Any]) -> str:
    side = str(case.get("side") or "").lower()
    action = str(case.get("actual_action") or "").lower()
    if side in {"long", "short"}:
        return side
    if "long" in action:
        return "long"
    if "short" in action:
        return "short"
    return ""


def status_label(status: str) -> str:
    labels = {
        "approved_candidate": "可人工审批",
        "observe": "继续观察",
        "reject": "拒绝晋级",
        "generated": "待回放",
    }
    return labels.get(status or "", status or "-")


def status_class(status: str) -> str:
    if status == "approved_candidate":
        return "good"
    if status == "reject":
        return "bad"
    return "watch"


def case_type_label(case_type: str) -> str:
    labels = {
        "loss_trade": "亏损样本",
        "hard_stop_loss": "硬顶样本",
        "reverse_trade": "反向开仓",
        "missed_big_move": "错过大行情",
    }
    return labels.get(case_type or "", case_type or "-")


def case_sort_key(case: dict[str, Any]) -> tuple[float, float]:
    return (num(case.get("confidence")), abs(num(case.get("pnl_usd")) or num(case.get("move_pct"))))


def load_cases(memory_dir: Path) -> list[dict[str, Any]]:
    cases_folder = memory_dir / "cases"
    path = latest_file(cases_folder, "cases_*.jsonl")
    return read_jsonl(path) if path else []


def load_market_payload(memory_dir: Path) -> Any:
    direct_paths = [
        memory_dir / "snapshots" / "market_snapshot_latest.json",
        ROOT / "server_logs_tencent" / "reports" / "market_snapshot_latest.json",
        ROOT / "reports" / "market_snapshot_latest.json",
    ]
    for path in direct_paths:
        payload = read_json(path)
        if payload:
            return payload

    latest_candidates: list[Path] = []
    for folder, pattern in (
        (memory_dir / "snapshots", "market_snapshot_*.json"),
        (ROOT / "server_logs_tencent" / "reports", "market_snapshot_*.json"),
        (ROOT / "reports", "market_snapshot_*.json"),
        (memory_dir / "snapshots", "market_moves_*.json"),
    ):
        if folder.exists():
            path = latest_file(folder, pattern)
            if path:
                latest_candidates.append(path)

    for path in sorted(latest_candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        payload = read_json(path)
        if payload:
            return payload
    return {}


def normalize_market_moves(payload: Any) -> list[dict[str, Any]]:
    rows: Any
    if isinstance(payload, dict):
        rows = payload.get("moves") or payload.get("market_moves") or []
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("symbol")]


def load_dashboard_data(memory_dir: Path, experiment_results: Path) -> dict[str, Any]:
    candidates = read_jsonl(memory_dir / "hypotheses" / "candidates_latest.jsonl")
    reviews = read_jsonl(memory_dir / "promotions" / "reviews_latest.jsonl")
    experiments = read_jsonl(experiment_results)
    cases = load_cases(memory_dir)
    symbol_lessons = read_jsonl(memory_dir / "lessons" / "symbol_lessons.jsonl")
    factor_lessons = read_jsonl(memory_dir / "lessons" / "factor_lessons.jsonl")
    family_path = experiment_results.parent.parent / "families_latest.json"
    families = read_json(family_path)
    if not families:
        families = read_json(memory_dir / "promotions" / "families_latest.json")
    snapshot_path = latest_file(memory_dir / "snapshots", "daily_summary_*.json")
    snapshot = read_json(snapshot_path) if snapshot_path else {}
    market_payload = load_market_payload(memory_dir)
    market_moves = normalize_market_moves(market_payload)
    return {
        "candidates": candidates,
        "reviews": reviews,
        "experiments": experiments,
        "cases": cases,
        "symbol_lessons": symbol_lessons,
        "factor_lessons": factor_lessons,
        "families": families if isinstance(families, list) else [],
        "snapshot": snapshot or {},
        "market_snapshot": market_payload if isinstance(market_payload, dict) else {},
        "market_moves": market_moves,
    }


def by_key(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(row.get(key) or ""): row for row in rows if row.get(key)}


def cases_by_id(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return by_key(cases, "case_id")


def moves_by_symbol(moves: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("symbol") or ""): row for row in moves if row.get("symbol")}


def render_metrics(data: dict[str, Any]) -> str:
    candidates = data["candidates"]
    cases = data["cases"]
    reviews = data["reviews"]
    experiments = data["experiments"]
    families = data.get("families") or []
    approved = sum(1 for r in reviews if r.get("promotion_status") == "approved_candidate")
    rejected = sum(1 for r in reviews if r.get("promotion_status") == "reject")
    reverse = sum(1 for c in cases if c.get("case_type") == "reverse_trade")
    missed = sum(1 for c in cases if c.get("case_type") == "missed_big_move")
    return f"""
<section class="metrics" aria-label="研究概览">
  <article><span>候选</span><b>{len(candidates)}</b><em>待审实验</em></article>
  <article><span>案例</span><b>{len(cases)}</b><em>长期记忆</em></article>
  <article><span>反向</span><b>{reverse}</b><em>需重点复核</em></article>
  <article><span>错过</span><b>{missed}</b><em>大行情卡点</em></article>
  <article><span>门禁</span><b>{approved}/{rejected}</b><em>通过 / 拒绝</em></article>
  <article><span>实验</span><b>{len(experiments)}</b><em>影子回放</em></article>
  <article><span>实验族</span><b>{len(families)}</b><em>治理单元</em></article>
</section>
""".strip()


def family_id_for_row(row: dict[str, Any]) -> str:
    return str(row.get("family_id") or row.get("experiment_family_id") or "")


def fallback_family_id(candidate: dict[str, Any]) -> str:
    return "FAM-" + stable_text(candidate.get("strategy"), candidate.get("change_type"), "generated")


def stable_text(*parts: Any) -> str:
    text = "-".join(str(p or "").strip().replace(" ", "_") for p in parts)
    return "".join(ch for ch in text if ch.isalnum() or ch in "-_./").replace("/", "-")


def family_action_label(action: str) -> str:
    labels = {
        "needs_manual_approval": "待人工审批",
        "archive_or_rework": "归档或重做",
        "observe": "继续观察",
        "active": "运行中",
    }
    return labels.get(action or "", action or "-")


def render_family_management(data: dict[str, Any]) -> str:
    families = data.get("families") or []
    if not families:
        return """
<section class="families"><h2>实验族管理</h2><p class="empty">暂无实验族记录。下一次影子实验运行后会自动生成 families_latest.json。</p></section>
""".strip()
    cards = []
    for family in families:
        action = str(family.get("recommended_action") or family.get("governance_status") or "observe")
        action_cls = "good" if action == "needs_manual_approval" else "bad" if action == "archive_or_rework" else "watch"
        delta = num(family.get("best_pnl_delta"))
        hard_delta = num(family.get("hard_stop_delta"), 0)
        cards.append(f"""
<article class="family-card {action_cls}">
  <div class="family-head">
    <div>
      <span>{h(family.get("base_strategy"))} · {h(family.get("change_type"))}</span>
      <h3>{h(family.get("family_id"))}</h3>
    </div>
    <b>{family_action_label(action)}</b>
  </div>
  <div class="family-metrics">
    <div><span>版本</span><strong>{h(family.get("experiment_count", 0))}</strong></div>
    <div><span>候选</span><strong>{h(family.get("candidate_count", 0))}</strong></div>
    <div><span>最佳PnL差</span><strong class="{'pos' if delta >= 0 else 'neg'}">{delta:+.2f}</strong></div>
    <div><span>硬顶变化</span><strong class="{'pos' if hard_delta <= 0 else 'neg'}">{hard_delta:+.0f}</strong></div>
  </div>
  <p>最新: {h(family.get("latest_experiment_id"))} · {status_label(str(family.get("latest_status") or ""))}</p>
</article>
""".strip())
    return f"<section class=\"families\"><h2>实验族管理</h2><div class=\"family-grid\">{''.join(cards)}</div></section>"


def render_candidate_queue(data: dict[str, Any]) -> str:
    experiments = by_key(data["experiments"], "experiment_id")
    reviews = by_key(data["reviews"], "candidate_id")
    cards = []
    for idx, candidate in enumerate(data["candidates"], 1):
        cid = str(candidate.get("candidate_id") or "")
        result = experiments.get(cid, {})
        review = reviews.get(cid, {})
        status = str(review.get("promotion_status") or result.get("promotion_status") or candidate.get("status") or "generated")
        pnl_delta = num(result.get("shadow_pnl")) - num(result.get("original_pnl"))
        cards.append(f"""
<a class="candidate-card {status_class(status)}" href="#candidate-{h(cid)}">
  <span class="queue-index">{idx:02d}</span>
  <div>
    <strong>{h(candidate.get("strategy"))}</strong>
    <small>{h(candidate.get("change_type"))}</small>
  </div>
  <p>{h(candidate.get("problem"))}</p>
  <footer>
    <span>{status_label(status)}</span>
    <span class="{'pos' if pnl_delta >= 0 else 'neg'}">{pnl_delta:+.2f}</span>
  </footer>
</a>
""".strip())
    if not cards:
        cards.append("<p class=\"empty\">当前没有候选。先积累案例，系统会自动生成待审假设。</p>")
    return f"<aside class=\"queue\" aria-label=\"候选队列\"><h2>候选队列</h2>{''.join(cards)}</aside>"


def render_gate(candidate: dict[str, Any], result: dict[str, Any], review: dict[str, Any]) -> str:
    status = str(review.get("promotion_status") or result.get("promotion_status") or candidate.get("status") or "generated")
    hard = f"{result.get('hard_stop_before', 0)} -> {result.get('hard_stop_after', 0)}"
    source_cases = candidate.get("source_cases") or []
    family_id = family_id_for_row(candidate) or family_id_for_row(result) or family_id_for_row(review) or fallback_family_id(candidate)
    return f"""
<div class="gate">
  <div class="gate-status {status_class(status)}">
    <span>晋级判定</span>
    <b>{status_label(status)}</b>
  </div>
  <dl>
    <div><dt>原始 PnL</dt><dd>{signed(result.get("original_pnl"))}</dd></div>
    <div><dt>影子 PnL</dt><dd>{signed(result.get("shadow_pnl"))}</dd></div>
    <div><dt>过滤/候选</dt><dd>{h(result.get("filtered_trades", "-"))}</dd></div>
    <div><dt>硬顶变化</dt><dd>{h(hard)}</dd></div>
    <div><dt>源案例</dt><dd>{len(source_cases)} 个</dd></div>
    <div><dt>审批动作</dt><dd>{h(review.get("decision") or "待人工确认")}</dd></div>
    <div><dt>实验族</dt><dd>{h(family_id)}</dd></div>
  </dl>
</div>
""".strip()


def render_approval_panel(candidate: dict[str, Any], result: dict[str, Any], review: dict[str, Any]) -> str:
    cid = str(candidate.get("candidate_id") or "")
    family_id = family_id_for_row(candidate) or family_id_for_row(result) or family_id_for_row(review) or fallback_family_id(candidate)
    payload = {
        "action_schema": "manual_strategy_research_approval/v1",
        "created_at": datetime.now(CST).isoformat(timespec="seconds"),
        "candidate_id": cid,
        "experiment_id": result.get("experiment_id") or cid,
        "family_id": family_id,
        "base_strategy": candidate.get("strategy") or result.get("base_strategy"),
        "change_type": candidate.get("change_type") or result.get("change_type"),
        "recommended_status": review.get("promotion_status") or result.get("promotion_status") or "observe",
        "manual_action": "observe",
        "approved_scope": "shadow_only",
        "next_step": "继续观察，不改实盘",
        "risk_notes": "",
        "operator": "human",
    }
    encoded = h(json.dumps(payload, ensure_ascii=False, indent=2))
    return f"""
<section class="approval-panel" data-candidate="{h(cid)}">
  <div class="approval-head">
    <div>
      <h3>人工审批台</h3>
      <p>审批只产生记录，不会直接改实盘。通过后仍需单独部署。</p>
    </div>
    <span>{h(family_id)}</span>
  </div>
  <div class="approval-actions">
    <button type="button" data-action="approve_shadow" data-scope="shadow_plus_small_live" data-next="允许进入小仓观察，需单独部署和限额">批准小仓观察</button>
    <button type="button" data-action="observe" data-scope="shadow_only" data-next="继续积累样本，不改实盘">继续观察</button>
    <button type="button" data-action="reject" data-scope="none" data-next="拒绝当前候选，不进入实盘">拒绝候选</button>
    <button type="button" data-action="archive_family" data-scope="family" data-next="归档实验族，后续只保留历史证据">归档实验族</button>
  </div>
  <textarea class="approval-json" rows="12" spellcheck="false">{encoded}</textarea>
  <div class="approval-foot">
    <button type="button" class="copy-approval">复制审批记录</button>
    <small>建议保存到 research_memory/approvals/manual_actions.jsonl 后再由脚本应用。</small>
  </div>
</section>
""".strip()


def render_replay_strip(case: dict[str, Any], move: dict[str, Any] | None) -> str:
    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    entry = num(source.get("entry_price"), 8)
    exit_price = num(source.get("exit_price"), 8)
    open_price = num((move or {}).get("open"), 8)
    close_price = num((move or {}).get("close"), 8)
    high = num((move or {}).get("high"), 8)
    low = num((move or {}).get("low"), 8)
    change = num((move or {}).get("change_pct"))
    amp = num((move or {}).get("amplitude_pct"))
    pnl = num(case.get("pnl_usd"))
    side = side_from_case(case)
    market_dir = "long" if change > 0 else "short" if change < 0 else ""
    align = "同向" if side and market_dir and side == market_dir else "反向" if side and market_dir else "未知"
    align_class = "pos" if align == "同向" else "neg" if align == "反向" else ""
    duration = duration_label(source.get("entry_time"), source.get("exit_time"))

    if not move or high <= low:
        return f"""
<div class="replay-strip muted-strip">
  <div class="replay-title"><b>样本回放</b><span>缺少该币种市场快照，仅展示交易上下文</span></div>
  <div class="replay-facts">
    <span>方向 {h(side or '-')}</span>
    <span>持仓 {h(duration)}</span>
    <span>入场 {h(fmt_price(source.get('entry_price')))}</span>
    <span>离场 {h(fmt_price(source.get('exit_price')))}</span>
  </div>
</div>
""".strip()

    entry_pos = clamp((entry - low) / (high - low) * 100) if entry else 50
    exit_pos = clamp((exit_price - low) / (high - low) * 100) if exit_price else entry_pos
    open_pos = clamp((open_price - low) / (high - low) * 100) if open_price else 0
    close_pos = clamp((close_price - low) / (high - low) * 100) if close_price else 100
    low_label = fmt_price(low)
    high_label = fmt_price(high)
    return f"""
<div class="replay-strip">
  <div class="replay-title">
    <b>样本回放</b>
    <span class="{align_class}">{align}开仓</span>
    <span class="{'pos' if change >= 0 else 'neg'}">日涨跌 {change:+.2f}%</span>
    <span>振幅 {amp:.2f}%</span>
    <span>持仓 {h(duration)}</span>
  </div>
  <div class="price-track" aria-label="当日价格区间 {h(low_label)} 到 {h(high_label)}">
    <span class="track-line"></span>
    <span class="track-dot open-dot" style="left:{open_pos:.1f}%;" title="开盘 {h(fmt_price(open_price))}"></span>
    <span class="track-dot close-dot" style="left:{close_pos:.1f}%;" title="收盘 {h(fmt_price(close_price))}"></span>
    <span class="trade-marker entry-dot" style="left:{entry_pos:.1f}%;" title="入场 {h(fmt_price(entry))}">入</span>
    <span class="trade-marker exit-dot" style="left:{exit_pos:.1f}%;" title="离场 {h(fmt_price(exit_price))}">出</span>
  </div>
  <div class="replay-scale"><span>低 {h(low_label)}</span><span>高 {h(high_label)}</span></div>
  <div class="replay-facts">
    <span>开 {h(fmt_price(open_price))}</span>
    <span>收 {h(fmt_price(close_price))}</span>
    <span>入 {h(fmt_price(entry))}</span>
    <span>出 {h(fmt_price(exit_price))}</span>
    <span class="{'pos' if pnl >= 0 else 'neg'}">PnL {pnl:+.2f}</span>
  </div>
</div>
""".strip()


def render_case(case: dict[str, Any], market_index: dict[str, dict[str, Any]] | None = None) -> str:
    pnl = num(case.get("pnl_usd"))
    move = num(case.get("move_pct"))
    confidence = int(num(case.get("confidence")) * 100)
    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    market_index = market_index or {}
    market_move = market_index.get(str(case.get("symbol") or ""))
    return f"""
<details class="case-card" open>
  <summary>
    <span class="case-kind">{case_type_label(str(case.get("case_type") or ""))}</span>
    <strong>{h(case.get("symbol"))}</strong>
    <span>{h(case.get("strategy"))}</span>
    <span class="{'pos' if pnl >= 0 else 'neg'}">{pnl:+.2f}</span>
  </summary>
  <div class="case-body">
    <p class="case-lesson">{h(case.get("lesson"))}</p>
    {render_replay_strip(case, market_move)}
    <div class="case-grid">
      <div><b>方向</b><span>{h(case.get("actual_action"))} / 期望 {h(case.get("expected_direction"))}</span></div>
      <div><b>日内变化</b><span class="{'pos' if move >= 0 else 'neg'}">{move:+.2f}%</span></div>
      <div><b>阶段</b><span>{h(case.get("market_stage"))}</span></div>
      <div><b>卡点</b><span>{h(case.get("decision_category"))}</span></div>
      <div><b>置信</b><span>{confidence}%</span></div>
      <div><b>时间</b><span>{h(source.get("entry_time") or "-")} -> {h(source.get("exit_time") or "-")}</span></div>
    </div>
    <p class="reason">{h(case.get("reason"))}</p>
    <p class="stage-detail">{h(source.get("stage_detail") or case.get("attribution"))}</p>
  </div>
</details>
""".strip()


def render_evidence_chain(
    candidate: dict[str, Any],
    cases: dict[str, dict[str, Any]],
    market_index: dict[str, dict[str, Any]],
) -> str:
    rows = []
    for cid in candidate.get("source_cases") or []:
        case = cases.get(str(cid))
        if case:
            rows.append(render_case(case, market_index))
        else:
            rows.append(f"<p class=\"missing-case\">缺少案例: {h(cid)}</p>")
    if not rows:
        rows.append("<p class=\"empty\">这个候选暂时没有绑定源案例。</p>")
    return "<section class=\"evidence\"><h3>证据链</h3>" + "".join(rows) + "</section>"


def render_graph(candidate: dict[str, Any], source_cases: list[dict[str, Any]], result: dict[str, Any], review: dict[str, Any]) -> str:
    cid = str(candidate.get("candidate_id") or "")
    status = str(review.get("promotion_status") or result.get("promotion_status") or "generated")
    case_types = Counter(str(c.get("case_type") or "") for c in source_cases)
    symbols = Counter(str(c.get("symbol") or "") for c in source_cases)
    stage = Counter(str(c.get("market_stage") or "") for c in source_cases)
    nodes = [
        ("案例", " / ".join(f"{case_type_label(k)} {v}" for k, v in case_types.most_common(3)) or "无"),
        ("币种", " / ".join(f"{k} {v}" for k, v in symbols.most_common(3)) or "无"),
        ("阶段", stage.most_common(1)[0][0] if stage else "无"),
        ("候选", cid),
        ("实验", str(result.get("experiment_id") or "-")),
        ("门禁", status_label(status)),
    ]
    blocks = "".join(f"<div class=\"kg-node\"><span>{h(k)}</span><b>{h(v)}</b></div>" for k, v in nodes)
    return f"<section class=\"knowledge\"><h3>策略知识图谱</h3><div class=\"kg-flow\">{blocks}</div></section>"


def render_candidate_detail(candidate: dict[str, Any], data: dict[str, Any]) -> str:
    cid = str(candidate.get("candidate_id") or "")
    experiments = by_key(data["experiments"], "experiment_id")
    reviews = by_key(data["reviews"], "candidate_id")
    all_cases = cases_by_id(data["cases"])
    market_index = moves_by_symbol(data["market_moves"])
    result = experiments.get(cid, {})
    review = reviews.get(cid, {})
    source_cases = [all_cases[c] for c in candidate.get("source_cases") or [] if c in all_cases]
    params = json.dumps(candidate.get("params") or {}, ensure_ascii=False)
    gate = json.dumps(candidate.get("promotion_gate") or {}, ensure_ascii=False)
    return f"""
<article class="candidate-detail" id="candidate-{h(cid)}">
  <header>
    <div>
      <span class="eyebrow">候选假设</span>
      <h2>{h(candidate.get("problem"))}</h2>
      <p>{h(candidate.get("proposal"))}</p>
    </div>
    <div class="id-chip">{h(cid)}</div>
  </header>
  {render_gate(candidate, result, review)}
  {render_approval_panel(candidate, result, review)}
  <section class="rationale">
    <div><b>预期收益</b><p>{h(candidate.get("expected_effect"))}</p></div>
    <div><b>主要风险</b><p>{h(candidate.get("risk"))}</p></div>
    <div><b>参数</b><code>{h(params)}</code></div>
    <div><b>门禁</b><code>{h(gate)}</code></div>
  </section>
  {render_graph(candidate, source_cases, result, review)}
  {render_evidence_chain(candidate, all_cases, market_index)}
</article>
""".strip()


def render_strategy_lessons(data: dict[str, Any]) -> str:
    factor_rows = data["factor_lessons"]
    symbol_rows = sorted(data["symbol_lessons"], key=lambda r: (num(r.get("reverse_cases")) + num(r.get("missed_cases")), abs(num(r.get("pnl_usd")))), reverse=True)[:12]
    factors = "".join(
        f"<tr><td>{h(r.get('strategy'))}</td><td>{case_type_label(str(r.get('case_type') or ''))}</td><td>{h(r.get('count'))}</td><td>{h(r.get('top_attribution'))}</td></tr>"
        for r in factor_rows[:12]
    )
    symbols = "".join(
        f"<tr><td>{h(r.get('strategy'))}</td><td>{h(r.get('symbol'))}</td><td>{h(r.get('cases'))}</td><td>{signed(r.get('pnl_usd'))}</td><td>{h(' / '.join(r.get('lessons') or []))}</td></tr>"
        for r in symbol_rows
    )
    return f"""
<section class="lessons">
  <h2>策略知识库</h2>
  <div class="lesson-grid">
    <div>
      <h3>高频归因</h3>
      <table><thead><tr><th>策略</th><th>类型</th><th>次数</th><th>主要归因</th></tr></thead><tbody>{factors}</tbody></table>
    </div>
    <div>
      <h3>重点币种</h3>
      <table><thead><tr><th>策略</th><th>币种</th><th>案例</th><th>PnL</th><th>经验</th></tr></thead><tbody>{symbols}</tbody></table>
    </div>
  </div>
</section>
""".strip()


def render_market_replay(data: dict[str, Any]) -> str:
    moves = sorted(data["market_moves"], key=lambda r: abs(num(r.get("change_pct"))), reverse=True)[:16]
    snapshot = data.get("market_snapshot") if isinstance(data.get("market_snapshot"), dict) else {}
    date = snapshot.get("date") or data.get("snapshot", {}).get("date") or "-"
    if not moves:
        return """
<details class="replay" open><summary><h2>快照 / 回放联动</h2><span>暂无市场快照</span></summary><p class="empty">暂无市场快照。腾讯主节点生成 market_snapshot 后，这里会自动展示大行情样本。</p></details>
""".strip()
    cards = []
    for move in moves:
        change = num(move.get("change_pct"))
        amp = num(move.get("amplitude_pct"))
        cards.append(f"""
<article class="move-card">
  <strong>{h(move.get("symbol"))}</strong>
  <b class="{'pos' if change >= 0 else 'neg'}">{change:+.2f}%</b>
  <span>振幅 {amp:.2f}%</span>
  <span>区间 {h(fmt_price(move.get("low")))} - {h(fmt_price(move.get("high")))}</span>
  <small>成交额 {num(move.get("quote_volume"))/100000000:.2f} 亿</small>
</article>
""".strip())
    return f"""
<details class="replay" open>
  <summary><h2>快照 / 回放联动</h2><span>{h(date)} 大行情样本 {len(moves)} 个</span></summary>
  <div class="move-grid">{''.join(cards)}</div>
</details>
""".strip()


def render_html(data: dict[str, Any]) -> str:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    candidate_details = "".join(render_candidate_detail(c, data) for c in data["candidates"])
    if not candidate_details:
        candidate_details = "<section class=\"candidate-detail\"><h2>暂无候选</h2><p>继续积累案例后，系统会自动生成可审阅假设。</p></section>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>研究审阅台</title>
<style>
:root {{
  --bg:#f6f8fb; --panel:#ffffff; --text:#172033; --muted:#667085; --line:#d9e2ef;
  --up:#16a34a; --down:#dc2626; --watch:#b45309; --good-bg:#eaf7ee; --bad-bg:#fff1f2; --watch-bg:#fff7ed;
  --ink:#0f172a; --soft:#eef3f8;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif; }}
.shell {{ max-width:1440px; margin:0 auto; padding:24px; }}
.topbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:18px; }}
.back-link {{ display:inline-flex; align-items:center; gap:8px; text-decoration:none; color:#0f172a; background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px 12px; font-weight:700; }}
.back-link:hover {{ border-color:#94a3b8; background:#f8fbff; }}
.hero {{ display:flex; justify-content:space-between; align-items:flex-end; gap:20px; padding:8px 0 22px; border-bottom:1px solid var(--line); }}
.hero h1 {{ margin:0; font-size:30px; letter-spacing:0; }}
.hero p {{ margin:8px 0 0; color:var(--muted); line-height:1.6; }}
.timestamp {{ color:var(--muted); font-size:13px; white-space:nowrap; }}
.metrics {{ display:grid; grid-template-columns:repeat(7,1fr); gap:10px; margin:18px 0; }}
.metrics article {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; min-height:90px; }}
.metrics span,.metrics em {{ display:block; color:var(--muted); font-size:12px; font-style:normal; }}
.metrics b {{ display:block; font-size:26px; margin:8px 0; color:var(--ink); }}
.workspace {{ display:grid; grid-template-columns:320px 1fr; gap:18px; align-items:start; }}
.queue {{ position:sticky; top:16px; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; max-height:calc(100vh - 32px); overflow:auto; }}
.queue h2,.lessons h2,.replay h2 {{ margin:0 0 12px; font-size:18px; }}
.candidate-card {{ display:grid; grid-template-columns:34px 1fr; gap:10px; padding:12px; border:1px solid var(--line); border-radius:8px; color:inherit; text-decoration:none; margin-bottom:10px; background:#fff; }}
.candidate-card:hover {{ border-color:#94a3b8; }}
.candidate-card.good {{ background:var(--good-bg); }} .candidate-card.bad {{ background:var(--bad-bg); }} .candidate-card.watch {{ background:var(--watch-bg); }}
.queue-index {{ font-weight:700; color:var(--muted); }}
.candidate-card strong,.candidate-card small {{ display:block; }}
.candidate-card small,.candidate-card p {{ color:var(--muted); }}
.candidate-card p {{ grid-column:2; margin:6px 0; line-height:1.45; font-size:13px; }}
.candidate-card footer {{ grid-column:2; display:flex; justify-content:space-between; font-size:13px; }}
.candidate-detail,.lessons,.replay,.families {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px; margin-bottom:18px; }}
.families h2 {{ margin:0 0 12px; font-size:18px; }}
.family-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }}
.family-card {{ border:1px solid var(--line); border-radius:8px; padding:14px; background:#fbfdff; }}
.family-card.good {{ background:var(--good-bg); }} .family-card.bad {{ background:var(--bad-bg); }} .family-card.watch {{ background:var(--watch-bg); }}
.family-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
.family-head span {{ color:var(--muted); font-size:12px; }}
.family-head h3 {{ margin:5px 0 0; font-size:15px; overflow-wrap:anywhere; }}
.family-head b {{ font-size:13px; white-space:nowrap; }}
.family-metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:12px 0; }}
.family-metrics div {{ border:1px solid var(--line); border-radius:8px; padding:8px; background:rgba(255,255,255,.7); }}
.family-metrics span {{ display:block; color:var(--muted); font-size:11px; margin-bottom:5px; }}
.family-metrics strong {{ font-size:16px; }}
.replay > summary {{ display:flex; align-items:center; justify-content:space-between; gap:12px; cursor:pointer; list-style:none; }}
.replay > summary::-webkit-details-marker {{ display:none; }}
.candidate-detail header {{ display:flex; justify-content:space-between; gap:18px; border-bottom:1px solid var(--line); padding-bottom:14px; }}
.eyebrow {{ color:var(--muted); font-size:12px; font-weight:700; }}
.candidate-detail h2 {{ margin:6px 0 8px; font-size:22px; }}
.candidate-detail p {{ line-height:1.65; color:#344054; }}
.id-chip {{ align-self:flex-start; padding:8px 10px; border-radius:8px; background:var(--soft); color:#334155; font-size:12px; max-width:360px; overflow-wrap:anywhere; }}
.gate {{ display:grid; grid-template-columns:220px 1fr; gap:14px; margin:16px 0; }}
.gate-status {{ border-radius:8px; padding:16px; border:1px solid var(--line); }}
.gate-status span {{ display:block; color:var(--muted); font-size:12px; }}
.gate-status b {{ display:block; margin-top:8px; font-size:22px; }}
.gate-status.good {{ background:var(--good-bg); color:#166534; }} .gate-status.bad {{ background:var(--bad-bg); color:#991b1b; }} .gate-status.watch {{ background:var(--watch-bg); color:#92400e; }}
.gate dl,.rationale,.case-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin:0; }}
.gate dl div,.rationale div,.case-grid div {{ border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfdff; }}
dt,.case-grid b,.rationale b {{ display:block; color:var(--muted); font-size:12px; margin-bottom:6px; }}
dd {{ margin:0; font-weight:700; }}
code {{ display:block; white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; color:#334155; }}
.approval-panel {{ margin:16px 0; border:1px solid var(--line); border-radius:8px; padding:14px; background:#f8fafc; }}
.approval-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }}
.approval-head h3 {{ margin:0 0 6px; font-size:16px; }}
.approval-head p {{ margin:0; color:var(--muted); font-size:13px; }}
.approval-head span {{ color:var(--muted); font-size:12px; overflow-wrap:anywhere; text-align:right; max-width:360px; }}
.approval-actions {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:10px; }}
.approval-actions button,.copy-approval {{ border:1px solid var(--line); border-radius:8px; padding:9px 10px; background:#fff; color:#172033; font-weight:700; cursor:pointer; }}
.approval-actions button:hover,.copy-approval:hover {{ border-color:#94a3b8; }}
.approval-json {{ width:100%; resize:vertical; border:1px solid var(--line); border-radius:8px; padding:10px; background:#fff; color:#334155; font-family:"SFMono-Regular",Consolas,monospace; font-size:12px; line-height:1.45; }}
.approval-foot {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-top:10px; }}
.approval-foot small {{ color:var(--muted); }}
.knowledge,.evidence {{ margin-top:18px; }}
.knowledge h3,.evidence h3,.lessons h3 {{ margin:0 0 10px; font-size:16px; }}
.kg-flow {{ display:grid; grid-template-columns:repeat(6,1fr); gap:8px; }}
.kg-node {{ min-height:74px; border:1px solid var(--line); border-radius:8px; padding:10px; background:#f8fafc; position:relative; }}
.kg-node span {{ color:var(--muted); font-size:12px; display:block; }}
.kg-node b {{ display:block; margin-top:8px; font-size:13px; overflow-wrap:anywhere; }}
.case-card {{ border:1px solid var(--line); border-radius:8px; margin-bottom:10px; background:#fff; overflow:hidden; }}
.case-card summary {{ display:grid; grid-template-columns:110px 1fr 90px 80px; gap:10px; align-items:center; cursor:pointer; padding:12px 14px; background:#f8fafc; }}
.case-kind {{ color:#334155; font-size:12px; font-weight:700; }}
.case-body {{ padding:14px; }}
.case-lesson {{ margin:0 0 10px; font-weight:600; color:#1f2937; }}
.replay-strip {{ border:1px solid var(--line); border-radius:8px; background:#f8fafc; padding:12px; margin:12px 0; }}
.replay-strip.muted-strip {{ opacity:.9; }}
.replay-title {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; font-size:12px; color:var(--muted); margin-bottom:10px; }}
.replay-title b {{ color:#334155; font-size:13px; }}
.replay-title .pos {{ color:var(--up); }} .replay-title .neg {{ color:var(--down); }}
.price-track {{ position:relative; height:54px; margin:4px 0 8px; }}
.track-line {{ position:absolute; left:0; right:0; top:26px; height:4px; border-radius:999px; background:linear-gradient(90deg,#22c55e,#f59e0b,#ef4444); opacity:.75; }}
.track-dot {{ position:absolute; top:19px; width:16px; height:16px; border-radius:999px; transform:translateX(-50%); border:2px solid #fff; box-shadow:0 1px 4px rgba(15,23,42,.2); }}
.open-dot {{ background:#60a5fa; }}
.close-dot {{ background:#94a3b8; }}
.trade-marker {{ position:absolute; top:-3px; transform:translateX(-50%); padding:3px 6px; border-radius:999px; color:#fff; font-size:11px; font-weight:800; box-shadow:0 1px 4px rgba(15,23,42,.22); }}
.entry-dot {{ background:#06b6d4; }}
.exit-dot {{ background:#f97316; }}
.replay-scale,.replay-facts {{ display:flex; justify-content:space-between; gap:10px; font-size:12px; color:var(--muted); flex-wrap:wrap; }}
.replay-facts {{ margin-top:8px; }}
.replay-facts span {{ padding:4px 8px; background:#fff; border:1px solid var(--line); border-radius:999px; }}
.good-badge {{ color:var(--up); }}
.bad-badge {{ color:var(--down); }}
.reason,.stage-detail {{ margin:10px 0 0; color:var(--muted); font-size:13px; }}
.pos {{ color:var(--up); }} .neg {{ color:var(--down); }}
.lesson-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
table {{ width:100%; border-collapse:collapse; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:13px; }}
th {{ background:#f1f5f9; color:#334155; }}
.move-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }}
.move-card {{ border:1px solid var(--line); border-radius:8px; padding:12px; background:#fbfdff; }}
.move-card strong,.move-card b,.move-card span,.move-card small {{ display:block; }}
.move-card b {{ font-size:20px; margin:8px 0; }}
.empty,.missing-case {{ color:var(--muted); background:#f8fafc; border:1px dashed var(--line); border-radius:8px; padding:12px; }}
@media (max-width:1100px) {{ .metrics {{ grid-template-columns:repeat(3,1fr); }} .workspace {{ grid-template-columns:1fr; }} .queue {{ position:static; max-height:none; }} .kg-flow,.move-grid,.family-grid {{ grid-template-columns:repeat(2,1fr); }} .approval-actions {{ grid-template-columns:repeat(2,1fr); }} }}
@media (max-width:760px) {{ .shell {{ padding:14px; }} .hero,.candidate-detail header,.replay > summary,.approval-head,.approval-foot {{ flex-direction:column; align-items:flex-start; }} .metrics,.gate,.gate dl,.rationale,.case-grid,.lesson-grid,.kg-flow,.move-grid,.family-grid,.family-metrics,.approval-actions {{ grid-template-columns:1fr; }} .case-card summary {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<main class="shell">
  <div class="topbar">{back_button()}</div>
  <section class="hero">
    <div>
      <h1>研究审阅台</h1>
      <p>把亏损、错过、反向样本沉淀为候选假设，再用影子实验和晋级门禁决定是否值得人工审批。</p>
    </div>
    <div class="timestamp">生成时间 {h(now)}</div>
  </section>
  {render_metrics(data)}
  <section class="workspace">
    {render_candidate_queue(data)}
    <div>
      {render_family_management(data)}
      {candidate_details}
      {render_strategy_lessons(data)}
      {render_market_replay(data)}
    </div>
  </section>
</main>
<script>
function updateApproval(panel, action, scope, nextStep) {{
  const box = panel.querySelector(".approval-json");
  if (!box) return;
  let payload = {{}};
  try {{ payload = JSON.parse(box.value); }} catch (err) {{ payload = {{}}; }}
  payload.manual_action = action;
  payload.approved_scope = scope;
  payload.next_step = nextStep;
  payload.updated_at = new Date().toISOString();
  box.value = JSON.stringify(payload, null, 2);
}}
document.querySelectorAll(".approval-panel").forEach((panel) => {{
  panel.querySelectorAll(".approval-actions button").forEach((button) => {{
    button.addEventListener("click", () => updateApproval(panel, button.dataset.action, button.dataset.scope, button.dataset.next));
  }});
  const copyButton = panel.querySelector(".copy-approval");
  if (copyButton) {{
    copyButton.addEventListener("click", async () => {{
      const box = panel.querySelector(".approval-json");
      if (!box) return;
      try {{
        await navigator.clipboard.writeText(box.value.replace(/\\n/g, "") + "\\n");
        copyButton.textContent = "已复制";
        setTimeout(() => copyButton.textContent = "复制审批记录", 1200);
      }} catch (err) {{
        box.select();
        document.execCommand("copy");
      }}
    }});
  }}
}});
</script>
</body>
</html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成研究审阅台 HTML")
    parser.add_argument("--memory-dir", default=str(DEFAULT_MEMORY_DIR))
    parser.add_argument("--experiment-results", default=str(DEFAULT_EXPERIMENT_RESULTS))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args(argv)

    data = load_dashboard_data(Path(args.memory_dir), Path(args.experiment_results))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(CST).strftime("%Y-%m-%d")
    html_text = render_html(data)
    path = out_dir / f"research_review_{date_str}.html"
    path.write_text(html_text, encoding="utf-8")
    latest = out_dir / "research_review_latest.html"
    latest.write_text(html_text, encoding="utf-8")
    print(f"研究审阅台已生成: {path}")
    print(f"Latest: {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
