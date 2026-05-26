from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.example.json"
REPORT_DIR = ROOT / "reports"


@dataclass
class HttpResult:
    ok: bool
    data: Any = None
    error: str = ""
    ms: float = 0.0


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def request_json(url: str, timeout: int, user_agent: str) -> HttpResult:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = (time.perf_counter() - started) * 1000
            if not raw:
                return HttpResult(True, None, ms=elapsed)
            return HttpResult(True, json.loads(raw.decode("utf-8")), ms=elapsed)
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return HttpResult(False, error=f"HTTP {exc.code}: {exc.reason} {detail}".strip(), ms=elapsed)
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return HttpResult(False, error=f"{type(exc).__name__}: {exc}", ms=elapsed)


def parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        number = float(value)
        if math.isfinite(number):
            return number
    except Exception:
        pass
    return default


def best_bid_ask(book: dict[str, Any]) -> dict[str, Any]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid_levels = [(to_float(x.get("price")), to_float(x.get("size"))) for x in bids if isinstance(x, dict)]
    ask_levels = [(to_float(x.get("price")), to_float(x.get("size"))) for x in asks if isinstance(x, dict)]
    bid_levels = [(p, s) for p, s in bid_levels if p > 0 and s > 0]
    ask_levels = [(p, s) for p, s in ask_levels if p > 0 and s > 0]
    best_bid = max(bid_levels, default=(0.0, 0.0), key=lambda item: item[0])
    best_ask = min(ask_levels, default=(0.0, 0.0), key=lambda item: item[0])
    return {
        "best_bid": best_bid[0],
        "best_bid_size": best_bid[1],
        "best_ask": best_ask[0],
        "best_ask_size": best_ask[1],
        "bid_levels": len(bid_levels),
        "ask_levels": len(ask_levels),
        "tick_size": book.get("tick_size"),
        "min_order_size": to_float(book.get("min_order_size")),
        "book_timestamp": book.get("timestamp"),
    }


def normalize_market(raw: dict[str, Any]) -> dict[str, Any]:
    outcomes = parse_jsonish(raw.get("outcomes")) or []
    prices = parse_jsonish(raw.get("outcomePrices")) or []
    token_ids = parse_jsonish(raw.get("clobTokenIds")) or []
    if isinstance(outcomes, str):
        outcomes = [outcomes]
    if isinstance(prices, str):
        prices = [prices]
    if isinstance(token_ids, str):
        token_ids = [token_ids]
    return {
        "id": str(raw.get("id") or ""),
        "question": raw.get("question") or raw.get("title") or "",
        "slug": raw.get("slug") or "",
        "description": raw.get("description") or "",
        "end_date": raw.get("endDate") or "",
        "liquidity": to_float(raw.get("liquidity") or raw.get("liquidityNum")),
        "volume": to_float(raw.get("volume") or raw.get("volumeNum")),
        "active": raw.get("active"),
        "closed": raw.get("closed"),
        "enable_order_book": bool(raw.get("enableOrderBook")),
        "outcomes": outcomes,
        "outcome_prices": [to_float(x) for x in prices],
        "token_ids": [str(x) for x in token_ids],
    }


def market_matches(market: dict[str, Any], includes: list[str], excludes: list[str]) -> bool:
    text = " ".join(
        [
            market.get("question", ""),
            market.get("slug", ""),
            market.get("description", ""),
            " ".join(str(x) for x in market.get("outcomes", [])),
        ]
    ).lower()
    def has_keyword(keyword: str) -> bool:
        keyword = keyword.lower().strip()
        if not keyword:
            return False
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None

    if excludes and any(has_keyword(k) for k in excludes):
        return False
    if not includes:
        return True
    return any(has_keyword(k) for k in includes)


def fetch_markets(config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(int(config.get("market_limit", 80))),
    }
    url = f"{config['gamma_base_url'].rstrip('/')}/markets?{urllib.parse.urlencode(params)}"
    result = request_json(url, int(config["request_timeout_seconds"]), config["user_agent"])
    health = {"url": url, "ok": result.ok, "latency_ms": round(result.ms, 1), "error": result.error}
    if not result.ok:
        return [], health
    data = result.data if isinstance(result.data, list) else result.data.get("data", [])
    markets = [normalize_market(x) for x in data if isinstance(x, dict)]
    min_liq = to_float(config.get("min_liquidity_usd"), 0)
    includes = list(config.get("include_keywords") or [])
    excludes = list(config.get("exclude_keywords") or [])
    filtered = [
        m
        for m in markets
        if m["enable_order_book"]
        and len(m["token_ids"]) == 2
        and len(m["outcomes"]) == 2
        and m["liquidity"] >= min_liq
        and market_matches(m, includes, excludes)
    ]
    filtered.sort(key=lambda m: (m["liquidity"], m["volume"]), reverse=True)
    return filtered, health


def fetch_book(config: dict[str, Any], token_id: str) -> HttpResult:
    base = config["clob_base_url"].rstrip("/")
    url = f"{base}/book?{urllib.parse.urlencode({'token_id': token_id})}"
    return request_json(url, int(config["request_timeout_seconds"]), config["user_agent"])


def analyze_market(config: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    outcomes = market["outcomes"]
    token_ids = market["token_ids"]
    books = []
    errors = []
    for outcome, token_id in zip(outcomes, token_ids):
        result = fetch_book(config, token_id)
        if not result.ok or not isinstance(result.data, dict):
            errors.append({"outcome": outcome, "token_id": token_id, "error": result.error, "latency_ms": round(result.ms, 1)})
            continue
        quote = best_bid_ask(result.data)
        quote.update({"outcome": outcome, "token_id": token_id, "latency_ms": round(result.ms, 1)})
        books.append(quote)

    analysis = {
        "market_id": market["id"],
        "question": market["question"],
        "slug": market["slug"],
        "end_date": market["end_date"],
        "liquidity": market["liquidity"],
        "volume": market["volume"],
        "outcomes": outcomes,
        "gamma_prices": market["outcome_prices"],
        "books": books,
        "errors": errors,
        "opportunities": [],
    }
    if len(books) != 2:
        return analysis

    first, second = books
    ask_sum = first["best_ask"] + second["best_ask"]
    bid_sum = first["best_bid"] + second["best_bid"]
    min_buy_size = min(first["best_ask_size"], second["best_ask_size"])
    min_sell_size = min(first["best_bid_size"], second["best_bid_size"])
    min_exec = to_float(config.get("min_executable_usd"), 5)
    min_edge = to_float(config.get("min_gross_edge_pct"), 0.25) / 100.0

    if first["best_ask"] > 0 and second["best_ask"] > 0:
        edge = 1.0 - ask_sum
        executable_usd = min_buy_size * ask_sum
        if edge >= min_edge and executable_usd >= min_exec:
            analysis["opportunities"].append(
                {
                    "type": "buy_both",
                    "gross_edge_pct": round(edge * 100, 4),
                    "ask_sum": round(ask_sum, 4),
                    "executable_shares": round(min_buy_size, 4),
                    "estimated_cost_usd": round(executable_usd, 4),
                    "estimated_gross_profit_usd": round(min_buy_size * edge, 4),
                    "note": "买入两个互斥结果，若最终必有一个结果结算为 1，则这是结构性毛边际。",
                }
            )
    if first["best_bid"] > 0 and second["best_bid"] > 0:
        edge = bid_sum - 1.0
        executable_usd = min_sell_size * bid_sum
        if edge >= min_edge and executable_usd >= min_exec:
            analysis["opportunities"].append(
                {
                    "type": "sell_both_inventory_required",
                    "gross_edge_pct": round(edge * 100, 4),
                    "bid_sum": round(bid_sum, 4),
                    "executable_shares": round(min_sell_size, 4),
                    "estimated_credit_usd": round(executable_usd, 4),
                    "estimated_gross_profit_usd": round(min_sell_size * edge, 4),
                    "note": "卖出两个结果需要已有库存/可卖份额，第一版只记录，不视作可直接执行。",
                }
            )
    return analysis


def summarize(markets: list[dict[str, Any]], analyses: list[dict[str, Any]], health: dict[str, Any]) -> dict[str, Any]:
    opportunities = [op | {"question": item["question"], "slug": item["slug"]} for item in analyses for op in item["opportunities"]]
    opportunities.sort(key=lambda x: (x.get("estimated_gross_profit_usd", 0), x.get("gross_edge_pct", 0)), reverse=True)
    near_misses = build_near_misses(analyses)
    book_errors = sum(len(x["errors"]) for x in analyses)
    checked = len(analyses)
    liquid = [x["liquidity"] for x in markets]
    return {
        "generated_at": now_utc().isoformat(),
        "mode": "shadow",
        "health": health,
        "markets_discovered": len(markets),
        "markets_checked": checked,
        "book_errors": book_errors,
        "opportunity_count": len(opportunities),
        "best_opportunities": opportunities[:20],
        "near_misses": near_misses[:20],
        "liquidity_checked_usd": round(sum(liquid), 2),
        "conclusion": build_conclusion(len(opportunities), book_errors, checked),
    }


def build_near_misses(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in analyses:
        books = item.get("books", [])
        if len(books) != 2:
            continue
        first, second = books
        if first["best_ask"] > 0 and second["best_ask"] > 0:
            ask_sum = first["best_ask"] + second["best_ask"]
            rows.append(
                {
                    "type": "buy_both_gap",
                    "gap_to_profit_pct": round(max(0.0, ask_sum - 1.0) * 100, 4),
                    "combined_price": round(ask_sum, 4),
                    "question": item["question"],
                    "slug": item["slug"],
                    "liquidity": item["liquidity"],
                }
            )
        if first["best_bid"] > 0 and second["best_bid"] > 0:
            bid_sum = first["best_bid"] + second["best_bid"]
            rows.append(
                {
                    "type": "sell_both_gap",
                    "gap_to_profit_pct": round(max(0.0, 1.0 - bid_sum) * 100, 4),
                    "combined_price": round(bid_sum, 4),
                    "question": item["question"],
                    "slug": item["slug"],
                    "liquidity": item["liquidity"],
                }
            )
    rows.sort(key=lambda x: (x["gap_to_profit_pct"], -x["liquidity"]))
    return rows


def build_conclusion(opportunity_count: int, book_errors: int, checked: int) -> str:
    if checked == 0:
        return "未完成有效检查：没有符合条件的二元订单簿市场，或 API 不可用。"
    if opportunity_count == 0:
        return "本轮未发现达到阈值的结构性毛套利；继续记录多轮样本后再判断频率。"
    if book_errors > checked:
        return "发现毛套利机会，但盘口错误较多，需要先提升数据稳定性。"
    return "发现结构性毛套利毛边际；仍需用连续监控验证持续时间、成交排队和滑点。"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def render_md(payload: dict[str, Any], analyses: list[dict[str, Any]]) -> str:
    lines = [
        "# Polymarket Probe",
        "",
        f"- 生成时间: `{payload['generated_at']}`",
        f"- 模式: `{payload['mode']}`",
        f"- 结论: {payload['conclusion']}",
        f"- 市场发现/检查: {payload['markets_discovered']} / {payload['markets_checked']}",
        f"- 盘口错误: {payload['book_errors']}",
        f"- 机会数: {payload['opportunity_count']}",
        "",
        "## API 健康",
        "",
        f"- Gamma: {'OK' if payload['health'].get('ok') else 'FAIL'}，延迟 {payload['health'].get('latency_ms')} ms",
    ]
    if payload["health"].get("error"):
        lines.append(f"- 错误: `{payload['health']['error']}`")
    lines += ["", "## 机会", ""]
    if not payload["best_opportunities"]:
        lines.append("本轮无达到阈值的结构性毛套利机会。")
    else:
        lines.append("| 类型 | 毛边际 | 预计毛利 | 可执行份额 | 市场 |")
        lines.append("|---|---:|---:|---:|---|")
        for op in payload["best_opportunities"]:
            lines.append(
                f"| {op['type']} | {op['gross_edge_pct']}% | {op.get('estimated_gross_profit_usd', 0)} | "
                f"{op.get('executable_shares', 0)} | {op['question']} |"
            )
    lines += ["", "## 最接近盈利的盘口", ""]
    if not payload.get("near_misses"):
        lines.append("无可比较盘口。")
    else:
        lines.append("| 类型 | 距离毛盈利 | 合计价格 | 市场 |")
        lines.append("|---|---:|---:|---|")
        for item in payload["near_misses"][:10]:
            lines.append(
                f"| {item['type']} | {item['gap_to_profit_pct']}% | {item['combined_price']} | {item['question']} |"
            )
    lines += ["", "## 已检查市场样本", ""]
    lines.append("| 市场 | 流动性 | Yes/Outcome A | No/Outcome B | 机会 |")
    lines.append("|---|---:|---|---|---:|")
    for item in analyses[:50]:
        book_text = []
        for book in item["books"]:
            book_text.append(
                f"{book['outcome']}: bid {book['best_bid']:.3f} / ask {book['best_ask']:.3f}"
            )
        while len(book_text) < 2:
            book_text.append("无盘口")
        lines.append(
            f"| {item['question']} | {item['liquidity']:.2f} | {book_text[0]} | {book_text[1]} | {len(item['opportunities'])} |"
        )
    return "\n".join(lines) + "\n"


def render_html(payload: dict[str, Any], analyses: list[dict[str, Any]]) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value))

    rows = []
    for item in analyses[:80]:
        books = item["books"]
        cells = []
        for book in books[:2]:
            spread = book["best_ask"] - book["best_bid"] if book["best_ask"] and book["best_bid"] else 0
            cells.append(
                f"<div><b>{esc(book['outcome'])}</b> bid {book['best_bid']:.3f} / ask {book['best_ask']:.3f}"
                f"<span>spread {spread:.3f}</span></div>"
            )
        while len(cells) < 2:
            cells.append("<div>无盘口</div>")
        cls = "hot" if item["opportunities"] else ""
        rows.append(
            f"<tr class='{cls}'><td>{esc(item['question'])}<small>{esc(item['slug'])}</small></td>"
            f"<td>{item['liquidity']:.2f}</td><td>{cells[0]}</td><td>{cells[1]}</td>"
            f"<td>{len(item['opportunities'])}</td></tr>"
        )
    op_cards = []
    for op in payload["best_opportunities"]:
        op_cards.append(
            f"<section class='card hot'><h3>{esc(op['type'])}: {op['gross_edge_pct']}%</h3>"
            f"<p>{esc(op['question'])}</p>"
            f"<dl><dt>预计毛利</dt><dd>{op.get('estimated_gross_profit_usd', 0)}</dd>"
            f"<dt>可执行份额</dt><dd>{op.get('executable_shares', 0)}</dd></dl>"
            f"<small>{esc(op.get('note', ''))}</small></section>"
        )
    if not op_cards:
        op_cards.append("<section class='card'><h3>暂无机会</h3><p>本轮没有达到阈值的结构性毛套利。</p></section>")
    near_rows = []
    for item in payload.get("near_misses", [])[:12]:
        near_rows.append(
            f"<tr><td>{esc(item['type'])}</td><td>{item['gap_to_profit_pct']}%</td>"
            f"<td>{item['combined_price']}</td><td>{esc(item['question'])}</td></tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Probe</title>
<style>
:root {{ --bg:#f6f7f8; --text:#101828; --muted:#667085; --line:#d0d5dd; --up:#16a34a; --down:#dc2626; --warn:#b45309; --card:#fff; }}
body {{ margin:0; font-family:Arial, "Microsoft YaHei", sans-serif; color:var(--text); background:var(--bg); }}
main {{ max-width:1180px; margin:0 auto; padding:24px; }}
h1 {{ margin:0 0 6px; font-size:28px; }}
.summary {{ display:grid; grid-template-columns:repeat(5, minmax(120px,1fr)); gap:10px; margin:18px 0; }}
.metric,.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }}
.metric b {{ display:block; font-size:22px; margin-top:6px; }}
.muted, small, span {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(260px,1fr)); gap:12px; margin:12px 0 20px; }}
.hot {{ border-color:#f59e0b; background:#fffbeb; }}
table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
th,td {{ padding:10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ background:#eef2f6; font-size:13px; }}
td:nth-child(2),td:nth-child(5) {{ text-align:right; }}
small {{ display:block; margin-top:4px; }}
dl {{ display:grid; grid-template-columns:auto 1fr; gap:4px 10px; }}
dt {{ color:var(--muted); }}
@media (max-width:760px) {{ main {{ padding:14px; }} .summary {{ grid-template-columns:1fr 1fr; }} table {{ font-size:13px; }} }}
</style>
</head>
<body><main>
<h1>Polymarket Probe</h1>
<p class="muted">生成时间 {esc(payload['generated_at'])}，只读影子模式，不执行下单。</p>
<section class="card"><b>结论</b><p>{esc(payload['conclusion'])}</p></section>
<div class="summary">
<div class="metric">市场发现<b>{payload['markets_discovered']}</b></div>
<div class="metric">已检查<b>{payload['markets_checked']}</b></div>
<div class="metric">机会数<b>{payload['opportunity_count']}</b></div>
<div class="metric">盘口错误<b>{payload['book_errors']}</b></div>
<div class="metric">Gamma延迟<b>{payload['health'].get('latency_ms')}ms</b></div>
</div>
<h2>机会</h2>
<div class="cards">{''.join(op_cards)}</div>
<h2>最接近盈利的盘口</h2>
<table><thead><tr><th>类型</th><th>距离毛盈利</th><th>合计价格</th><th>市场</th></tr></thead><tbody>
{''.join(near_rows) if near_rows else '<tr><td colspan="4">无可比较盘口</td></tr>'}
</tbody></table>
<h2>盘口样本</h2>
<table><thead><tr><th>市场</th><th>流动性</th><th>Outcome A</th><th>Outcome B</th><th>机会</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Polymarket profitability probe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-orderbooks", type=int, default=None)
    parser.add_argument("--all-markets", action="store_true", help="Ignore include_keywords and scan every eligible binary market.")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.all_markets:
        config["include_keywords"] = []
    max_books = args.max_orderbooks or int(config.get("max_orderbooks", 80))
    markets, health = fetch_markets(config)
    selected = markets[:max_books]
    analyses = []
    for market in selected:
        analyses.append(analyze_market(config, market))
        time.sleep(0.08)
    payload = summarize(markets, analyses, health)
    payload["config"] = {
        "market_limit": config.get("market_limit"),
        "max_orderbooks": max_books,
        "min_liquidity_usd": config.get("min_liquidity_usd"),
        "min_gross_edge_pct": config.get("min_gross_edge_pct"),
        "min_executable_usd": config.get("min_executable_usd"),
        "include_keywords": config.get("include_keywords"),
    }
    payload["markets"] = analyses

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%d_%H%M%S")
    write_json(REPORT_DIR / f"polymarket_probe_{stamp}.json", payload)
    write_json(REPORT_DIR / "polymarket_probe_latest.json", payload)
    (REPORT_DIR / f"polymarket_probe_{stamp}.md").write_text(render_md(payload, analyses), encoding="utf-8")
    (REPORT_DIR / "polymarket_probe_latest.md").write_text(render_md(payload, analyses), encoding="utf-8")
    (REPORT_DIR / f"polymarket_probe_{stamp}.html").write_text(render_html(payload, analyses), encoding="utf-8")
    (REPORT_DIR / "polymarket_probe_latest.html").write_text(render_html(payload, analyses), encoding="utf-8")

    print(json.dumps({k: payload[k] for k in ["generated_at", "conclusion", "markets_checked", "opportunity_count", "book_errors"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
