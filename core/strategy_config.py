"""Strategy configuration loader.

Config files are advisory by default.  They document live parameters and can be
used by review/backtest tooling without importing scanner modules.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


@dataclass(slots=True)
class StrategyConfig:
    key: str
    name: str
    account: str
    scanner: str
    data_dir: str
    logs_dir: str
    params: dict[str, Any]

    def get(self, dotted_key: str, default: Any = None) -> Any:
        cur: Any = self.params
        for part in dotted_key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def load_strategy_config(name: str, config_dir: Path | None = None) -> StrategyConfig:
    config_dir = config_dir or CONFIG_DIR
    path = config_dir / f"{name}.toml"
    if not path.exists():
        raise FileNotFoundError(f"strategy config not found: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    strategy = data.get("strategy", {})
    return StrategyConfig(
        key=str(strategy.get("key") or name),
        name=str(strategy.get("name") or name),
        account=str(strategy.get("account") or ""),
        scanner=str(strategy.get("scanner") or ""),
        data_dir=str(strategy.get("data_dir") or ""),
        logs_dir=str(strategy.get("logs_dir") or ""),
        params={k: v for k, v in data.items() if k != "strategy"},
    )


def load_all_strategy_configs(config_dir: Path | None = None) -> dict[str, StrategyConfig]:
    config_dir = config_dir or CONFIG_DIR
    configs: dict[str, StrategyConfig] = {}
    for path in sorted(config_dir.glob("v*.toml")):
        cfg = load_strategy_config(path.stem, config_dir)
        configs[cfg.key] = cfg
    return configs


def dump_config_manifest(config_dir: Path | None = None) -> str:
    configs = load_all_strategy_configs(config_dir)
    payload = {
        key: {
            "name": cfg.name,
            "account": cfg.account,
            "scanner": cfg.scanner,
            "data_dir": cfg.data_dir,
            "logs_dir": cfg.logs_dir,
            "params": cfg.params,
        }
        for key, cfg in configs.items()
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

