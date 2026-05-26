"""Fail a staged material change when its durable Git record is missing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


PROHIBITED_PARTS = {"__pycache__"}
PROHIBITED_PREFIXES = (
    "runtime/",
    "logs/",
    "reports/",
    "server_logs_tencent/",
    "复盘报告/",
    "回测数据/",
    "polymarket_lab/reports/",
)
PROHIBITED_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".env", ".pem", ".key")
MATERIAL_PREFIXES = (
    "策略文件/",
    "交易客户端/",
    "core/",
    "config/",
    "部署工具/",
    "polymarket_lab/",
    "cloud/",
)
MATERIAL_ROOT_FILES = {"requirements.txt", ".gitignore", ".gitattributes"}


def staged_files() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRD", "-z"],
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in proc.stdout.split(b"\0") if item]


def is_prohibited(path: str) -> bool:
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    return (
        any(normalized.startswith(prefix) for prefix in PROHIBITED_PREFIXES)
        or any(part in PROHIBITED_PARTS for part in pure.parts)
        or normalized.lower().endswith(PROHIBITED_SUFFIXES)
    )


def is_material(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized in MATERIAL_ROOT_FILES or any(
        normalized.startswith(prefix) for prefix in MATERIAL_PREFIXES
    )


def main() -> int:
    files = staged_files()
    if not files:
        print("No staged changes.")
        return 0

    prohibited = [path for path in files if is_prohibited(path)]
    if prohibited:
        print("Blocked: generated/runtime/secret-like files are staged:", file=sys.stderr)
        for path in prohibited:
            print(f"  {path}", file=sys.stderr)
        return 1

    material = [path for path in files if is_material(path)]
    if material and "CHANGELOG.md" not in files:
        print(
            "Blocked: material changes require a staged CHANGELOG.md entry "
            "with reason, completed work, remaining work, verification, and live impact.",
            file=sys.stderr,
        )
        for path in material:
            print(f"  {path}", file=sys.stderr)
        return 1

    ledger_state = "present" if "CHANGELOG.md" in files else "not required"
    print(
        f"Git change guard passed: {len(files)} staged file(s), "
        f"{len(material)} material file(s), durable ledger {ledger_state}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
