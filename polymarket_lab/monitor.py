from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports"
DEFAULT_CONFIG = ROOT / "config.example.json"
PROBE_DETAIL_RE = re.compile(r"^(polymarket_probe_\d{8}_\d{6})\.(?:json|md|html)$")


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def prune_probe_reports(keep_runs: int) -> int:
    if keep_runs < 1:
        return 0
    stems: set[str] = set()
    for path in REPORT_DIR.glob("polymarket_probe_*.*"):
        match = PROBE_DETAIL_RE.match(path.name)
        if match:
            stems.add(match.group(1))
    expired = sorted(stems)[:-keep_runs]
    removed = 0
    for stem in expired:
        for suffix in (".json", ".md", ".html"):
            path = REPORT_DIR / f"{stem}{suffix}"
            if path.exists():
                path.unlink()
                removed += 1
    return removed


def trim_jsonl(path: Path, keep_lines: int) -> int:
    if keep_lines < 1 or not path.exists():
        return 0
    retained: deque[str] = deque(maxlen=keep_lines)
    total = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            retained.append(line)
            total += 1
    removed = total - len(retained)
    if removed <= 0:
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.writelines(retained)
    tmp.replace(path)
    return removed


def load_latest() -> dict[str, Any]:
    path = REPORT_DIR / "polymarket_probe_latest.json"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(ROOT / "probe.py"),
        "--config",
        str(args.config),
        "--max-orderbooks",
        str(args.max_orderbooks),
    ]
    if args.all_markets:
        cmd.append("--all-markets")
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=args.run_timeout_seconds)
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        return {
            "generated_at": utc_stamp(),
            "ok": False,
            "elapsed_seconds": round(elapsed, 2),
            "error": (proc.stderr or proc.stdout)[-2000:],
        }
    latest = load_latest()
    health = latest.get("health") or {}
    markets_checked = latest.get("markets_checked", 0)
    ok = bool(health.get("ok")) and markets_checked > 0
    return {
        "generated_at": latest.get("generated_at") or utc_stamp(),
        "ok": ok,
        "elapsed_seconds": round(elapsed, 2),
        "conclusion": latest.get("conclusion"),
        "markets_checked": markets_checked,
        "opportunity_count": latest.get("opportunity_count", 0),
        "book_errors": latest.get("book_errors", 0),
        "health": health,
        "error": "" if ok else health.get("error", "no markets checked"),
        "best_opportunities": latest.get("best_opportunities", [])[:5],
        "near_misses": latest.get("near_misses", [])[:5],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous read-only Polymarket monitor.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--max-orderbooks", type=int, default=80)
    parser.add_argument("--all-markets", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--run-timeout-seconds", type=int, default=300)
    parser.add_argument("--keep-probe-runs", type=int, default=288)
    parser.add_argument("--keep-summary-lines", type=int, default=25920)
    args = parser.parse_args()

    while True:
        summary = run_probe(args)
        append_jsonl(REPORT_DIR / "polymarket_monitor_summary.jsonl", summary)
        for opportunity in summary.get("best_opportunities", []) or []:
            append_jsonl(
                REPORT_DIR / "polymarket_opportunities.jsonl",
                {
                    "seen_at": summary["generated_at"],
                    **opportunity,
                },
            )
        pruned_files = prune_probe_reports(args.keep_probe_runs)
        trimmed_summaries = trim_jsonl(
            REPORT_DIR / "polymarket_monitor_summary.jsonl", args.keep_summary_lines
        )
        status = "OK" if summary.get("ok") else "FAIL"
        print(
            json.dumps(
                {
                    "status": status,
                    "generated_at": summary.get("generated_at"),
                    "markets_checked": summary.get("markets_checked", 0),
                    "opportunity_count": summary.get("opportunity_count", 0),
                    "elapsed_seconds": summary.get("elapsed_seconds"),
                    "error": summary.get("error", "")[:240],
                    "pruned_probe_files": pruned_files,
                    "trimmed_summary_rows": trimmed_summaries,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.once:
            return 0 if summary.get("ok") else 1
        time.sleep(max(10, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
