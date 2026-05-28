# AutoTrading Change Ledger

This is the durable reason-and-outcome ledger for every material design, code, configuration, deployment, rollback, optimization, or live operational change.

## 2026-05-28 14:30 CST - Sentinel scans separated into dedicated table
- Trigger / reason: event_store.sqlite3 grew to 1.7GB due to SENTINEL_SCANNED events (~97% of rows). User requested sentinel data in separate table with daily partitioning.
- Completed: (1) Added `sentinel_scans` table to `core/event_store.py` DDL with columns: ts, date (YYYY-MM-DD), strategy, symbol, event_type, reason, category, decision_stage, filter_layer, change_pct, velocity_pct, abs_velocity_pct, quote_volume, scan_result, payload_json. Indexes on date, (strategy,date), (symbol,date). (2) Added `EventStoreWriter.write_sentinel_scan()` method. (3) Modified `scanner.py` and `scanner_v14.py` — sentinel scans now write to `sentinel_scans` table via `_log_sentinel_scan_event()`, no longer pollute `events` table. (4) Modified `sentinel_quality_review.py` — reads from `sentinel_scans` first, falls back to `events` for backward compatibility. (5) Updated `cleanup_event_store.py` — also cleans `sentinel_scans` table by date column.
- Not completed / remaining: Deploy to Tencent (SSH still recovering). Need to migrate existing SENTINEL_SCANNED data from events to sentinel_scans (optional, old data will age out). A/v11 -1109 is Testnet account issue.
- Verification: All 5 files compile successfully. No strategy core algorithm changed.
- Live impact / deployment: New table created automatically on first write. Existing events table untouched. No strategy logic changed.
- Files / release / commit: `core/event_store.py`, `策略文件/scanner.py`, `策略文件/scanner_v14.py`, `部署工具/sentinel_quality_review.py`, `部署工具/cleanup_event_store.py`, `CHANGELOG.md`.

## 2026-05-28 14:00 CST - SQLite cleanup script, pipeline fix, portal fix
- Trigger / reason: event_store.sqlite3 on Tencent grew to 1.7GB, causing analysis pipeline timeout and SSH unavailability. Portal dashboard had Python syntax error (`{{}}` in f-string).
- Completed: (1) Added `部署工具/cleanup_event_store.py` — safely deletes SENTINEL_SCANNED/EVENT/SIGNAL older than N days, protects all trading events (OPEN/CLOSE/FORCED_CLOSE/OPEN_FAILED/OPEN_SKIPPED), creates backup before deletion, runs VACUUM. (2) Fixed `portal_dashboard.py` `{{}}` syntax error — replaced with proper Python dict construction. (3) Updated `aliyun_analysis_refresh.sh` — removed `set -euo pipefail`, added `timeout 600` for sync step, each step uses `|| echo WARN` instead of failing the pipeline. (4) Analysis scripts uploaded to Tencent for local execution (data-local-compute pattern).
- Not completed / remaining: Tencent server SSH unreachable (likely OOM from portal_dashboard.py on 1.7GB SQLite). Need to wait for recovery, then run cleanup_event_store.py to shrink DB. A/v11 -1109 is Testnet account issue (keys loaded correctly in process). Analysis pipeline architecture needs to shift: run on Tencent locally, sync only reports to Aliyun.
- Verification: cleanup_event_store.py dry-run passed locally. portal_dashboard.py compiles and generates locally. Aliyun portal updated to 11:15 CST.
- Live impact / deployment: No strategy code changed. Infrastructure fixes only.
- Files / release / commit: `部署工具/cleanup_event_store.py`, `部署工具/portal_dashboard.py`, `部署工具/aliyun_analysis_refresh.sh`, `CHANGELOG.md`.

## 2026-05-28 10:15 CST - Fix A/v11 OPEN_FAILED -1109 and B/v16 CLOSE pnl audit
- Trigger / reason: Daily review found 694 A/v11 OPEN_FAILED with -1109 Invalid account, and B/v16 CLOSE events with pnl=None.
- Completed: (1) Root cause for -1109: all three scanner services missing `EnvironmentFile=/etc/crypto-auto-trader/trading.env`. Added EnvironmentFile to crypto-scanner, crypto-scanner-v14, crypto-scanner-v16 services. Reloaded daemon and restarted all three. (2) Root cause for pnl=None: EventStoreWriter stores pnl in payload_json.raw sub-field, not at top level. Actual data exists (e.g. GRASSUSDT pnl=-1.5034, NEARUSDT pnl=+15.9727 in raw). This is a display/query issue, not data loss.
- Not completed / remaining: EventStoreWriter could be enhanced to extract pnl_usd/pnl_pct from raw to top-level columns for easier querying. B/v16 CLOSE events should populate top-level pnl fields.
- Verification: All three scanner services active/running after restart. EnvironmentFile confirmed in service config.
- Live impact / deployment: A/v11 should now successfully open positions with valid API key. No strategy logic changed.
- Files / release / commit: `CHANGELOG.md`; systemd service configs modified on Tencent server.

## 2026-05-27 21:45 CST - Attention API server and portal confirmation buttons
- Trigger / reason: User requested server-side API for attention item confirmation instead of script-only workflow.
- Completed: (1) Added `部署工具/attention_api_server.py` — lightweight HTTP API using Python built-in http.server, endpoints: GET /api/attention, POST /api/attention/ack, POST /api/attention/resolve, GET /api/health. Supports both SQLite and JSON fallback. (2) Deployed as `crypto-attention-api.service` on Aliyun port 8090. (3) Updated `portal_dashboard.py` — added "确认" button in attention table, JavaScript calls API via AJAX. (4) Synced attention JSON to Aliyun.
- Not completed / remaining: Aliyun security group may need port 8090 opened for external access. Portal needs to be regenerated on Aliyun to show buttons.
- Verification: `py_compile` passed. API service active/running. curl test returns items correctly.
- Live impact / deployment: New API service on Aliyun. Portal UI enhanced with confirmation buttons.
- Files / release / commit: `部署工具/attention_api_server.py`, `部署工具/deploy_attention_api.sh`, `部署工具/portal_dashboard.py`, `CHANGELOG.md`.

## 2026-05-27 20:40 CST - Phase 9 live transition checklist
- Trigger / reason: Execute Phase 9 of FUTURE_EXECUTION_PLAN.md.
- Completed: Added `部署工具/live_transition_checklist.py`. Evaluates readiness for Testnet→live transition by checking: PF≥1.2, win rate≥40%, ≥100 trades in 30d, hard-stop rate≤10%, gate status P0/P1, zero recovery positions. Includes transition rules (fee/slippage 0.15%, expected PF decay 0.7-0.9x on live) and rollback triggers. Outputs `runtime/live_transition_latest.json` and `reports/live_transition_latest.md`. Uploaded to Aliyun.
- Not completed / remaining: Current results: A/v11 NOT READY (2/6), B/v16 NOT READY (4/6), C/v14 NOT READY (2/6). No strategy meets all criteria yet. Live transition requires explicit user approval.
- Verification: `py_compile` passed. Test run: overall_ready=False, B/v16 closest at 4/6 checks passed.
- Live impact / deployment: None. Checklist only, no live transition.
- Files / release / commit: `部署工具/live_transition_checklist.py`, `CHANGELOG.md`.

## 2026-05-27 20:30 CST - Phase 8 promotion gate hardening
- Trigger / reason: Execute Phase 8 of FUTURE_EXECUTION_PLAN.md.
- Completed: Hardened `strategy_evolution_gate.py` `classify_decision()`: (1) Added `adjust_pnl_for_fees()` — deducts 0.15% round-trip cost from shadow PnL; (2) Added `check_rollback_triggers()` — checks sample sufficiency, PF decay, hard-stop increase, account risk; (3) P0 now requires ≥50 samples + fee-adjusted positive PnL; (4) P1 requires ≥30 samples; (5) Rollback triggers added as blockers. Constants: FEE_SLIPPAGE_ADJUSTMENT_PCT=0.15%, MIN_SAMPLE_FOR_P0=50, MIN_SAMPLE_FOR_P1=30, ROLLBACK_PF_DECAY_RATIO=0.8, ROLLBACK_HARD_STOP_RATIO=1.5.
- Not completed / remaining: Regime segmentation (trend/chop/high-vol/low-liquidity) not implemented. Rollback trigger automation not deployed.
- Verification: `py_compile` passed. Aliyun upload OK.
- Live impact / deployment: Gate logic hardened, no live strategy change.
- Files / release / commit: `部署工具/strategy_evolution_gate.py`, `CHANGELOG.md`.

## 2026-05-27 20:20 CST - Phase 7 recovery position management
- Trigger / reason: Execute Phase 7 of FUTURE_EXECUTION_PLAN.md.
- Completed: Enhanced `strategy_truth_ledger.py` with: (1) detailed recovery position audit per strategy (symbol, side, entry/mark price, PnL, margin, leverage); (2) `evaluate_recovery_exit_policies()` shadow-tests 5 candidate exit policies (4h/8h/24h time exit, 2% trailing, opposite signal); (3) recovery exit policies included in JSON output and MD report. Uploaded to Aliyun.
- Not completed / remaining: No automatic recovery exit deployed. Shadow results need validation before any live policy.
- Verification: `py_compile` passed. Test run shows 5 recovery positions, exit policies evaluated.
- Live impact / deployment: None. Analysis only.
- Files / release / commit: `部署工具/strategy_truth_ledger.py`, `CHANGELOG.md`.

## 2026-05-27 19:30 CST - Phase 3/4/5 shadow experiment configs
- Trigger / reason: Execute Phase 3/4/5 of FUTURE_EXECUTION_PLAN.md.
- Completed: Added 10 new experiment specs to `core/experiment.py`: A/v11 entry threshold 115/120, trailing pullback 0.8/1.0; B/v16 ATR stop bands, overheat cap 85; C/v14 strict candidate long65/70, filter ablation (sector, BTC trend). Added `run_threshold_experiment()` and `run_filter_ablation()` to `experiment_runner.py`. Total: 13 experiments. Uploaded to Aliyun.
- Not completed / remaining: Results need 3/7/14/30 day windows. No strategy code changed.
- Verification: `py_compile` passed. 13 specs loaded. Aliyun upload OK.
- Live impact / deployment: Shadow experiments only.
- Files / release / commit: `core/experiment.py`, `部署工具/experiment_runner.py`, `CHANGELOG.md`.

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

## 2026-05-27 19:05 CST - Phase 6 sentinel quality review
- Trigger / reason: Execute Phase 6 of FUTURE_EXECUTION_PLAN.md — measure sentinel signal contribution.
- Completed: Added `部署工具/sentinel_quality_review.py`. Reads SQLite events with sentinel fields, classifies strategy response (opened/skipped/filtered/no_signal/error), computes per-reason and per-strategy stats, lists top 20 movers. Outputs `runtime/sentinel_quality_latest.json` and `reports/sentinel_quality_latest.md`. Uploaded to Aliyun and integrated into 2-hour analysis pipeline.
- Not completed / remaining: Forward-return calculation (15/30/60/120m) not yet implemented (needs K-line data). Coverage audit needs market snapshot comparison. Sentinel score bonus shadow experiment not started.
- Verification: Local `py_compile` passed. Local test: 17,982 sentinel decisions, 16,111 scanned, 14 opened, open rate 0.1%.
- Live impact / deployment: No live strategy change. Analysis on Aliyun only.
- Files / release / commit: `部署工具/sentinel_quality_review.py`, `CHANGELOG.md`; Git commit to contain this entry.

## 2026-05-27 18:50 CST - Phase 2 command-center strategy quality board
- Trigger / reason: Execute Phase 2 of FUTURE_EXECUTION_PLAN.md — integrate truth ledger into portal dashboard so decision-makers see "who is really making money" on the first screen.
- Completed: Extended `部署工具/portal_dashboard.py` with: (1) `strategy_truth_summary()` function reads `runtime/strategy_truth_latest.json`; (2) `build_data()` includes `strategy_truth` data; (3) `build_findings()` generates P1 alerts for negative-expectancy strategies (PF<1) and P2 notes for positive-expectancy strategies (PF>=2) and recovery positions; (4) HTML template includes new "策略质量看板" section showing per-strategy closed trades, win rate, net PnL, PF, payoff ratio, hard-stop count, recovery count, and recovery unrealized PnL. Uploaded to Aliyun.
- Not completed / remaining: Phase 6 (sentinel quality review) not started. Portal generation on Aliyun needs verification after next timer run.
- Verification: Local `py_compile` passed. Local portal generation succeeded. Generated HTML contains "策略质量看板" section with data from truth ledger (19 minutes old).
- Live impact / deployment: No live strategy change. Portal dashboard updated on Aliyun; will be synced to Tencent via reverse sync.
- Files / release / commit: `部署工具/portal_dashboard.py`, `CHANGELOG.md`; Git commit to contain this entry.

## 2026-05-27 18:30 CST - Phase 1 strategy truth ledger implementation
- Trigger / reason: Execute Phase 1 of FUTURE_EXECUTION_PLAN.md — create authoritative truth table separating active strategy PnL from recovery positions.
- Completed: Added `部署工具/strategy_truth_ledger.py`. Reads SQLite `event_store.sqlite3` OPEN/CLOSE/FORCED_CLOSE events and `account_snapshots`. Matches open-close pairs, identifies recovery positions (in snapshots but no matching OPEN event), computes per-strategy stats (win rate, PF, payoff ratio, hard-stop count, recovery count). Outputs `runtime/strategy_truth_latest.json` and `reports/strategy_truth_latest.md`. Uploaded to Aliyun and integrated into both analysis pipelines (daily full + 2-hour refresh).
- Not completed / remaining: Phase 2 (portal integration) not started. Truth ledger reads from synced SQLite on Aliyun; needs verification after first Aliyun timer run with real Tencent data. Fee/slippage estimates are approximate (100USDT × leverage × 0.05% × 2).
- Verification: Local `py_compile` passed. Local test run produced correct output: A/v11 closed=16 win_rate=12.5% net_pnl=-25.98 PF=0.01 recovery=3; B/v16 closed=24 win_rate=50% net_pnl=+127.72 PF=5.9 recovery=2; C/v14 closed=0 (limited local data). Aliyun script uploaded and integrated into both `run_shadow_review.sh` and `aliyun_analysis_refresh.sh`.
- Live impact / deployment: No live strategy change. New analysis script runs on Aliyun only.
- Files / release / commit: `部署工具/strategy_truth_ledger.py`, `CHANGELOG.md`; Git commit to contain this entry.

## 2026-05-27 17:30 CST - Phase 0.5 dual-server architecture migration execution
- Trigger / reason: User approved the dual-server plan and requested immediate execution. Aliyun (39.105.156.210) confirmed: 2 CPU, 1.7GiB RAM (1.4GiB available), 40GB disk (34GB free), Python 3.13.12 with numpy/pandas/paramiko/tomli installed. Only 1 timer running (shadow-review daily 02:20).
- Completed: (1) Slimmed Tencent `system_alerts.py` — removed portal-refresh, counterfactual, evolution-gate, and market-review service/timer checks (migrated to Aliyun). (2) Created `部署工具/sync_aliyun_reports_to_tencent.py` — reverse sync script uploads Aliyun-generated reports to Tencent. (3) Upgraded Aliyun `run_shadow_review.sh` — added Step 9 reverse sync after portal generation. (4) Stopped/disabled on Tencent: `crypto-portal-refresh.service`, `crypto-counterfactual-open-skips.timer`, `crypto-strategy-evolution-gate.timer`, `crypto-market-review.timer`. (5) Deployed Aliyun `crypto-analysis-refresh.timer` — runs every 2 hours: sync data → counterfactual eval → evolution gate → portal refresh → reverse sync to Tencent. (6) Cleaned MEMORY.md from 463 lines to 73 lines — distilled 2026-05-17~2026-05-24 detailed entries into a compact summary section.
- Not completed / remaining: Phase 0.5.1 Aliyun resource check complete. Phase 0.5.2 sync expansion (add event_store.sqlite3, account_snapshot_latest.json, research_memory/ to sync) not yet done. Phase 1 (truth ledger) not started. `deploy_shadow_aliyun.py` needs update to include new scripts.
- Verification: Tencent now shows 7 services (scanner×3 + sentinel + account-snapshot + market-data-cache + system-alerts) + 1 timer (data-maintenance). Aliyun shows 2 timers (shadow-review daily + analysis-refresh every 2h). All scripts `py_compile` passed locally. Aliyun timer deployed and enabled.
- Live impact / deployment: Tencent portal-refresh stopped — portal will be generated on Aliyun and synced back. Tencent counterfactual/evolution-gate/market-review timers stopped — these now run on Aliyun. No strategy code changed.
- Files / release / commit: `部署工具/system_alerts.py`, `部署工具/sync_aliyun_reports_to_tencent.py`, `部署工具/aliyun_analysis_refresh.sh`, `记忆文档/MEMORY.md`, `CHANGELOG.md`; Git commit to contain this entry.

## 2026-05-27 16:45 CST - Dual-server architecture optimization and expanded execution plan
- Trigger / reason: User requested optimizing the two-server (Tencent + Aliyun) architecture to reduce Tencent resource pressure, and expanding the execution plan with resource/API constraints, VPB evaluation, sentinel coverage audit, testnet-to-live transition, and rollback triggers.
- Completed: Rewrote `记忆文档/FUTURE_EXECUTION_PLAN.md` as a dual-server architecture plan. Key changes: (1) Tencent retains only 6 API-dependent services; Aliyun takes all analysis/report/experiment/gate tasks as Timer jobs; (2) Added reverse sync from Aliyun to Tencent for generated reports; (3) Added resource constraints: memory <50MB per new process, storage red line at 15GB free, Binance API weight budget <600/min (50% safety line); (4) Added Phase 0.5 for architecture migration, Phase 9 for testnet-to-live transition; (5) Added VPB contribution experiment in Phase 3; (6) Added sentinel coverage audit in Phase 6; (7) Added concrete rollback triggers in Phase 8. Updated `PROJECT_STATE.md` to reference the new plan.
- Not completed / remaining: No code changes made. Phase 0.5 architecture migration, Phase 1 truth ledger, and all subsequent phases remain unimplemented. Aliyun resource check (CPU/memory/disk) pending.
- Verification: Documentation files updated locally. No live service or strategy behavior changed.
- Live impact / deployment: None; planning/documentation only.
- Files / release / commit: `记忆文档/FUTURE_EXECUTION_PLAN.md`, `PROJECT_STATE.md`, `CHANGELOG.md`; Git commit to contain this entry.

## 2026-05-27 15:35 CST - Persist future strategy-quality execution plan
- Trigger / reason: User asked whether the detailed plan can solve all current problems, requested more concrete technical milestones, and asked to write it into the future execution plan.
- Completed: Added `记忆文档/FUTURE_EXECUTION_PLAN.md` as a durable handoff plan. It clarifies which current system problems the plan can solve and which future market/exchange problems it cannot guarantee. It breaks execution into technical phases: safety baseline, unified strategy truth ledger, command-center quality board, A/v11 evidence program, B/v16 payoff improvement, C/v14 rebuild/retire program, sentinel contribution review, recovery-position management, and promotion-gate hardening. Updated `PROJECT_STATE.md` and `记忆文档/MEMORY.md` so future agents discover and follow the plan.
- Not completed / remaining: This change records the plan only. It does not implement the planned scripts, portal sections, SQLite tables, shadow experiments, or promotion-gate extensions yet.
- Verification: Documentation files were updated locally and are ready for Git guard/commit. No live service or strategy behavior was changed.
- Live impact / deployment: None; planning/documentation only.
- Files / release / commit: `记忆文档/FUTURE_EXECUTION_PLAN.md`, `PROJECT_STATE.md`, `记忆文档/MEMORY.md`, `CHANGELOG.md`; Git commit to contain this entry.

## 2026-05-27 15:12 CST - Harden Binance open-order preflight and failure attribution
- Trigger / reason: User requested a comprehensive fix for recent open-order failures. Live SQLite inspection found repeated Binance failures from exchange order rules rather than strategy alpha alone: `-4164 notional_too_small`, `-4005 quantity_over_max`, `-4411 TradFi-Perps agreement required`, `-4131 percent_price_filter`, `-2027 max_position_or_leverage`, and `-1007 status unknown`.
- Completed: Added shared Binance USDM order-rule helpers for exchangeInfo parsing, MARKET_LOT_SIZE/minNotional/maxQty rounding, TradFi-Perps blocking, client order IDs, and percent-price preflight. A/B/C clients now share quantity validation and formatted market quantities. The execution engine performs quantity and market-price preflight before sending orders, preserves nested Binance error codes, and treats `-1007` status-unknown responses as success only after polling account positions. A/B/C scanners now record deterministic execution preflight rejections as `OPEN_SKIPPED` with `decision_stage=execution_preflight`, leaving `OPEN_FAILED` for real exchange/API failures. A/v11 also preserves raw Binance error responses instead of collapsing them to `-1`.
- Not completed / remaining: Exchange outages, backend timeouts with no confirmed position, and true account-level leverage/margin caps can still occur and should remain visible as `OPEN_FAILED`. Continue monitoring fresh failures by exact Binance code before changing strategy thresholds.
- Verification: Local `py_compile` passed for `core/binance_order_rules.py`, `core/execution_engine.py`, all three Binance clients, A/B/C scanners, and `release_manager.py`. `git diff --check` passed with only existing CRLF normalization warnings on two touched files. Remote AST syntax check passed for the deployed eight core strategy/client files without writing `__pycache__`. Deployed file SHA256 hashes match local for the changed strategy/client/execution files. Systemd shows A/B/C scanner services `active/running`, `Result=success`, `NRestarts=0`; journal since the deploy shows only normal stop/start and no Traceback/ImportError/SyntaxError/下单失败 lines. SQLite check since `2026-05-27 15:15 CST` found `OPEN_FAILED=0`. Latest live sync after deployment shows all seven core services active, alerts `ok/0`, five positions, account unrealized PnL about `+443.20 USDT`, and A/v11 sizing violations `0`.
- Live impact / deployment: Deployed Tencent `strategy-a` release `20260527-151519-strategy-a-9ac8976`, `strategy-b` release `20260527-151519-strategy-b-9ac8976`, and `strategy-c` release `20260527-151520-strategy-c-9ac8976`; restarted `crypto-scanner.service`, `crypto-scanner-v16.service`, and `crypto-scanner-v14.service`.
- Files / release / commit: `core/binance_order_rules.py`, `core/execution_engine.py`, `交易客户端/binance_client.py`, `交易客户端/binance_client_v2.py`, `交易客户端/binance_client_v3.py`, `策略文件/scanner.py`, `策略文件/scanner_v14.py`, `策略文件/scanner_v16.py`, `部署工具/release_manager.py`; source commit `9ac8976`, this follow-up ledger commit records live deployment verification.

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
