"""Local research pipeline runner.

Keeps the two-year historical Kline backfill moving locally. When the local
download queue is complete, starts the full local indicator-factory backtest.

This tool is intentionally local-only. It does not call Binance, mutate live
strategy config, restart services, place orders, or enable auto apply.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / "runtime"
REPORTS_DIR = ROOT / "reports"
PIPELINE_JSON = RUNTIME_DIR / "local_research_pipeline_latest.json"
PIPELINE_MD = REPORTS_DIR / "local_research_pipeline_latest.md"
LOG_DIR = RUNTIME_DIR / "local_research_pipeline"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def download_complete(progress_payload: dict[str, Any]) -> bool:
    progress = progress_payload.get("progress") if isinstance(progress_payload.get("progress"), dict) else {}
    quality = progress_payload.get("quality") if isinstance(progress_payload.get("quality"), dict) else {}
    pending = int(progress.get("pending_tasks") or 0)
    failed = int(progress.get("failed_requests") or 0)
    return bool(quality.get("task_queue_complete")) or (pending == 0 and failed == 0 and progress_payload.get("status") == "complete")


def state_payload(phase: str, status: str, log_path: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "generated_at": now_iso(),
        "module": "local_research_pipeline",
        "phase": phase,
        "status": status,
        "safety": {
            "local_only": True,
            "cloud_compute": False,
            "binance_requests_enabled": False,
            "live_config_mutation": False,
            "paper_or_real_orders": False,
            "strategy_frequency_change": False,
            "automatic_tuning_allowed": False,
            "automatic_rollback_allowed": False,
            "automatic_upgrade_allowed": False,
        },
        "paths": {
            "log": str(log_path),
            "download_progress": str(RUNTIME_DIR / "historical_kline_backfill_2y_local.json"),
            "download_report": str(REPORTS_DIR / "historical_kline_backfill_2y_local.md"),
            "indicator_factory_json": str(RUNTIME_DIR / "indicator_factory_latest.json"),
            "indicator_factory_html": str(ROOT / "research_lab" / "indicator_factory" / "indicator_factory_latest.html"),
        },
        "full_backtest": {
            "tool": "indicator_factory.py",
            "days": 730,
            "intervals": ["15m", "30m", "1h", "4h"],
            "mode": "all_combos",
            "stage": "full-2y-v1",
        },
    }
    if extra:
        payload.update(extra)
    return payload


def render_md(payload: dict[str, Any]) -> str:
    progress = payload.get("download_progress") if isinstance(payload.get("download_progress"), dict) else {}
    quality = payload.get("download_quality") if isinstance(payload.get("download_quality"), dict) else {}
    backtest = payload.get("backtest_summary") if isinstance(payload.get("backtest_summary"), dict) else {}
    lines = [
        "# Local Research Pipeline",
        "",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Phase: `{payload.get('phase')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Safety: `local_only / no Binance / no cloud / no live mutation / no orders`",
        f"- Log: `{payload.get('paths', {}).get('log')}`",
        f"- Download progress: `{payload.get('paths', {}).get('download_progress')}`",
        f"- Full backtest target: `indicator_factory full-2y-v1 all combos`",
    ]
    if progress:
        lines.extend(
            [
                "",
                "## Download",
                f"- Percent: `{progress.get('percent')}`",
                f"- Pending tasks: `{progress.get('pending_tasks')}`",
                f"- Completed requests: `{progress.get('completed_requests')}`",
                f"- Failed requests: `{progress.get('failed_requests')}`",
                f"- Skipped existing: `{progress.get('skipped_existing')}`",
                f"- Written rows: `{progress.get('written_rows')}`",
                f"- Queue complete: `{quality.get('task_queue_complete')}`",
                f"- Quality: `{quality.get('status')}`",
            ]
        )
    if backtest:
        lines.extend(
            [
                "",
                "## Full Backtest",
                f"- Run id: `{backtest.get('run_id')}`",
                f"- Tested combos: `{backtest.get('tested_combos')}`",
                f"- Tested combo-intervals: `{backtest.get('tested_combo_intervals')}`",
                f"- Candidate count: `{backtest.get('candidate_count')}`",
                f"- Action: `{backtest.get('action')}`",
            ]
        )
    return "\n".join(lines) + "\n"


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_local_proxy() -> str:
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    try:
        proc = subprocess.run(
            ["netsh", "winhttp", "show", "proxy"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=3,
        )
        text = proc.stdout or ""
    except Exception:
        text = ""
    match = re.search(r"(127\.0\.0\.1|localhost):(\d+)", text)
    if match:
        host = "127.0.0.1" if match.group(1) == "localhost" else match.group(1)
        port = int(match.group(2))
        if port_open(host, port):
            return f"http://{host}:{port}"
    if port_open("127.0.0.1", 7890):
        return "http://127.0.0.1:7890"
    return ""


def command_env() -> tuple[dict[str, str], str]:
    env = os.environ.copy()
    proxy = detect_local_proxy()
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
        env.setdefault("NO_PROXY", "localhost,127.0.0.1")
        env.setdefault("no_proxy", "localhost,127.0.0.1")
    return env, proxy


def save_state(phase: str, status: str, log_path: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = state_payload(phase, status, log_path, extra)
    write_json_atomic(PIPELINE_JSON, payload)
    write_text_atomic(PIPELINE_MD, render_md(payload))
    return payload


def run_logged(cmd: list[str], log_path: Path, *, timeout_sec: int | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env, proxy = command_env()
    with log_path.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"\n==== {now_iso()} ====\n")
        fh.write(" ".join(cmd) + "\n")
        fh.write(f"proxy={proxy or '-'}\n")
        fh.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            return proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.kill()
            fh.write(f"\nTIMEOUT after {timeout_sec}s\n")
            return 124


def make_download_cmd(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-B",
        str(ROOT / "部署工具" / "historical_kline_backfill.py"),
        "--apply",
        "--runtime-dir",
        "runtime",
        "--reports-dir",
        "reports",
        "--research-store",
        "research_store",
        "--top-n",
        str(args.top_n),
        "--days",
        str(args.days),
        "--intervals",
        args.intervals,
        "--providers",
        args.providers,
        "--format",
        "jsonl",
        "--max-rps",
        str(args.max_rps),
        "--max-requests",
        str(args.batch_requests),
        "--max-runtime-sec",
        str(args.batch_runtime_sec),
        "--request-timeout",
        str(args.request_timeout),
        "--flush-requests",
        str(args.flush_requests),
        "--output-prefix",
        args.download_prefix,
    ]


def make_backtest_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        "-B",
        str(ROOT / "部署工具" / "indicator_factory.py"),
        "run",
        "--root",
        str(ROOT),
        "--days",
        str(args.days),
        "--intervals",
        args.intervals,
        "--stage",
        args.backtest_stage,
    ]
    if args.all_combos:
        cmd.append("--all-combos")
    else:
        cmd.extend(["--max-combos", str(args.max_combos)])
    return cmd


def latest_progress(prefix: str) -> dict[str, Any]:
    return read_json(RUNTIME_DIR / f"{prefix}.json")


def latest_backtest_summary() -> dict[str, Any]:
    payload = read_json(RUNTIME_DIR / "indicator_factory_latest.json")
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "run_id": payload.get("run_id") or summary.get("run_id"),
        "tested_combos": summary.get("tested_combos"),
        "tested_combo_intervals": summary.get("tested_combo_intervals"),
        "candidate_count": summary.get("candidate_count"),
        "action": summary.get("action"),
        "decision_counts": summary.get("decision_counts"),
    }


def run_pipeline(args: argparse.Namespace) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"pipeline_{datetime.now(CST).strftime('%Y%m%d_%H%M%S')}.log"
    _env, proxy = command_env()
    save_state("download", "starting", log_path, {"proxy": proxy or ""})
    loops = 0
    while True:
        loops += 1
        save_state("download", "running_batch", log_path, {"loop": loops, "proxy": proxy or ""})
        code = run_logged(make_download_cmd(args), log_path, timeout_sec=args.batch_runtime_sec + 120)
        progress_payload = latest_progress(args.download_prefix)
        progress = progress_payload.get("progress") if isinstance(progress_payload.get("progress"), dict) else {}
        quality = progress_payload.get("quality") if isinstance(progress_payload.get("quality"), dict) else {}
        save_state(
            "download",
            str(progress_payload.get("status") or f"exit_{code}"),
            log_path,
            {
                "loop": loops,
                "last_exit_code": code,
                "download_progress": progress,
                "download_quality": quality,
                "last_task": progress_payload.get("last_task"),
            },
        )
        if download_complete(progress_payload):
            break
        if args.max_loops and loops >= args.max_loops:
            save_state("download", "stopped_max_loops", log_path, {"loop": loops})
            return 2
        time.sleep(max(0, args.sleep_sec))

    save_state("full_backtest", "starting", log_path, {"download_progress": progress, "download_quality": quality})
    backtest_code = run_logged(make_backtest_cmd(args), log_path, timeout_sec=None)
    status = "completed" if backtest_code == 0 else "failed"
    save_state(
        "full_backtest",
        status,
        log_path,
        {
            "backtest_exit_code": backtest_code,
            "download_progress": progress,
            "download_quality": quality,
            "backtest_summary": latest_backtest_summary(),
        },
    )
    return backtest_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local 2y data backfill, then full indicator-factory backtest")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--intervals", default="15m,30m,1h,4h")
    parser.add_argument("--providers", default="bybit,okx")
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--max-rps", type=float, default=0.2)
    parser.add_argument("--batch-requests", type=int, default=240)
    parser.add_argument("--batch-runtime-sec", type=int, default=1200)
    parser.add_argument("--request-timeout", type=float, default=8.0)
    parser.add_argument("--flush-requests", type=int, default=10)
    parser.add_argument("--sleep-sec", type=int, default=30)
    parser.add_argument("--download-prefix", default="historical_kline_backfill_2y_local")
    parser.add_argument("--backtest-stage", default="full-2y-v1")
    parser.add_argument("--all-combos", action="store_true", default=True)
    parser.add_argument("--max-combos", type=int, default=120)
    parser.add_argument("--max-loops", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
