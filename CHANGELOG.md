# AutoTrading Change Ledger

This is the durable reason-and-outcome ledger for every material design, code, configuration, deployment, rollback, optimization, or live operational change.

## Recording Rule

- Update this file in the same commit as any material repository change.
- Record a live deploy, rollback, service/config change, or risk decision even when no source code changed.
- Keep generated runtime/log/report data out of Git; record only the conclusion, validation result, release id, and remaining work here.
- Update `PROJECT_STATE.md` when the current architecture, operating procedure, or open priorities change.
- Update `记忆文档/MEMORY.md` when a durable decision, important incident, or milestone should survive daily report rollover.
- Read-only inspection that yields no new decision, incident, or action does not require an empty Git commit.

## Entry Template

```markdown
## YYYY-MM-DD HH:MM CST - Short title
- Trigger / reason:
- Completed:
- Not completed / remaining:
- Verification:
- Live impact / deployment:
- Files / release / commit:
```

## 2026-05-27 15:12 CST - Harden Binance open-order preflight and failure attribution
- Trigger / reason: User requested a comprehensive fix for recent open-order failures. Live SQLite inspection found repeated Binance failures from exchange order rules rather than strategy alpha alone: `-4164 notional_too_small`, `-4005 quantity_over_max`, `-4411 TradFi-Perps agreement required`, `-4131 percent_price_filter`, `-2027 max_position_or_leverage`, and `-1007 status unknown`.
- Completed: Added shared Binance USDM order-rule helpers for exchangeInfo parsing, MARKET_LOT_SIZE/minNotional/maxQty rounding, TradFi-Perps blocking, client order IDs, and percent-price preflight. A/B/C clients now share quantity validation and formatted market quantities. The execution engine performs quantity and market-price preflight before sending orders, preserves nested Binance error codes, and treats `-1007` status-unknown responses as success only after polling account positions. A/B/C scanners now record deterministic execution preflight rejections as `OPEN_SKIPPED` with `decision_stage=execution_preflight`, leaving `OPEN_FAILED` for real exchange/API failures. A/v11 also preserves raw Binance error responses instead of collapsing them to `-1`.
- Not completed / remaining: Exchange outages, backend timeouts with no confirmed position, and true account-level leverage/margin caps can still occur and should remain visible as `OPEN_FAILED`. Post-deploy monitoring must compare new `OPEN_FAILED` events after the release time; if fresh failures remain, inspect by exact Binance code before loosening strategy filters.
- Verification: Local `py_compile` passed for `core/binance_order_rules.py`, `core/execution_engine.py`, all three Binance clients, A/B/C scanners, and `release_manager.py`. `git diff --check` passed with only existing CRLF normalization warnings on two touched files. Live context before deployment showed all seven core services active, seven positions, and account unrealized PnL about `+430.77 USDT`.
- Live impact / deployment: Source change prepared for A/v11, B/v16, and C/v14 strategy deployments. Live release ids and post-deploy verification will be appended after deployment.
- Files / release / commit: `core/binance_order_rules.py`, `core/execution_engine.py`, `交易客户端/binance_client.py`, `交易客户端/binance_client_v2.py`, `交易客户端/binance_client_v3.py`, `策略文件/scanner.py`, `策略文件/scanner_v14.py`, `策略文件/scanner_v16.py`, `部署工具/release_manager.py`; Git commit to contain this entry.

## 2026-05-27 10:02 CST - Decommission Polymarket and remove command-center integration
- Trigger / reason: User requested deletion of all Polymarket-related files/code and stopping related services, then asked to analyze server memory and whether Account C/v14 filters remain too strict.
- Completed: Removed the tracked `polymarket_lab` code and the Tencent/Aliyun Polymarket deploy scripts. Removed Polymarket from the command-center portal, durable attention detector, live-context pull, and system-alert service list. Stopped, disabled, and removed Tencent `polymarket-monitor.service`; deleted Tencent `/opt/polymarket-lab`. Updated handoff docs so new agents treat Polymarket as decommissioned, not a current live module.
- Not completed / remaining: Historical memory still records the prior Polymarket experiments for audit context. C/v14 was analyzed but not loosened in this change; any threshold/confirmation change should be handled as a separate live strategy risk change after additional evidence.
- Verification: Tencent `polymarket-monitor.service` reports inactive after stop/disable/remove and `/opt/polymarket-lab` is absent. Server memory after shutdown: MemAvailable about 1.4GiB, swap used about 210MiB, no OOM lines in the checked 12-hour kernel journal; largest persistent Python processes are A/v11, B/v16, C/v14 at roughly 40-49MB RSS each. Local `py_compile` and `git diff --check` passed. Latest live sync pulls 20 files and only 7 active core services; `reports/index.html` no longer contains Polymarket text.
- Live impact / deployment: Polymarket is no longer monitored or shown in the command center. Tencent portal release `20260527-100318-portal-ad9d497` deployed and restarted `crypto-portal-refresh.service` and `crypto-system-alerts.service`; trading scanners, sentinel, account snapshot, and strategy evolution were not intentionally changed by this decommission.
- Files / release / commit: `部署工具/portal_dashboard.py`, `部署工具/decision_attention.py`, `部署工具/pull_live_context.py`, `部署工具/system_alerts.py`, `部署工具/git_change_guard.py`, `.gitignore`, `README.md`, `PROJECT_STATE.md`, `记忆文档/MEMORY.md`, deleted `polymarket_lab/*`, deleted `部署工具/deploy_polymarket_*.py`; Git commit to contain this entry.

## 2026-05-27 01:20 CST - A/v11 guarded replacement small-live and DB-backed attention acknowledgements
- Trigger / reason: User approved continuing `A/v11 EXP-20260523-v11-replacement-quality` only as an expansion/small-live path, not a full rollout, and requested all other P0 items be archived. User also challenged why P-level attention was being edited through JSON instead of a database.
- Completed: Narrowed A/v11 full-position replacement to the validated guarded scope: `STRONG_SIGNAL_THRESHOLD=112`, `EVICT_SCORE_GAP=25`, profitable positions at `>=2%` are hard-protected, policy version `small_live_v11_replacement_quality_v1`, no total-position increase, no same-symbol stacking change, and no entry-threshold loosening. Added a manual approval record for guarded small-live observation. Attention acknowledgements now write to SQLite tables in `runtime/event_store.sqlite3` (`attention_items`, `attention_acknowledgements`); JSON/Markdown/HTML are now export/cache surfaces for the portal, not the canonical acknowledgement store. Added `acknowledge_attention_items.py` and deployed it to Tencent.
- Not completed / remaining: A/v11 guarded replacement must now be monitored through real `EVICT_CLOSE`, `OPEN_RETRY_AFTER_EVICT`, A/v11 PnL, hard-stop risk, and missed-profit metrics before any further expansion. Attention UI still lacks a browser button for acknowledgements; acknowledgement is script-driven for now.
- Verification: Local `py_compile` passed for scanner and changed tools. Tencent `strategy-a` release `20260527-011050-strategy-a-68bca23` deployed and restarted `crypto-scanner.service` active/running. Tencent portal/research releases `20260527-011150-portal-68bca23` and `20260527-011150-research-68bca23` deployed. Server strategy evolution gate reran successfully: P0=0, top item is A/v11 `small_live_monitoring`, latest A/v11 replacement-quality delta `+844.0191`. Non-A/v11 P0 alerts (`入口页刷新失败`, `总入口页面偏旧`, `近期发生 OOM`) were archived in SQLite acknowledgements. Latest live sync shows 24 files pulled, all core services active, attention counts P0=0/P1=3/P2=4.
- Live impact / deployment: Live A/v11 replacement behavior is changed to a narrower guarded small-live policy and `crypto-scanner.service` was restarted. Portal/system-alert services restarted. B/v16 and C/v14 scanners were not restarted by this change.
- Files / release / commit: `策略文件/scanner.py`, `config/v11.toml`, `部署工具/decision_attention.py`, `部署工具/acknowledge_attention_items.py`, `部署工具/apply_research_approval.py`, `部署工具/experiment_runner.py`, `部署工具/release_manager.py`, `research_memory/approvals/*`, `CHANGELOG.md`, `PROJECT_STATE.md`, `记忆文档/MEMORY.md`; Git commit to contain this entry.

## 2026-05-27 00:45 CST - Global health audit and anti-stall live sync hardening
- Trigger / reason: The user requested a full project audit covering bugs, strategy direction, daily review progress, functional coordination, automatic fixes, Git sync, and durable handoff memory. The immediate operational pain was repeated long stalls while pulling live context and checking the server.
- Completed: Identified the 2026-05-26 21:56 CST Tencent SSH stall as an instance OOM event, not a simple network problem. Added a persistent 2G `/swapfile` on the Tencent server with `vm.swappiness=10`. `system_alerts.py` now reports memory, swap, and recent kernel OOM events as command-center alerts. `pull_live_context.py` now prefers OpenSSH with keepalive, compressed grouped file pulls, hard deadlines, and JSON error output; it also syncs daily market review latest Markdown/HTML and market snapshots. `daily_market_review.py` now shows C/v14 `入场候选/原始信号` instead of treating raw analysis noise as executable signals, and writes `market_review_latest.md` for fresh-clone handoff. Historical secrets in `记忆文档/MEMORY.md` were redacted from the current tree.
- Not completed / remaining: The OOM alert remains active until the six-hour kernel-journal window clears or the user explicitly accepts it in the attention ledger. Historical Git commits still contain old credential text; rotate any exposed Binance/testnet/API and SSH password credentials if they are still valid, and consider history rewriting before broader repository sharing. Strategy upgrade is not auto-applied: A/v11 replacement-quality is P0 `verified_upgrade_ready` but still requires expansion review.
- Verification: Local `py_compile` passed for changed scripts. Tencent services `crypto-scanner`, `crypto-scanner-v16`, `crypto-scanner-v14`, sentinel, account snapshot, portal refresh, system alerts, and Polymarket monitor are all `active`; four timers are `enabled`, and failed units are `0`. 2026-05-26 23:56 strategy evolution gate and 2026-05-27 00:00 data maintenance both finished with `Result=success`. Regenerated 2026-05-25 daily review on Tencent: C/v14 shows `57` entry candidates versus `46378` raw signals. Live context pull completed in about 20 seconds with `files_ok=24`.
- Live impact / deployment: Deployed Tencent `portal` release `20260526-234101-portal-84d0ad8`; deployed Tencent `research` releases `20260526-234139-research-84d0ad8` and `20260527-003530-research-84d0ad8`. No strategy scanner was restarted by the research update; portal/system alerts were restarted by the portal release. Server swap change is persistent through `/etc/fstab`.
- Files / release / commit: `部署工具/system_alerts.py`, `部署工具/pull_live_context.py`, `部署工具/daily_market_review.py`, `记忆文档/MEMORY.md`, `CHANGELOG.md`, `PROJECT_STATE.md`; Git commit to contain this entry.

## 2026-05-26 18:00 CST - Enforce complete Git synchronization for future changes
- Trigger / reason: The decision maker requires every future operation, design update, optimization, and change to be synchronized to Git with clear reasons, completed work, and remaining work so a different computer or model can continue without missing context.
- Completed: Added this durable change ledger; extended model and handoff rules; added a staged-change guard that rejects material code/config/tool commits without a staged `CHANGELOG.md` update.
- Not completed / remaining: GitHub CI does not yet enforce the guard remotely; until CI is added, agents must run the local check before every commit and push.
- Verification: `python -m py_compile 部署工具\git_change_guard.py` passed; when the tool file was staged without this ledger it correctly blocked the commit, and after adding this ledger `python 部署工具\git_change_guard.py` passed.
- Live impact / deployment: Documentation and local Git workflow only; no live strategy file, server configuration, or service is changed.
- Files / release / commit: `CHANGELOG.md`, `AGENTS.md`, `README.md`, `PROJECT_STATE.md`, `记忆文档/MEMORY.md`, `部署工具/git_change_guard.py`; no live release; Git commit contains this record.

## 2026-05-26 21:05 CST - Correct C/v14 signal-to-entry observability
- Trigger / reason: C/v14 showed extremely high signal counts but very few opens, making the command center imply a broken conversion funnel. Code inspection showed the metric was counting raw 15m confirmation signals and low-score raw analysis candidates as `SIGNAL`, while the live open gate only allows 1h entry candidates above the true threshold.
- Completed: C/v14 now writes `SIGNAL` only for real 1h entry candidates that pass the live score gate; raw candidate count remains in system heartbeat as `raw_signals_found`. Portal strategy summary now labels the column as entry candidates and, for C/v14, recalculates historical candidate counts from existing SQLite rows using the actual 1h long/short thresholds while showing raw candidate count separately. `config/v14.toml` was corrected to match the currently effective hardcoded thresholds. `release_manager.py` now accepts explicit `--dry-run` to match handoff documentation.
- Not completed / remaining: This does not loosen C/v14 live entry rules or prove a better C/v14 alpha. Next step is to collect post-fix candidate/open ratios and then decide whether C/v14 scoring itself should be redesigned or retired.
- Verification: Local `py_compile` passed for `策略文件/scanner_v14.py`, `部署工具/portal_dashboard.py`, and `部署工具/release_manager.py`. Local portal generation completed. `release_manager.py` dry-run passed for Tencent `strategy-c` and `portal`.
- Live impact / deployment: Intended live impact is observability/reporting only for C/v14 and portal; no trade threshold is loosened. Live deployment was attempted for release `20260526-210539-strategy-c-4c37e4d` but did not upload files because both Paramiko and native SSH timed out during SSH protocol banner exchange.
- Files / release / commit: `策略文件/scanner_v14.py`, `部署工具/portal_dashboard.py`, `config/v14.toml`, `部署工具/release_manager.py`; code commit `4c37e4d` pushed; live deployment remains pending until SSH access is healthy.

## 2026-05-26 21:14 CST - Retry Tencent deployment blocked before SSH login
- Trigger / reason: User requested updating the C/v14 observability fix to the server.
- Completed: Retried Tencent `strategy-c` release `20260526-211114-strategy-c-051f7d5` with a 30 second SSH/banner timeout and separately probed local TCP connectivity to port 22.
- Not completed / remaining: No server files were uploaded and no live services were restarted. Need restore healthy SSH banner/login first, then deploy `strategy-c` and `portal`.
- Verification: Paramiko deployment failed before authentication with `Error reading SSH protocol banner`. Native SSH also timed out during banner exchange. A short TCP probe returned `tcp_22_connected`, so port 22 is reachable but sshd did not return the SSH banner in time.
- Live impact / deployment: No live impact; deployment did not reach upload stage.
- Files / release / commit: This ledger entry records the failed operational attempt; code remains at `051f7d5`.

## 2026-05-26 21:45 CST - Tencent Lighthouse TAT channel also stalled
- Trigger / reason: SSH remains blocked before banner while C/v14 and portal deployment are pending; user attempted Tencent automated command execution as an alternate access path.
- Completed: Identified the target as Lighthouse instance `ap-singapore / lhins-8kx38vad`; evaluated the provided TAT invocation `invt-b43kp606bb`.
- Not completed / remaining: The TAT command has not executed because its status remains `DELIVERING` with empty output. Server access and pending deployment now require VNC login or restoration of an instance-side management channel.
- Verification: External TCP port 22 remains connectable while SSH banner times out; TAT returned `DELIVERING` for `ps aux | grep tat_agent | grep -v grep`, so it cannot currently confirm Agent state.
- Live impact / deployment: No server mutation verified; do not reboot the instance while trading services may still be running.
- Files / release / commit: Operational incident record only; pending deploy remains the C/v14/portal change from `4c37e4d`.

## 2026-05-26 22:08 CST - C/v14 and portal deployed after SSH recovery
- Trigger / reason: User restarted SSH on Tencent; pending C/v14 signal observability and portal summary fixes needed to be applied to live.
- Completed: Verified SSH login restored. Deployed Tencent `strategy-c` release `20260526-220420-strategy-c-631db66`, which uploaded C/v14 scanner/config/core dependencies, syntax-checked without writing `__pycache__`, and restarted `crypto-scanner-v14.service`. Uploaded portal files through release `20260526-220538-portal-631db66`; post command initially failed because it did not `cd` into `/opt/crypto-auto-trader`, then portal generation and `crypto-portal-refresh.service` / `crypto-system-alerts.service` restart were completed manually. Fixed `release_manager.py` so future post commands run from the target root and syntax checks avoid writing remote pycache.
- Not completed / remaining: Monitor the new C/v14 funnel over the next several scan cycles: raw candidates, entry candidates, opens, skips, and PnL. This deployment changes reporting/candidate logging, not C/v14 alpha.
- Verification: Remote portal now contains `入场候选` and shows C/v14 `148` entry candidates versus `84524` raw candidates. `crypto-scanner-v14.service`, `crypto-portal-refresh.service`, and `crypto-system-alerts.service` are all `active/running`, `Result=success`, `NRestarts=0`; v14 journal shows normal stop/start and no startup traceback in the checked window.
- Live impact / deployment: Live C/v14 service and command-center portal are updated. No entry threshold was loosened; the change narrows signal logging to real entry candidates and makes the portal disclose raw versus entry candidates.
- Files / release / commit: `部署工具/release_manager.py` fixed locally; live releases `20260526-220420-strategy-c-631db66` and `20260526-220538-portal-631db66`.
