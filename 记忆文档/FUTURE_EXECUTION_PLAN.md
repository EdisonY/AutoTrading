# AutoTrading 双服务器架构优化 + 长期执行计划

## 2026-05-31 下一阶段目标：从调参系统升级为策略进化系统

## 2026-06-02 当前长期任务 P 级执行队列（canonical）

本节是后续自动推进的优先级来源。这里的 P 级是长期工程优先级，不是入口页 attention priority。旧阶段清单仍保留历史上下文；如冲突，以本节顺序为准。

### Long-term P0 - 必须先完成

1. **P0-A Binance API 根治**
   - 目标：把余额/仓位读取从轮询迁到 user-data-stream 或集中账户状态服务；把 guard 从协作式文件锁升级为独立队列/集中限速；避免 IP 级 418/429 反复拖住账户快照和策略扫描。
   - 已完成：signed/public guard、指数冷却、trade reserve、post-ban quiet/recovery ramp（默认 ban 后静默 5 分钟，再 30 分钟 signed+public 合并限 `4/min` + `15000ms` 间隔）、部分重复 `positionRisk` 合并、B/C `exchangeInfo` 迁出 signed REST、开仓风控门禁优先读取新鲜中心状态（过期/标记 stale 则不用且暂停新开仓，不再回退 signed REST）、runtime 非订单持仓同步/硬顶扫描改为 fresh central state 优先且 stale 时跳过、account snapshot 多账户采集默认间隔 `65s` 并遇到 rate-limit 即停止后续账户、public 60秒 top path/source 归因、public guard 先平滑到 `60/min`，后因 `public:C/v14 /fapi/v1/klines` 429 再降到 `45/min + 1400ms`、A/v11/C/v14 启动同步不再在中心状态缺失时 fallback 到 signed account config/positions/balance、A/B/C scanner 余额摘要 helper 不再 signed fallback、19:29 CST 自然恢复验证 `fresh_accounts=3/partial_error=0/P0=0`、account 组件部署前 import smoke、23:29 CST 手动重启 account 旧进程载入 recovery guard、2026-06-03 Testnet construction mode 暂停 Binance-facing 服务并用 `runtime_data_reset.py` 将运行数据归零、`core/account_state.py` + `account_state_service.py` 中心状态文件/service 基础（`runtime/account_state_latest.json`）已部署并完成第一段 fresh-run、`ExecutionEngine` open/close confirmation 已有中心状态证明路径且默认要求 fresh central account-state，缺失/过旧时 fail-closed 不再回退 position REST、`core/binance_api_queue.py` 独立 API 队列 foundation 已有 SQLite 持久化、优先级、cooldown、lease、幂等与结果记录，`binance_api_queue_service.py` / `crypto-binance-api-queue.service` 已作为施工服务草案并启动验证、`core/account_state_stream.py` 已能把 Binance Futures `ACCOUNT_UPDATE` user-data-stream 事件归并为中心 account-state 账户行、`account_state_service.py --stream-events ... --stream-strategy ...` 已能离线应用 user-stream JSONL 到中心状态并持久化 offset/dedup state、`core/binance_user_stream.py` 已有 listen-key 状态持久化、keepalive/restart 到期判断、start/keepalive/close 队列请求规格，并能通过 queue executor 自动获取/续期 listen-key、`core/binance_api_executor.py` 与 `binance_api_queue_service.py --execute` 已能执行队列中的 public/signed REST 请求、签名、记录结果并在 418/429/-1003 时设置 queue cooldown、`core/binance_user_stream_runtime.py` 与 `binance_user_stream_service.py` 已有 user-data-stream message parsing、event JSONL 持久化、latest-event 文件、messages-file 离线回放和可选 websocket 模式，A/v11、B/v16、C/v14 三个 user-stream systemd 草案已纳入 release并启动到 idle 运行态，`core/binance_api_queue_client.py` 已把 A/B/C scanner client signed REST、client public exchangeInfo REST、scanner/哨兵/market-data-cache/VPB/order-rules/counterfactual/daily-review public REST 接入独立 API 队列且默认 fail-closed；2026-06-03 03:22 CST central account-state fresh-run 达到 `fresh_accounts=3/partial_error=0`；发现并修复后续阻塞：user-stream 不再 `Wants=crypto-account-state.service`，account-state 改为 `Type=oneshot` 手动基线服务，避免 900 秒 polling 反复触发 signed balance；`signed:A` cooldown 到期后，旧 deferred balance 请求已 failed/no-retry，API queue + A/B/C user-stream-only fresh-run 已启动，服务统一 `User=ubuntu`，idle touch 可把 central state summary 维持为 `fresh_accounts=3/partial_error=0`；sentinel systemd unit 已纳入 release 并安装；market-data-cache + sentinel 可在中心队列下 clean 返回 `200`；`core.binance_api_queue_client` 现在遇到 active queue cooldown 会在 submit 前 fail-closed，不再继续写入 `api_requests`，并会把超时且未 leased 的请求标 failed 避免晚到重放；施工期 `crypto-binance-api-queue.service` executor 已降到 `--interval 60`；A/B/C/sentinel/market-data public queue waits 至少 `180s`；queue client 新增 pending-backlog fail-closed，默认 public pending 上限 `3`、signed 上限 `20`，达到上限不入队；A/B/C scanner universe 支持 env 小样本限制，默认不变。
   - 未完成：scanner fresh-run 暴露 public Kline 压力仍可触发 Testnet `HTTP 418/-1003`。最新阻塞为 A/v11 Kline public `HTTP 418/-1003`，queue public cooldown 已在 `2026-06-03T10:31:59.888+08:00` 清除；A/B/C scanner、sentinel、market-data-cache 已在 `10:06 CST` 再次停止。cooldown 清除后，market-data-cache 单 public producer 运行 5+ 分钟 clean，新增 ticker 请求均为 `200` 且无 cooldown。market-data-cache + sentinel `300s` 施工期 systemd interval 已准备/上传。用户最新要求改为 offline-first：先停止全部 Binance-facing/test 服务，离线落地 P0/P1 优化，再统一启动服务器 staged fresh-run。下一次测试按 60s executor 更慢分阶段重试：cache、sentinel、A/v11、B/v16、C/v14 逐步启动；仍需完整 30 分钟 clean fresh-run。account-snapshot、system-alerts/data-maintenance 仍未进入新体系 fresh-run。
   - 验收：30 分钟内 journal 无 418/429/-1003；账户快照可自然保持 fresh；策略服务遇到单点 ban 不继续延长 ban；入口页显示 cooldown/source/top paths。

2. **P0-B Replay/live 同路径**
   - 目标：A/B/C scanner 和 replay 使用同一套纯策略 gate/decision 函数，实盘只负责编排、下单、持久化。
   - 已完成：`core.replay` 事件模型、观测型 `ReplayGateResult`、gate audit、OPEN_SKIPPED 归因覆盖；A/v11、B/v16、C/v14 entry-threshold gates 已抽到 `core.strategy_gates` 并由 live scanner 调用；B/v16 15m confirmation gate、C/v14 15m confirmation gate、C/v14 low-score tail guard、C/v14 stale entry-price market-data guard、C/v14 ATR market microstructure gate、A/v11 resonance-adjusted score、resonance-required gate、tradability gate、replacement-signal eligibility、pool-capacity replacement eligibility、releasable-position selection 与 market microstructure gate、A/B/C no-same-symbol position gate、A/B/C central account-state availability gate、C/v14 same-side position/sector/stop-loss gates、B/v16 stop-loss gate、B/C score-max gate、B/v16 active-position limit gate、B/v16 watchlist score adjustment、B/v16 small-live stage guard、C/v14 consecutive-loss global cooldown、A/v11+C/v14 per-symbol datetime cooldown、B/v16 scan-count cooldown、A/C ATR-zero blacklist、B loss blacklist、A/B/C same-timeframe already-held pre-filter、B/C positive quantity execution gate、A/v11 fixed-margin sizing gate、A/B/C execution result preflight-vs-failure gate 也已抽到 shared pure function；`tests/test_strategy_gates.py` 已提供第一版 shared gate parity smoke；`core.strategy_gate_cases` 已提供 JSON-like same-input gate case runner，可将 replay/live 输入送入同一 pure gate 并检查 expected allowed/reason；2026-06-03 已把 case runner 扩到更多现有纯 gates：account-state、active-position、A/v11 margin sizing/market/replacement/resonance/releasable、B/v16 confirmation/small-live、C/v14 confirmation/tail/stale-price/market、same-side/timeframe/sector/blacklist/symbol cooldown/scan cooldown/watchlist adjustment 等；`replay_live_parity_audit.py` 已新增 exact same-input 审计，只把事件 payload 中持久化的 `strategy_gate_case(s)` 送入 shared pure gate runner，单独报告 exact case 覆盖率、pass/mismatch/error 和缺 case 行，并接入入口页、Aliyun refresh/shadow、反向同步和 live-context 拉取；A/v11、B/v16、C/v14 scanner 已在关键 `OPEN_SKIPPED` 路径持久化 exact `strategy_gate_case`，覆盖中心账户状态、重复持仓、执行数量/预检，并补 A/v11 market/tradability/margin、B/v16 active-position limit/small-live stage/confirmation/threshold、C/v14 stale-price/ATR market-data/confirmation/tail 等场景；strict audit 现在也单独读取 `sentinel_scans` scan-level gate rows，A/v11 前置 blacklist/timeframe/stop-loss cooldown/symbol cooldown/threshold、B/v16 score-max、C/v14 blacklist/timeframe/symbol cooldown/same-side/score-max/stop-loss/sector 等前置拒绝会写入 exact case；B/v16 threshold reject 与 C/v14 tail-guard reject 已开始写入多 gate `strategy_gate_cases` orchestration chain，分别保留前序 confirmation pass 和最终失败 gate；A/v11 pool-full replacement 路径现在写入 `replacement_signal + pool_capacity` 链，强制替换失败时追加 `replacement_release_result`。
   - 2026-06-03 19:56 补充：A/B/C entry-risk reject 已写入 `entry_risk` exact case，覆盖总仓位上限、方向仓位上限、可用余额保护三类风控拒绝。
   - 2026-06-03 20:04 补充：A/B/C non-preflight `OPEN_FAILED` 已写入 `execution_result` exact case，preflight skip 与真实执行失败现在都能被 strict audit 区分。
   - 2026-06-03 20:26 补充：`replay_live_parity_audit.py` 已新增 close-flow 独立统计，close/replacement/post-open sizing cleanup rows 与 open-flow、scan-level 分开报告 exact coverage/pass/mismatch/error；A/v11 `EVICT_FAILED`、normal/hard-stop close failure、post-open sizing close failure，以及 B/C normal close、forced-close failure 现在写入 `execution_result` exact case；B/C hard-stop exception 也会写成 `FORCED_CLOSE_FAILED` 观测事件。audit 已对 `raw`/`raw_event` 重复 nested case 去重。
   - 2026-06-03 20:46 补充：A/v11 replacement release 现在可在 detail 模式下返回所有候选仓位的 `a_v11_releasable_position` exact case，并把成功/失败释放结果与 close `execution_result` 证明一起写入/返回。`EVICT_CLOSE`、`EVICT_FAILED`、`OPEN_RETRY_AFTER_EVICT`、`OPEN_SKIPPED` 可携带 candidate -> release_result -> close_execution 链。
   - 2026-06-03 20:57 补充：A/v11、B/v16、C/v14 成功 `OPEN` 事件现在写入 `execution_result` exact case，strict audit 可验证执行成功/确认成功，不再把成功开仓全部当 missing-case gap。
   - 2026-06-03 21:10 补充：B/v16 成功 `OPEN` 事件现在写入 `[confirmation, entry_threshold, execution_result]` exact chain；C/v14 成功 `OPEN` 事件现在写入 `[confirmation, tail_guard, execution_result]` exact chain。B/C 成功开仓已从单 execution proof 前进到第一版 orchestration proof。
   - 2026-06-03 21:38 补充：A/v11 成功 `OPEN` 事件现在写入第一版 open-success orchestration chain。普通 Hanmuxia 开仓包含 entry-threshold、resonance-required（如适用）、pool-capacity、market/tradability/position/sizing/account-state/risk 和 execution；VPB 开仓不伪装成 Hanmuxia threshold pass，只带 shared pool/safety/risk/sizing/execution context；replacement 成功开仓会把 release cases 并入最终 `OPEN`。同时修复 A/v11 成功执行后 `min_notional_adjustment` 与 margin bounds 未定义的旧隐患。
   - 2026-06-03 21:50 补充：B/v16 成功 `OPEN` 事件现在把 runtime pass context 扩到 duplicate-position、central account-state、entry-risk、positive-quantity；C/v14 成功 `OPEN` 事件现在把 runtime pass context 扩到 duplicate-position、central account-state、entry-risk、stale-price、market-microstructure、positive-quantity。B/C 成功开仓 risk/position/quantity/market context gap 已补第一版。
   - 2026-06-03 21:56 补充：A/v11 post-open sizing cleanup 成功/失败事件现在写入 `[confirmed a_v11_margin_sizing fail, close execution_result]` exact chain，成功撤销也不再是 close-flow missing-case gap。
   - 2026-06-03 22:04 补充：`replay_live_parity_audit.py` 现在输出 historical same-input acceptance conclusion，按 open-flow、scan-flow、close-flow 分别给出 `accepted/coverage_gap/blocked_by_mismatch/missing_exact_cases/not_applicable`，整体输出 `acceptance_status`、`acceptance_conclusion`、`readiness_score_pct`、`blocking_flows`、`fresh_run_required` 和下一步动作。
   - 未完成：最终 staged fresh-run 仍需生成新数据来证明部署后的 exact coverage/pass rate；没有 exact case 的旧历史行只能算 coverage gap，不能算 replay/live parity 已通过。
   - 验收：给定同一时间、币种、上下文，replay 与 live 入场/否决结论一致；关键 OPEN_SKIPPED/OPEN_FAILED 无未知 gate。

### Long-term P1 - P0 稳住后并行推进

1. **P1-A A/v11 trailing-pullback 质量决策**
   - 当前问题：已批准 rollout 后进入 rollback-watch；需要决定继续、收窄或回滚。
   - 已完成：`a_v11_rollout_review.py` 已输出 24h/72h/168h 窗口、成本、强平贡献、top losers/winners、close reasons、side/timeframe PnL；2026-06-03 已新增 A/v11 专用 `decision_packet`，包含 selected live parameter、approval rationale、risk evidence、maturity、rollback path、automation=disabled_report_only；2026-06-03 23:08 补充：rollout review 已新增 exit-model attribution，把已平仓结果拆成 `atr_trailing_stop`、`take_profit`、`max_loss_guard`、`profit_retrace_guard`、`range_exit`、`exchange_auto_close`、`other`，并加入 `0.10%/0.15%/0.25%` 费用/滑点敏感度；23:59 补充：rollout review 已新增本地 Kline cache 驱动的 replay/fill comparison，按 A/v11 `OPEN` -> `CLOSE/FORCED_CLOSE` 配对后用 `core.replay_fill` 重放 approved ATR trailing rule，并报告 paired/completed、status counts、实际/重放 PnL 差异、replay exit reason 和 top deltas；缺 Kline cache 时显式报缺口，不调用 Binance API。
   - 验收：按 24h/72h/168h 窗口拆出亏损来源、top losers、出场原因、regime、强平/成本贡献，并形成可执行 decision packet；没有 operator-quality evidence 不自动回滚。

2. **P1-B B/v16 full-live 候选质量决策**
   - 当前问题：ATR stop bands 与 overheat cap 85 已 full-live，但 24h after-cost PnL 和 forced-close rate 承压。
   - 已完成：`b_v16_rollout_review.py` 已输出 24h/72h/168h report-only windows、cost-adjusted PnL、failure/forced-close pressure、top losers/winners、decision packet 和 portal visibility；2026-06-04 补充：review 已新增 realized profit/loss、profit factor、exit-model attribution（`forced_or_hard_stop`、`atr_stop_band`、`exchange_auto_close`、`take_profit`、`signal_exit`、`other`）、open-failure attribution（min-notional、position-side mismatch、margin、timeout/unknown、exchange-rule preflight、account-state、quantity/lot-size 等）以及 `0.10%/0.15%/0.25%/0.35%` cost sensitivity；portal 已显示 72h PF、主 exit model、主 open-fail reason、0.25% cost-stress PnL。2026-06-04 04:01 补充：B/v16 rollout review 已新增第一版本地 replay/fill comparison，配对 B/v16 `OPEN` -> `CLOSE/FORCED_CLOSE`，读取 `research_store/klines` / `runtime/kline_cache` 和可选 depth snapshots，重放 SL/TP + `1.0 ATR` activation/pullback，报告 paired/completed、actual-vs-replay delta、replay exit reason、depth fill/slippage；04:08 补充：B/v16 hard-bottom 与 profit-retrace protective exits 已纳入 replay parity。自动回滚仍关闭。
   - 验收：拆 hard-stop/forced-close/open-fail/high-vol regime 贡献，形成继续观察、收窄或准备回滚建议；没有成熟窗口和账户风险证据不自动回滚。

3. **P1-C 完整 replay/fill 引擎**
   - 目标：OPEN_SKIPPED 放行后的成交、持仓、出场、费用/滑点仿真统一到一个 replay/fill 引擎。
   - 已完成：`core.replay_fill` 第一版 deterministic fill kernel，支持 long/short、SL/TP、fee、slippage、保守 intrabar stop/take 冲突处理、end-of-window exit，并有单元测试；2026-06-03 `counterfactual_open_skips.py` 已用该 kernel 计算 OPEN_SKIPPED 假设放行后的 fill/PnL，并把 replay_fill payload 写进结果；同日新增通用 percent trailing-stop activation/exit，支持 long/short 保守 intrabar；恢复仓 review 已补账户快照路径 MFE/MAE、MFE回撤、first-seen 和 sample count，作为 recovery-exit replay 的只读事实层；2026-06-03 22:17 补充：fill kernel 已支持 ATR-based trailing activation/pullback（`atr`、`trailing_activation_atr`、`trailing_stop_atr`），可表达 A/v11 trailing-pullback 的核心出场形态；2026-06-03 22:29 补充：`counterfactual_open_skips.py` 已对带正 ATR 的 A/v11 15m/30m skipped rows 显式接入 approved ATR trailing 参数，15m 为 `1.0/1.0 ATR`，30m 为 `1.2/0.8 ATR`，并在 `replay_fill.exit_model` 标注 `a_v11_atr_trailing`；2026-06-03 23:25 补充：recovery-position review 已读取 `SIGNAL` / `SIGNAL_ONLY` / `OPEN_SKIPPED` 作为同策略信号证据，标注同向重开支持与反向信号人工复核；23:59 补充：A/v11 rollout review 已有第一版本地 cache bar-by-bar replay/fill comparison；2026-06-04 00:17 补充：recovery-position review 已新增 report-only 策略专属退出证据层，按 A/B/C profile 标注 `opposite_signal_manual_review`、`mfe_drawdown_manual_review`、`recovery_trailing_watch`、`same_side_reopen_hold_bias` 或 `keep_shadow_monitoring`；00:44 补充：recovery-position review 已新增第一版本地K线 bar replay evidence，读取本地/镜像 `runtime/kline_cache`，按 first-seen snapshot 到 snapshot_ts 通过 `core.replay_fill` 重放，标注 `bar_replay_exit_manual_review` / `bar_replay_hold_bias` / `replay_data_gap`，不调用 Binance；00:54 补充：OPEN_SKIPPED 反事实报告已把每条 `replay_fill` 提升为汇总证据，显示 exit model/reason、gross、fee、slippage、net、avg bars 和按出场模型分组的成本/收益；01:26 补充：fill kernel 与 counterfactual 已支持 report-only partial-fill caps，可按最大成交数量/名义额截断目标仓位，或在 strict no-partial 模式下返回 `fill_error:*`，并在 JSON/MD/portal 汇总 requested/filled/unfilled、partial count、avg fill ratio 和 liquidity assumption；01:45 补充：`core.replay_fill` 已有第一版 depth-aware entry primitive，可选 `entry_order_book` 让 long 吃 asks、short 吃 bids，计算平均入场价、depth slippage、levels used、partial fill，显式空盘口 fail-closed；01:56 补充：`core.replay_depth_cache` 已把本地/镜像 `runtime/depth_cache` 盘口快照接入 OPEN_SKIPPED counterfactual，报告和入口页可显示 order-book fill、depth snapshot age 与 depth slippage，仍不调用 Binance；03:10 补充：`research_depth_backfill.py` 已提供 depth snapshot plan/submit/ingest 路径，可在 staged run 中把当前盘口采样经 central queue 写入 `research_store/depth_snapshots` 与 `runtime/depth_cache`；03:39 补充：`core.replay_depth_cache` 已能直接读取本地/镜像 `research_store/depth_snapshots` JSONL/parquet，因此 staged depth ingest 后无需先刷新 `runtime/depth_cache` 也能被 OPEN_SKIPPED、A/v11 rollout replay 与 recovery replay 消费；05:20 补充：`counterfactual_open_skips.py` 已新增 report-only fill stress grid，在 completed `replay_fill` payload 上派生 `base/conservative/stress` 三档成交率和额外入场冲击敏感度，Markdown/portal 可直接看保守成交下的 net PnL 折损。
   - 未完成：长历史 Kline 窗口、真实 staged depth 采样覆盖、queue-priority/market-impact simulation 和 governance 仍未完成；当前 depth-aware 只能消费已有或 staged ingest 后累计的本地盘口，不直接请求 Binance。
   - 验收：每个实盘 OPEN_SKIPPED 能回答“若放行，按同一出场规则会怎样”。

4. **P1-D 灰度/回滚门禁增强**
   - 已完成：2026-06-03 `strategy_evolution_gate.py` 已为 decision 输出 `decision_packet`，包含改动、预期优势、风险、证据成熟度、回滚路径、operator action 和 `disabled_report_only` 自动化状态；`rollback_watch_review.py` 已在 P0/P1 rollback-watch 报告中渲染这些 packet；同日新增 close-failure attribution，拆 raw/resolved/unresolved close failures 和 compact reason buckets，并在 rollback-watch/portal 中显示；23:42 补充：post-approval quality 现在带 `0.10%/0.15%/0.25%/0.35%` paper cost/slippage sensitivity，保守 `0.25%` 成本越过 rollback-review loss line 时会进入窗口质量和 decision packet 风险项；2026-06-04 00:34 补充：evolution gate 已新增 strategy/change-family `gate_profile`，A/v11 replacement、A/v11 trailing、B/v16 exit-risk、C/v14 sample expansion、confirmation-policy 和 default profiles 有不同 P0/P1 样本阈值与 24h/72h/168h closed-sample 要求，decision/window quality/decision packet/Phase 8 audit 均展示该 profile；full-live review 另有 report-only `regime_robustness` 评分与 audit gap；01:07 补充：post-approval mature windows 现在按 gate profile 计算最低 PF 阈值，`CLOSE/FORCED_CLOSE` 会拆 realized profit/loss 并输出 24h/72h `profit_factor`，低 PF 成熟窗口会进入 bad quality 和 decision packet 风险项；01:26 补充：P1-C 的 partial-fill cap 近似可作为 paper-fill 证据输入；01:38 补充：post-approval window quality/decision packet 已有 report-only fill-liquidity sensitivity，按 fill ratio `1.00/0.75/0.50/0.25` 展示 filled/unfilled notional、scaled after-cost PnL 和 conservative `0.50` 风险项，不触发自动回滚；04:30 补充：rollback-watch review 已新增 `operator_readiness`，检查决策包完整性、回滚路径、风险项、证据成熟度、自动化关闭状态、关闭失败压力和 replay readiness，输出 `operator_ready` 与 governance gaps；05:12 补充：`rollback_execution_plan.py` 已把 operator-ready rollback-watch 项转成 report-only 执行预演，生成 dry-run release-manager 命令提示、disabled apply 命令引用、freeze/evidence/rollback-path/post-check/abort checklist，并接入口页/Aliyun/release/sync/live-context；05:31 补充：`rollback_automation_guard.py` 已新增自动回滚闸门审计，检查显式 policy、replay readiness、dry-run plan、reviewed release id 与 apply-disabled enforcement，输出 JSON/MD/portal，永远保持 `automatic_rollback_allowed=false` 和 `apply_enabled=false`，最多到 `preconditions_met_report_only`。不执行回滚/部署/服务重启。
   - 未完成：完整 paper fill/slippage simulation（含 bar-by-bar/订单簿深度/排队撮合）、operator-approved automatic rollback procedure；当前 fill-liquidity 仍是比例假设，dry-run 执行清单和 automation guard 只帮助人工预演/审计，不代表自动执行或允许 apply。
   - 验收：每个 P0/P1 策略候选必须包含改动、优势、风险、回滚路径；自动升级/回滚仍默认关闭，直到验收充分。

5. **P1-E 长历史 K线/研究仓增强**
   - 当前问题：已有近期 `klines/features`，但不是长历史 K线仓。
   - 已完成：2026-06-04 `research_kline_features.py` 已从“只导出本轮 cache”升级为默认合并已有 research_store `klines` 分区与新同步 cache 行，按 `(symbol, interval, open_time_ms)` 去重，并从合并后的时间序列重建 `features`；manifest 记录 cache/existing/merged 行数和覆盖范围。重复离线刷新现在可自然累计更长 K线历史，不调用 Binance、不增加 SQLite 压力。2026-06-04 `research_store_query.py` 又补了 30 天 Kline 覆盖验收：默认要求 `15m/30m/1h` 关键周期各自达到 `30` 天，输出 `kline_acceptance` 的 `ok/coverage_gap/no_klines`、缺失周期和不足周期；入口页研究仓卡片和 K线表直接显示该状态。2026-06-04 `research_kline_backfill.py` 已补 queue-safe backfill planner：默认 plan-only，可从 `events` / `sentinel_scans` 在 K线为空时选 symbol，可用 `--submit` 写入 central API queue，可用 `--ingest-done` 合并 DONE Kline 响应回 `klines/features`；Aliyun refresh/shadow、release/deploy、反向同步、live-context pull 和入口页已接入。2026-06-04 `research_depth_backfill.py` 已补 depth snapshot planner/warehouse：默认 plan-only，可用 `--submit` 写入 `/fapi/v1/depth` 队列请求，可用 `--ingest-done` 合并 DONE depth 响应到 `depth_snapshots` 并刷新 `runtime/depth_cache`；研究仓查询和入口页展示 depth coverage。2026-06-04 `core.replay_depth_cache` 已补直接读 `research_store/depth_snapshots`，使该研究仓数据能立即喂 replay/fill。2026-06-04 `research_store_retention.py` 已补分区 retention planner：默认只生成 `hot/warm/archive_candidate/invalid_date` 计划和报告，显式 `--apply` 才把旧分区移动到 ignored `research_store_archive/`，不删除数据；入口页、Aliyun refresh/shadow、release/deploy、反向同步和 live-context pull 已接入。2026-06-04 `research_store_compaction.py` 已补分区 compaction planner：默认只生成大文件/多行/多 row-group/格式转换候选计划和报告，显式 `--apply` 会先备份到 ignored `research_store_compaction_backup/` 再重写单分区文件；入口页、Aliyun refresh/shadow、release/deploy、反向同步和 live-context pull 已接入。
   - 未完成：仍未真实执行 Kline/depth 的 `--submit` / queue executor / `--ingest-done` staged backfill/sampling，因此还没有在真实 post-refresh research-store 数据上跑出 `kline_acceptance=ok` 或足够 depth coverage；compaction 目前只是安全 planner，真实大分区出现后仍需人工/运维选择是否 `--apply`，并复跑查询与 replay readiness。
   - 验收：30 天以上策略漏斗、replay feature、sentinel outcome 查询秒级可用，并不依赖 SQLite 长期膨胀。

### Long-term P2 - P0/P1 闭环后推进

1. **P2-A Sentinel 深化**
   - 继续拆 `not_scanned`：未进 watchlist、scanner universe 不支持、cadence miss、mirror truncation，并接 full replay/fill outcome。

2. **P2-B Recovery-position 策略**
   - 已补 takeover 后账户快照路径 MFE/MAE、MFE回撤、first-seen 和 sample count（report-only）；已补同策略同币种信号证据，显示同向重开支持、反向 open-like 信号人工复核和最新 compact signal facts（report-only）；已补策略专属退出证据，按 A/B/C MFE/MFE回撤 profile 标注反向信号复核、MFE回撤复核、trailing观察、同向持有倾向或继续监控（report-only）；已补第一版本地K线 bar replay evidence，用 first-seen snapshot 近似恢复仓接管入口并通过共享 `core.replay_fill` 给出 exit review / hold bias / data gap。
   - 未完成：自动 recovery-exit 规则仍需长 Kline 窗口、更完整 bar-by-bar/depth replay、原始 exchange open time 证据和治理门禁后才能考虑。

3. **P2-C 已有候选继续观察**
   - A/v11 replacement-quality guarded small-live；B/v16 confirm-soft-pass shadow；C/v14 受控扩样漏斗/PF。

4. **P2-D 工程治理**
   - GitHub CI 接入 `git_change_guard.py`；attention browser ack 链路复核；历史凭据轮换/清理。

5. **P2-E 开源框架 PoC**
   - NautilusTrader / Qlib / Freqtrade / vectorbt 只做小样本 PoC，除非 replay 准确率、查询速度或开发效率显著提升，否则不迁移主系统。

### 2026-06-01 N2 进展补充：Replay/live 门控审计

- [x] 新增 `replay_gate_audit.py`：从 SQLite `events` 读取 live `SIGNAL/OPEN/OPEN_SKIPPED/OPEN_FAILED`，统一送入 `core.replay` 分类，输出 gate 覆盖率、未知 gate、按策略分布和主要 gate。
- [x] 接入阿里云轻量分析、每日 shadow review、反向同步、live-context 拉取和入口页。入口页第一屏显示 `Replay门控`，详情区显示 `Replay / Live 门控审计`。
- [x] 已补 A/v11 legacy `OPEN_SKIPPED` 归因：池满/方向持仓上限归 `capacity`，同币种已有仓归 `position`，交易所/preflight 拒单归 `execution`。2026-06-01 20:26 CST Tencent live replay gate audit 已达到 `gate_coverage_pct=100.0%`、`unknown_gate=0`。
- [x] 新增第一版纯 gate 结果接口：`core.replay.ReplayGateResult` / `evaluate_observed_gate()`，`replay_gate_audit.py` 已改用该 API；旧 `classify_replay_decision()` 保持兼容。
- [x] 新增 `replay_live_parity_audit.py`：只审计 serialized `strategy_gate_case(s)`，输出 `runtime/replay_live_parity_latest.json` / `reports/replay_live_parity_latest.md`，入口页展示 `Replay/live 同输入审计`。这比 observed gate audit 更严格；缺 case 行是 gap，不可猜测。
- [ ] 下一步：抽 A/B/C 纯策略函数，让 replay 和 scanner 直接调用同一 gate，而不是只做事后分类。
- 验收口径：observed gate 覆盖率需长期 >90%，且未知 gate 不集中于关键 OPEN_SKIPPED/OPEN_FAILED；exact parity 还需要足够 `strategy_gate_case(s)` 覆盖、pass rate 接近 100%、mismatch/error 为 0，才能声称同输入一致。

用户最新目标：作为决策者，入口页必须先给出经过筛选和思考后的结论；系统优化的核心不是展示更多信息，而是让盈亏、策略质量、进化机会和风险暴露更清楚。当前样本仍偏少，但不再全局盲目放宽入场门槛。

### 当前决策

- A/v11：不继续放宽。它是稳定主样本来源，重点守住 100 USDT 保证金纪律、同币种禁止叠仓、满仓替换保护和异常告警。
- B/v16：已全量开放 2026-05-27 两个已验证候选（ATR 分档止损、过热封顶 85）。下一步只观察样本质量、硬止损率、真实 PnL 和关闭确认，不额外放宽。
- C/v14：已经进入受控扩样窗口。下一步观察入场候选到开仓转化、OPEN_SKIPPED 分层、胜率/PF；在没有一段时间验证前，不再二次放宽。
- 全局原则：样本不足可以通过“受控扩样 + 自动回滚条件”解决，不能通过无限降低门槛解决。

### 新增阶段 N1 - 决策者入口页重构

**目标**：`reports/index.html` 第一屏只回答四个问题：现在赚亏多少，哪些策略有效/无效，系统有没有风险，是否出现可批准的更优方案。

关键技术节点：
- [x] `portal_dashboard.py` 新增 executive summary 数据层，融合账户快照、策略真相台账、进化门禁、关注台账、系统告警。
- [x] 第一屏展示三策略当前动作：稳态监控、观察全量候选、受控扩样、暂停扩展、需要回滚。
- [x] findings 只展示 top 6 关键发现，详细表下移。
- [x] 每个结论必须能下钻到来源：account snapshot、truth ledger、evolution gate、counterfactual、attention ledger、research store。

验收：
- 决策者不看详情页，也能知道是否需要干预。
- P0/P1、账户异常、策略升级机会、样本扩张状态必须在第一屏出现。

### 新增阶段 N2 - 统一 replay / live 同路径

**目标**：让回测、影子、反事实、实盘经过同一套策略门控路径，避免“回测能赚、实盘不开”或“反事实口径不一致”。

关键技术节点：
- [x] 定义统一事件模型起点：`core/replay.py` 提供 `ReplayEvent`、`ReplayDecision`、事件类型归一和初始 gate 分类。
- [x] 定义统一 gate 结果面：`ReplayGateResult` / `evaluate_observed_gate()`，先覆盖观测型 live event；后续 A/B/C 纯策略 gate 应返回同一 shape。
- [x] 新增 replay 特征对齐数据集：`replay_feature_dataset.py` 从 DuckDB research-store 读取 `events` + `features`，把 A/B/C 的 OPEN、OPEN_SKIPPED、OPEN_FAILED、SIGNAL 对齐到最近同币种历史特征行，并输出 `research_store/replay_features`、`runtime/replay_feature_dataset_latest.json`、`reports/replay_feature_dataset_latest.md`。
- [ ] A/B/C 抽出纯策略函数和门控函数，实盘 scanner 只负责编排和下单。
- [ ] replay 引擎从 SQLite/Parquet 读取历史事件、K线、哨兵上下文，重放 OPEN_SKIPPED/OPEN/CLOSE。
- [x] `counterfactual_open_skips.py` 已先把 OPEN_SKIPPED 归一到 `core.replay.ReplayEvent` / `ReplayDecision`，反事实过滤层分组优先使用统一 replay gate。
- [ ] 反事实评估的成交/出场仿真仍需继续迁到完整 replay 引擎，不再各脚本单独复刻过滤逻辑。
- [ ] 每个实盘 OPEN_SKIPPED 必须能回答：如果放行，按同一出场规则会怎样。

验收：
- 给定同一时间、同一币种、同一上下文，replay 与 live 对入场/否决结论一致。
- C/v14 的“信号多、开仓少”可被分解到明确门控层。

### 2026-06-01 N2/N6 进展补充：Binance API 全局压力闸门

- [x] 新增 `core/binance_api_guard.py`，让 A/v11、B/v16、C/v14 和账户快照服务共享同一个 signed REST 文件级闸门。
- [x] 三套 Binance client 已接入 `wait_before_request()` / `record_response()`：每次 signed REST 前全进程限速，遇到 `418/429/-1003/too many requests` 会持久化 `banned_until_ms`，让其他进程自动等待。
- [x] `system_alerts.py` 已读取 `runtime/binance_api_guard_state.json`，入口页可暴露 API guard cooldown、最近请求路径和 top signed REST 路径。
- [x] 三套 Binance client 的账户/仓位缓存 TTL 改为 `BINANCE_ACCOUNT_CACHE_TTL_SEC`，默认 `5s`，订单提交后仍强制失效，减少扫描周期重复 `positionRisk`。
- [x] API guard 增加 `BINANCE_API_GUARD_MAX_REQUESTS_PER_MIN`，默认 `120/min`，和 `BINANCE_API_GUARD_MAX_ACCOUNT_REQUESTS_PER_MIN`，默认 `80/min`，并输出 `rolling_count_60s/top_paths_60s` 给系统告警。
- [x] A/v11、B/v16、C/v14 的开仓风控门禁改为单次 `positionRisk` 快照派生总仓位/方向仓位，并单次读取余额；`core/exchange_state.py` 统一解析 active position、side count、symbol lookup、USDT balance。
- [x] A/v11 平仓提交在执行层已提供 `quantity/order_side` 时不再二次查询 `positionRisk`；A/v11 余额读取切到 `/fapi/v2/balance`；账号快照裸跑缺少 `BINANCE_*` 环境变量时不覆盖最新有效快照。
- [x] API guard 增加第一版交易关键路径优先级：普通 signed read 在总预算前预留默认 `20/min` 额度给 order/cancel/leverage/margin 等交易路径；系统告警显示 priority counts、normal limit、trade reserve 和 `last_error_*`。
- [x] open/close confirmation 重复 `positionRisk` 合并第一步：`ExecutionEngine` 在单次 close 操作内复用 0.75 秒内的新鲜仓位快照，避免 close target 与紧接 retry target 连续重复读取；带 sleep 的确认仍刷新。
- [x] 2026-06-02 API ban 复发后保守降压：signed REST 默认总量 `90/min`、单账户 `45/min`、最小间隔 `650ms`、ban grace `10min`；账户快照先尊重共享 guard cooldown；A/B/C client 保留更长错误体用于解析 `banned until`。
- [x] public market-data polling 纳入第一层统一预算：A/B/C scanner public `fetch_json`、B/v16 live aggTrades、sentinel ticker、market-data-cache ticker、order-rule bookTicker/premiumIndex 共用 `BINANCE_PUBLIC_API_GUARD_MAX_REQUESTS_PER_MIN`（默认 `120/min`）和同一 ban cooldown；无明确 `banned until` 的 418/429 fallback cooldown 默认 `30min`。
- [ ] 下一步：把账户余额/仓位迁到 user-data-stream 或更低频的集中账户状态服务，减少 `positionRisk` 轮询。
- [ ] 下一步：把 open confirmation 与跨操作账户状态迁到 user-data-stream/集中账户状态服务；close 确认缓存继续用真实 live 结果验证是否还可扩大 TTL。
- [ ] 下一步：做 public request 归因/缓存审计，定位 6000 requests/min IP 级限制是否主要来自 K线/ticker/depth/aggTrades 或外部共享 IP 噪声。
- [ ] 下一步：guard 升级为独立队列服务或集中 account-state 服务内置限速，进一步替代协作式文件锁。

验收口径：
- 30 分钟 journal 中 418/429/-1003 为 0。
- `runtime/binance_api_guard_state.json` 能显示最近请求路径、冷却窗口和 top paths。
- 即使账户快照遇到 ban，三策略不继续延长 ban；入口页必须显示冷却原因。

### 新增阶段 N3 - Parquet/DuckDB 研究仓

**目标**：SQLite 保留在线当前态和关注项，研究查询迁移到按日 Parquet + DuckDB，提升长期回测和复盘速度。

关键技术节点：
- [x] 新增 `research_store/` ignored 数据目录，先按日导出 `events`、`sentinel_scans`、`account_snapshots`。
- [x] 新增导出脚本：`部署工具/research_store_export.py` 可从 SQLite 只读导出 partitioned Parquet/JSONL，并写 manifest。
- [x] 新增 DuckDB 查询脚本起点：`部署工具/research_store_query.py` 可从 exported research_store 生成策略漏斗、OPEN_SKIPPED gate、哨兵贡献和最新账户概览。
- [x] `portal_dashboard.py` 已读取 `runtime/research_store_summary_latest.json`，在入口页功能状态、详情入口和样本漏斗区展示研究仓摘要。
- [x] 修复 Tencent→Aliyun 常规数据同步：`shadow_sync_from_tencent.py` 默认生成 3 日 bounded SQLite mirror，限制 `sentinel_scans`/`account_snapshots` 行数，并在分析前验证 quick_check、表行数和新鲜度。
- [x] 修复 Aliyun→Tencent 关键报告反向同步：`sync_aliyun_reports_to_tencent.py` 默认按关键性排序上传 index/counterfactual/research-store/runtime，使用 bounded OpenSSH/base64 单文件上传、短超时、重试、错误上限；市场日报等 bulky detail 需 `--include-optional`。
- [x] 数据维护 timer 只保留近期 SQLite 明细，长期研究读 Parquet。`data_maintenance.py` 会清理旧 raw events、账户快照和独立 `sentinel_scans` 分区，避免哨兵扫描长期撑大 SQLite。
- [x] 补 watermark 增量导出：`research_store_export.py` 现在把每个 table/date 分区的 `rows/max_ts/path/status` 写入 manifest，下一次导出会跳过未变化分区；`--force` 可强制重写。
- [x] 补 `klines/features` 研究数据集第一版，避免只依赖事件流做策略研究。`shadow_sync_from_tencent.py` 会同步最新 `runtime/kline_cache`，`research_kline_features.py` 导出 `klines` 与 `features` 分区，`research_store_query.py` 和入口页展示覆盖度。2026-06-04 起，Kline 导出默认合并已有分区与新 cache 并去重重算 features，可通过重复刷新自然累计更长历史；研究仓查询和入口页也会显式判断 `15m/30m/1h` 是否达到 30 天覆盖。后续仍需真正长历史 backfill。

验收：
- 30 天策略漏斗查询在秒级完成。
- SQLite 增长不再影响日报和入口页生成。

### 新增阶段 N4 - 进化门禁升级为灰度/回滚系统

**目标**：系统发现更优方案后，不只提醒，还能进入可审计的灰度、观察、回滚流程。

关键技术节点：
- [x] 门禁增加第一版 regime 分层：post-approval 实盘窗口根据平均绝对涨跌、速度、成交额、方向偏斜和强平密度标记 `high_volatility` / `trend` / `low_liquidity` / `range`。
- [x] 加入第一版费用/滑点与窗口质量模拟：post-approval 实盘窗口按每笔约 400USDT 名义价值、0.15% 成本估算扣费后 PnL，并计算强平率、开仓失败率和窗口质量。
- [x] 每个候选必须有 24h/72h/7d 观察窗口和最低样本数。post-approval 窗口按 24h/72h/168h 分别要求 closed samples `20/50/100`，未成熟窗口继续标记 `maturing` 并写入门禁 blocker。
- [x] 放开后自动生成第一版 rollback watch：已批准 full-live 候选默认进入 `full_live_monitoring`，触发尺寸异常、硬止损风险、账户大亏、扣费后劣化、硬止损增加、OPEN_FAILED 压力时升 `rollback_watch/P1` 或 `rollback_required/P0`。
- [x] 补 post-approval 24h/72h/7d 实盘窗口雏形：`strategy_evolution_gate.py` 从事件库按 full-live approval 生成 24h/72h/168h 实盘观察窗，统计 OPEN/CLOSE/FORCED_CLOSE/OPEN_FAILED/CLOSE_FAILED/OPEN_SKIPPED 和已实现 PnL。
- [x] 补第一版窗口收益质量阈值：已放开候选窗口在样本足够后，如果扣费后 PnL 低于阈值、强平率过高或开仓失败率过高，会升 `rollback_watch/P1`；关闭确认失败仍升 `rollback_required/P0`。
- [ ] 补更细 regime 分层、关闭确认失败更细归因、更稳健的窗口 PF 阈值和自动代码回滚动作。
- [x] 首页最高优先级展示“已验证更优方案”或“已放开候选正在劣化”。`portal_dashboard.py` 第一屏现在单独生成进化优先提示条：回滚项最高，其次 P0/P1 已验证更优方案，否则展示已放开候选 24h quality/OPEN_FAILED/CLOSE_FAILED。

验收：
- 不再因为短窗口好运气而升级。
- 放开后的坏策略能自动升 P0 提醒回滚。

### 新增阶段 N5 - 开源框架 PoC

**目标**：不重写实盘系统，先验证外部框架能否提升研究效率。

候选：
- NautilusTrader：优先评估事件驱动 replay/backtest/live parity。
- Qlib/RD-Agent 思路：评估自动生成研究假设和批量实验。
- Freqtrade/vectorbt：仅参考参数搜索和快速批量回测。

验收：
- PoC 只接一套策略和一小段历史数据。
- 只有当 replay 准确率、查询速度或开发效率显著提升时，才考虑深度迁移。

## 背景

当前系统存在三个核心问题：
1. 腾讯云承担了交易+分析双重角色，内存/存储压力持续增长
2. 阿里云资源闲置，仅做每日凌晨同步+影子实验
3. 策略真相台账缺失，无法区分主动策略PnL与恢复仓PnL

**目标**：将两台服务器重新分工——腾讯云只保留必须调用币安API的功能，阿里云承担所有分析/报告/实验/门禁任务。

---

## 双服务器架构重新分工

### 腾讯云（实盘节点）— 只保留交易核心

| 服务 | 类型 | 职责 | API调用 |
|------|------|------|---------|
| `crypto-scanner.service` | 常驻 | A/v11 策略扫描 | K线+下单 |
| `crypto-scanner-v16.service` | 常驻 | B/v16 策略扫描 | K线+下单+实盘CVD |
| `crypto-scanner-v14.service` | 常驻 | C/v14 策略扫描 | K线+下单 |
| `crypto-market-mover-sentinel.service` | 常驻 | 市场异动哨兵 | 24h ticker |
| `crypto-account-snapshot.service` | 常驻 | 账户快照 | 余额+持仓 |
| `crypto-market-data-cache.service` | 常驻 | 统一行情缓存 | exchangeInfo+ticker |

**总计**：6个常驻服务，全部必须调用币安API。

### 阿里云（分析节点）— 承担所有离线计算

| 任务 | 类型 | 职责 | 需要API? |
|------|------|------|----------|
| 日志同步 | Timer | 从腾讯拉取SQLite/JSONL/报告 | 否 |
| 策略真相台账 | Timer/新增 | 分离主动策略PnL vs 恢复仓PnL | 否 |
| 命令中心生成 | Timer | portal_dashboard.py | 否 |
| 每日复盘 | Timer | daily_market_review.py | 否 |
| 信号质量报告 | Timer | signal_quality_review.py | 否 |
| 反事实评估 | Timer/新增 | counterfactual_open_skips.py | 否 |
| 策略进化门禁 | Timer | strategy_evolution_gate.py | 否 |
| 哨兵贡献评估 | Timer/新增 | sentinel_quality_review.py | 否 |
| 研究记忆构建 | Timer | research_memory_builder.py | 否 |
| 影子实验 | Timer | experiment_runner.py | 否 |
| 系统告警(分析侧) | Timer/新增 | 检查数据新鲜度+报告质量 | 否 |

**总计**：1个同步Timer + 10个分析Timer，无常驻进程，无API调用。

### 反向同步：阿里云 → 腾讯云

阿里云生成的报告需要同步回腾讯云供命令中心展示：
- `reports/strategy_truth_latest.json/md`
- `reports/portal_latest.html`
- `reports/strategy_evolution_latest.json/md/html`
- `reports/counterfactual_open_skips_latest.json/md/html`
- `reports/sentinel_quality_latest.json/md`
- `reports/market_review_latest.md/html`
- `runtime/strategy_evolution_latest.json`
- `research_memory/attention/open_items.json`

实现方式：新增 `部署工具/sync_aliyun_reports_to_tencent.py`，在阿里云分析任务完成后执行反向同步。

---

## 资源与API约束

### Binance API Weight 预算

当前估算（需实测验证）：
- 哨兵：每15秒拉一次24h ticker，约 1 weight × 4/min = ~4 weight/min
- 行情缓存：每15秒拉exchangeInfo+ticker，约 40 weight × 4/min = ~160 weight/min
- 三策略扫描：每轮约100币×2周期×1根K线 = ~200 weight/轮，约每2-3分钟一轮 = ~70-100 weight/min
- 账户快照：每30秒查3账户余额+持仓 = ~6 weight × 2/min = ~12 weight/min

**总计约 250-280 weight/min**，Binance限制1200 weight/min，当前使用约23%。

**约束**：新增功能不得使总weight超过600 weight/min（50%安全线）。

### 内存约束

| 节点 | 当前可用 | 常驻进程RSS | 安全线 |
|------|---------|------------|--------|
| 腾讯云 | ~1.4GiB | ~150MB(三策略+哨兵+快照+缓存) | 新增进程RSS < 50MB |
| 阿里云 | 待确认 | ~0(无常驻) | Timer进程临时占用，完成后释放 |

**约束**：腾讯云不新增常驻服务。阿里云Timer进程为临时占用，不构成内存压力。

### 存储约束

| 节点 | 总量 | 已用 | 剩余 | 红线 |
|------|------|------|------|------|
| 腾讯云 | 50GB | ~12% | ~41GB | <15GB触发紧急归档 |
| 阿里云 | 待确认 | 待确认 | 待确认 | 同样设15GB红线 |

**约束**：新增SQLite表必须有归档策略。分析产出写入阿里云，不增加腾讯云存储。

---

## 执行阶段

### Phase 0 - 安全基线与冻结规则

**目标**：防止在证据层重建期间意外改变实盘风险。

- [ ] 在 `PROJECT_STATE.md` 中添加本计划引用
- [ ] 任何实盘阈值/止损改动前必须先运行 `pull_live_context.py`
- [ ] 所有改动必须经过 `CHANGELOG.md` + `git_change_guard.py`
- [ ] 不从本计划直接改A/B/C实盘阈值，先转为shadow实验或小仓审批

**验收**：未来agent可从 `PROJECT_STATE.md` 发现本文件。无ledger entry不得改策略。

---

### Phase 0.5 - 双服务器架构迁移

**目标**：将分析任务从腾讯云迁移到阿里云，释放腾讯云资源。

#### 0.5.1 确认阿里云资源

- [ ] SSH到阿里云，检查CPU/内存/磁盘/Python环境
- [ ] 确认阿里云Python版本和依赖（numpy, pandas等）
- [ ] 确认阿里云到腾讯云的SSH连通性

#### 0.5.2 升级阿里云同步链路

当前 `shadow_sync_from_tencent.py` 同步内容：
- JSONL日志（scanner_data, logs等）
- 文本日志（stdout.log）
- 报告文件（market_snapshot_latest.json）

需要新增同步：
- [ ] `runtime/event_store.sqlite3`（核心，用于离线分析）
- [ ] `runtime/account_snapshot_latest.json`
- [ ] `runtime/market_data_cache.json`
- [ ] `runtime/strategy_evolution_latest.json`（如果腾讯云还有旧版）
- [ ] `research_memory/` 整个目录

实现：扩展 `shadow_sync_from_tencent.py` 的 `REPORT_FILES` 和新增目录同步。

#### 0.5.3 新增反向同步（阿里云 → 腾讯云）

- [ ] 新增 `部署工具/sync_aliyun_reports_to_tencent.py`
- [ ] 同步阿里云生成的报告到腾讯云 `reports/` 目录
- [ ] 在阿里云分析任务完成后自动执行

#### 0.5.4 迁移腾讯云Timer到阿里云

从腾讯云移除（改为阿里云执行）：
- [ ] `crypto-portal-refresh.service` → 阿里云Timer生成后反向同步
- [ ] `crypto-counterfactual-open-skips.timer` → 阿里云Timer
- [ ] `crypto-market-review.timer` → 阿里云Timer（已有类似）
- [ ] `crypto-strategy-evolution-gate.timer` → 阿里云Timer（已有类似）

腾讯云保留：
- [ ] 6个常驻交易服务（scanner×3 + sentinel + account-snapshot + market-data-cache）
- [ ] `crypto-data-maintenance.timer`（清理腾讯本地数据）
- [ ] `crypto-system-alerts.timer`（检查腾讯本地服务状态）

#### 0.5.5 阿里云新增分析Timer

新增 `crypto-analysis-pipeline.timer`（或拆分为多个Timer）：

```
# 阿里云分析流水线（每日凌晨执行，或每2小时轻量执行）
1. shadow_sync_from_tencent.py --days 3          # 拉取最新数据
2. strategy_truth_ledger.py                        # Phase 1 真相台账
3. counterfactual_open_skips.py                    # 反事实评估
4. sentinel_quality_review.py                      # Phase 6 哨兵评估
5. strategy_evolution_gate.py                      # 进化门禁
6. portal_dashboard.py --out-dir reports            # 命令中心生成
7. sync_aliyun_reports_to_tencent.py               # 反向同步到腾讯
```

**验收**：
- 腾讯云常驻服务全部active，内存使用下降
- 阿里云Timer按时执行，报告正确生成
- 反向同步成功，腾讯云 `reports/index.html` 包含阿里云生成的内容
- 阿里云分析结果与之前腾讯云生成的结果一致

---

### Phase 1 - 统一策略真相台账

**目标**：创建权威的每日/滚动真相表，分离主动策略质量与恢复仓PnL。

**运行位置**：阿里云

**实现**：
- [ ] 新增 `部署工具/strategy_truth_ledger.py`
- [ ] 读取同步到阿里云的SQLite `event_store.sqlite3`、`account_snapshots`
- [ ] 产出：
  - `runtime/strategy_truth_latest.json`
  - `reports/strategy_truth_latest.md`
  - SQLite表（写入阿里云本地）：
    - `strategy_daily_facts` — 每策略每日聚合
    - `position_lifecycle_facts` — 每笔持仓生命周期
    - `recovery_position_facts` — 恢复仓独立记录
    - `strategy_quality_rollups` — 多窗口滚动指标

**关键字段**：
- strategy, account, symbol, side, entry_time, exit_time, holding_minutes
- is_active_trade (bool) — 从OPEN事件分类
- realized_pnl, unrealized_pnl, fee_estimate, margin, leverage
- win_rate, pf, avg_win, avg_loss, payoff_ratio
- hard_stop_count, mfe, mae
- open_reason, close_reason, filter_layer, sentinel_fields
- evidence_window: 1d, 3d, 7d, 14d, 30d

**分类逻辑**：
- 主动策略交易：从A/B/C scanner的 `OPEN` 事件匹配
- 恢复/接管仓位：账户快照中出现但无近期 `OPEN` 事件匹配的持仓

**验收**：
- A/v11, B/v16, C/v14 各自显示主动策略PnL和恢复仓PnL
- 命令中心可直接看到"谁在真赚钱"

---

### Phase 2 - 命令中心决策摘要升级

**目标**：让 `reports/index.html` 回答决策者的第一屏问题。

**运行位置**：阿里云生成 → 反向同步到腾讯云

**实现**：
- [ ] 扩展 `portal_dashboard.py` 读取 `strategy_truth_latest.json`
- [ ] 新增紧凑"策略质量看板"：
  - 主动策略PnL（剔除恢复仓）
  - 恢复仓PnL
  - 当前浮盈亏
  - PF、胜率、盈亏比
  - 预检失败统计
  - 反事实错杀排行
  - 进化门禁优先级
- [ ] 不重新引入用户要求删除的教学/术语/健康表

**验收**：
- 从入口页直接看到：哪个策略贡献主动PnL、哪个账户有浮盈、恢复仓是否扭曲结果、是否有新OPEN_FAILED、策略升级是P0/P1/P2/reject

---

### Phase 3 - A/v11 证据计划

**目标**：决定A/v11应该保留、收窄还是通过replacement-quality扩展。

**运行位置**：阿里云shadow实验

**计划实验**：
- [ ] `A-v11-entry-threshold-15m-115-120`：对比15m阈值105 vs 115 vs 120
- [ ] `A-v11-trailing-pullback-0p8-1p0-atr`：对比pullback 0.6 vs 0.8 vs 1.0 ATR
- [ ] `A-v11-vpb-contribution`：评估VPB量价突破策略的独立贡献
- [ ] `A-v11-replacement-quality-guarded-expand`：继续P2小仓观察

**约束**：
- 同币种禁止叠仓和100USDT保证金纪律保持硬性阻断
- 无3/7/14/30天多窗口正向证据不得全量放开
- replacement-quality在门禁升至P2以上前保持受限

---

### Phase 4 - B/v16 主力Alpha优化

**目标**：在不破坏当前信号优势的前提下提升B/v16盈亏比。

**运行位置**：阿里云shadow实验

**计划实验**：
- [ ] `B-v16-atr-stop-bands`：分档止损（高波1.5ATR/正常2.0/低波2.5）
- [ ] `B-v16-confirmation-continuous-score`：15m确认从二元改为连续评分
- [ ] `B-v16-overheat-score-cap`：测试>85/90分是否因反转风险导致低收益
- [ ] `B-v16-low-score-exception-shadow`：低分开仓仅shadow评估

**约束**：
- 所有比较必须包含手续费/滑点估算
- 低于阈值不能从<30独立样本中晋级

---

### Phase 5 - C/v14 重建或退役

**目标**：停止把C/v14原始信号量当作证据；要么重建为聚焦模型，要么退役。

**运行位置**：阿里云shadow实验

**计划实验**：
- [ ] `C-v14-strict-candidate-compression`：只让通过真实入场门槛的1h候选进入日志
- [ ] `C-v14-two-factor-trend-momentum`：核心因子=趋势+动量，量价/结构改为加分项
- [ ] `C-v14-filter-ablation`：逐个隔离赛道限制/BTC趋势/尾部过滤/15m确认/冷却，测量哪些过滤保护PnL、哪些只压制所有入场

**约束**：
- 使用修正后的入场候选口径，不用旧原始信号计数
- 先做paper/shadow，不因错失大行情的逸事就放宽实盘
- 14/30天窗口后仍无正期望 → 标记为research-only或减少实盘角色

---

### Phase 6 - 哨兵贡献与大行情捕捉

**目标**：将哨兵从扫描列表扩展器转变为可度量的信号质量输入。

**运行位置**：阿里云

**实现**：
- [x] 新增 `部署工具/sentinel_quality_review.py`
- [x] 产出：
  - `runtime/sentinel_quality_latest.json`
  - `reports/sentinel_quality_latest.md`
  - 首版 forward returns / coverage 写入 JSON/Markdown；SQLite表 `sentinel_forward_returns` 暂缓，待完整 replay/fill 引擎落地后再决定是否持久化。

**关键字段**：
- sentinel_reason: gainer/loser/volume_spike/velocity_spike
- rank, 24h_change, velocity, quote_volume, first_seen, repeated_count
- forward_returns: 15m/30m/60m/120m
- strategy_response: opened/filtered/rejected/no_signal
- profitable_after_fee: bool

**实验**：
- [ ] `sentinel-score-bonus-shadow`：测试+5/+10/+15加分，仅限有正向收益证据的哨兵类型
- [x] 首版大行情覆盖审计：对比 `SENTINEL_SIGNAL` 总线大行情与后续策略扫描覆盖，入口页展示覆盖率和未覆盖样例。
- [x] 首版大行情归因审计：把大行情拆成未进入策略扫描、已扫描但无信号、策略拒绝、风控/冷却/仓位拒绝、确认层拒绝、执行/交易所规则拒绝、行情数据拒绝、预过滤拒绝、分析/数据错误等粗分桶。

**约束**：
- 无正向前向收益+足够样本 → 不上线哨兵加分
- 哨兵覆盖度审计：统计过去30天涨幅>20%的币种，检查多少在扫描范围内

**验收**：
- [x] 命令中心显示大行情覆盖率、未覆盖样例、15m/30m/60m/120m 哨兵前向收益。
- [x] 命令中心显示大行情粗归因：未进入策略扫描 / 扫描无信号 / 策略拒绝 / 风控拒绝 / 执行拒绝等。
- [x] 命令中心显示未扫描大行情首版细分：镜像内从未被策略扫描 / 只在较远时间被扫描 / 扫描窗口错过。
- [x] 新增 durable watchlist snapshot 历史采集与同步：`runtime/market_mover_watchlist_history.jsonl` 和 daily shard 会进入阿里云 bounded mirror，入口页显示是否可用。
- [ ] 下一步在 watchlist history 积累后继续拆分 `never_scanned_in_mirror`：未进 watchlist / scanner universe 不支持 / 同步镜像截断，并连接完整 replay/fill outcome。

---

### Phase 7 - 恢复仓管理

**目标**：分离和管理恢复仓，不与策略alpha混淆。

**运行位置**：阿里云

**实现**：
- [x] 在 `strategy_truth_ledger.py` 中添加恢复仓标签
- [x] 新增恢复仓独立审查首版：
  - 持仓年龄、当前PnL、保证金PnL%、风险分层、shadow动作
  - 入口页显示恢复仓审查摘要和明细；当前为只读，不自动平仓
- [x] 补充接管后账户快照路径 MFE/MAE、MFE回撤、first-seen 和 sample count（只读事实层）
- [ ] 补充平仓原因归因、反向信号证据、策略是否会开同样仓位

**候选退出策略（shadow测试）**：
- [x] 时间退出：4h/8h/24h 首版只读计数
- [ ] 接管后浮动止损（当前只有 2% 浮亏近似，不是 MFE/MAE trailing）
- [ ] 仅在原策略给出反向信号时平仓
- [x] 当前实盘退出规则（基线）：报告中明确 no-auto-exit

**约束**：
- 恢复仓永不计入主动策略alpha
- 无shadow证据不部署自动退出规则（朴素时间退出可能截断大赢家）

---

### Phase 8 - 晋级门禁硬化

**目标**：让策略进化门禁严格到可用于真实决策。

**运行位置**：阿里云

**实现**：
- [x] 扩展 `strategy_evolution_gate.py` 首版 Phase 8 门禁硬化审计：
  - 按策略和变更类型的最小样本检查
  - 3/7/14/30天窗口一致性
  - 手续费/滑点调整
  - post-approval paper cost/slippage sensitivity (`0.10%/0.15%/0.25%/0.35%`)
  - 行情状态分层：趋势日/震荡日/高波动日/低流动性尾部币
  - 当前账户风险检查
  - 回滚触发定义
  - 入口页显示 gate-hardening 状态；只读审计，不自动升级/回滚
- [ ] 继续补齐策略/变更类型差异化样本阈值、完整纸面 fill/slippage 仿真（含 bar-by-bar/撮合深度）、跨 regime 稳健性评分

**晋级规则**：
- P0：强多窗口证据 + 足够样本 + 风险可接受 + 明确回滚规则 + 仍需人工审批
- P1：有前景，等待决策者审阅
- P2：仅观察或小仓
- P3：仅研究
- Reject：不晋级，除非重做

**回滚触发条件**：
- [ ] 新版本上线24h内 `OPEN_FAILED > 5` → 自动回滚
- [ ] 新版本上线7天内 PF < 旧版本 PF × 0.8 → 人工审核
- [ ] 新版本硬顶触发率 > 旧版本 × 1.5 → 暂停+人工审核
- [ ] 账户7天总亏损 > 200 USDT → 暂停所有策略改动

**约束**：
- 确认类shadow输出无真实/纸面PnL → 不可达P0/P1
- 每个P0/P1项必须包含：改了什么、为什么更好、什么会出错、如何回滚

---

### Phase 9 - 实盘过渡验证（新增）

**目标**：Testnet验证的策略需要在实盘环境验证后才能全面推广。

**前提**：Phase 1-8 产出的最优策略配置。

**实现**：
- [ ] 实盘小仓（如50 USDT/笔）运行7天
- [ ] 记录Testnet vs 实盘差异：
  - 滑点差异（实盘小币可能0.1-0.3%）
  - 成交率差异
  - PF衰减系数
  - API延迟差异
- [ ] 如果实盘PF < Testnet PF × 0.7，暂停并分析原因

**约束**：
- 实盘API调用weight会增加（更多确认查询），需提前计算预算
- 实盘切换需要额外的环境配置（API key、IP白名单等）

---

## 执行顺序

```
Phase 0   安全基线                    ← 立即
Phase 0.5 双服务器架构迁移            ← 立即（基础设施）
Phase 1   策略真相台账                ← 0.5完成后
Phase 2   命令中心升级                ← 1完成后
Phase 6   哨兵贡献评估                ← 1完成后（与2并行）
Phase 3   A/v11 证据计划              ← 2完成后
Phase 4   B/v16 盈亏比优化            ← 2完成后（与3并行）
Phase 5   C/v14 重建/退役             ← 2完成后（与3/4并行）
Phase 7   恢复仓管理                  ← 1完成后（与3/4/5并行）
Phase 8   晋级门禁硬化                ← 3/4/5完成后
Phase 9   实盘过渡验证                ← 8完成后
```

---

## 停止条件

- 实盘 `OPEN_FAILED` 在Binance预检硬化后重现 → 暂停策略改动，按Binance错误码诊断
- 账户硬顶风险或尺寸违规重现 → 暂停实盘扩展
- SQLite事件新鲜度或账户快照新鲜度失败 → 不从过期报告做当前状态判断
- 实验样本 < 30 → 不升至P2以上
- 腾讯云可用内存 < 500MB 或可用磁盘 < 15GB → 触发紧急归档，暂停新功能
- Binance API weight使用率 > 50% → 暂停新增API调用功能

---

## 关键文件清单

### 需要修改的现有文件
- `部署工具/shadow_sync_from_tencent.py` — 扩展同步内容
- `部署工具/deploy_shadow_aliyun.py` — 新增分析Timer部署
- `部署工具/portal_dashboard.py` — 读取真相台账
- `部署工具/strategy_evolution_gate.py` — 硬化晋级规则
- `PROJECT_STATE.md` — 更新架构描述
- `记忆文档/FUTURE_EXECUTION_PLAN.md` — 更新为本计划
- `记忆文档/MEMORY.md` — 记录架构迁移决策
- `CHANGELOG.md` — 记录所有变更

### 需要新增的文件
- `部署工具/strategy_truth_ledger.py` — Phase 1
- `部署工具/sentinel_quality_review.py` — Phase 6
- `部署工具/sync_aliyun_reports_to_tencent.py` — Phase 0.5
- `部署工具/aliyun_analysis_pipeline.sh` — 阿里云分析流水线

---

## 验证清单

每个Phase完成后：
1. `py_compile` 通过
2. `git_change_guard.py` 通过
3. 阿里云Timer执行成功
4. 反向同步成功
5. 命令中心正确显示新内容
6. 腾讯云服务保持active
7. 内存/存储/API weight在安全线内

## 2026-06-03 progress marker
- [x] P0-B exact replay/live parity audit: added strict serialized `strategy_gate_case(s)` audit, portal section/card/bullets, Aliyun refresh/shadow execution, reverse sync, live-context pull, and unit tests. Current local reset DB has no open-flow rows, so it reports missing exact cases rather than claiming parity.
- [x] P0-B first scanner exact-case persistence: A/B/C `OPEN_SKIPPED` payloads now include serialized `strategy_gate_case` for important account-state, duplicate-position, market-data, sizing/quantity, active-position, tradability, and execution-preflight gates. This prepares strict parity coverage for next staged fresh-run without starting services.
- [x] P0-B second scanner exact-case persistence: B/v16 now adds exact cases for small-live stage guard, 15m confirmation, and entry-threshold rejects; C/v14 now adds exact cases for 15m confirmation and tail-guard rejects. Scalar-like values are normalized before JSON serialization.
- [x] P0-B scan-level exact parity: `replay_live_parity_audit.py` now reads `sentinel_scans` gate rows separately from open-flow events, and A/B/C selected pre-filter/cooldown/score/risk scan rows persist exact cases without mixing scan metrics into open-flow coverage.
- [x] P0-B close-flow exact parity: `replay_live_parity_audit.py` now reports close/replacement/post-open cleanup rows separately, and A/B/C close failure paths persist exact `execution_result` cases without changing live order behavior.
- [x] P0-B B/C open-success chain parity: B/v16 successful opens now persist confirmation -> threshold -> execution exact cases, and C/v14 successful opens now persist confirmation -> tail_guard -> execution exact cases.
- [x] P0-B A/v11 open-success chain parity: A/v11 successful Hanmuxia opens now persist entry/pool/safety/risk/sizing/execution exact chains; VPB opens preserve VPB semantics and carry only shared context; replacement-success opens append release cases.
- [x] P1-B B/v16 full-live rollout review: read-only 24h/72h/168h windows, cost-adjusted PnL, failure/forced-close pressure, decision packet, portal section, Aliyun refresh, reverse sync, and live-context pull. No automatic rollback or parameter change.
- [x] P1-B/P1-D B/v16 rollout attribution: 72h windows now include realized profit/loss, profit factor, exit-model attribution, open-failure reason attribution, and `0.10%/0.15%/0.25%/0.35%` cost sensitivity; portal shows PF/main exit/main open-fail/0.25% cost-stress PnL. No automatic rollback or parameter change.
- [x] P1-B/P1-C B/v16 rollout local replay/fill comparison: rollout review now pairs B/v16 open/close rows, reads local/mirrored research-store Klines or Kline cache, optionally uses depth snapshots for entry fill, and replays SL/TP + `1.0 ATR` activation/pullback through `core.replay_fill`. No Binance API call or live behavior change.
- [x] P1-C A/v11 ATR trailing counterfactual fill: `counterfactual_open_skips.py` now applies A/v11 approved ATR trailing parameters when skipped rows include positive ATR and 15m/30m timeframe; non-A/B/C rows without usable A/v11 ATR keep fixed percent barrier fallback.
- [x] P1-A/P1-C/P1-D A/v11 rollout-review exit model and cost sensitivity: real close rows are grouped by exit model and 72h decision packets include cost sensitivity at `0.10%/0.15%/0.25%`. No automatic rollback or parameter change.
- [x] P1-D evolution-gate paper cost sensitivity: `strategy_evolution_gate.py` post-approval quality and decision packets now include `0.10%/0.15%/0.25%/0.35%` report-only cost/slippage sensitivity; conservative `0.25%` can flag mature windows for manual rollback review. No automatic rollback or parameter change.
- [x] P1-D profile-aware gate thresholds and regime robustness: `strategy_evolution_gate.py` now exposes strategy/change-family `gate_profile` thresholds, uses profile-specific P0/P1 and post-approval closed-sample requirements, and adds report-only cross-regime robustness scoring/audit gaps. No automatic rollout or rollback.
- [x] P1-D rollback governance readiness: `rollback_watch_review.py` now marks each P0/P1 rollback-watch item as `operator_ready`, `waiting_for_replay_readiness`, `waiting_for_mature_evidence`, `packet_gap`, or `monitoring_only`, based on decision-packet completeness, rollback path, maturity, close-failure pressure, and replay readiness. No automatic rollback or live behavior change.
- [x] P1-D rollback execution dry-run plan: `rollback_execution_plan.py` turns operator-ready rollback-watch items into report-only dry-run checklist/report with release-manager command hints, disabled apply commands, freeze/evidence/rollback-path/post-check/abort criteria, portal visibility, and Aliyun/release/sync/live-context wiring. No automatic rollback, deploy, service restart, or live behavior change.
- [x] P1-D rollback automation guard: `rollback_automation_guard.py` audits explicit policy approval, replay readiness, actionable dry-run plans, reviewed release ids, and apply-disabled enforcement. It writes JSON/Markdown/portal output and always keeps `automatic_rollback_allowed=false` plus `apply_enabled=false`; even all preconditions met only means `preconditions_met_report_only`.
- [x] P1-C A/v11 rollout local-cache replay/fill comparison: rollout review now pairs A/v11 open/close rows and replays the approved ATR trailing rule from local Kline cache only; missing cache is reported as data gap, no Binance API call.
- [x] P1-C/P2-B recovery local-cache bar replay evidence: truth ledger now replays recovery positions from first-seen snapshot to latest snapshot through `core.replay_fill` using only local/mirrored Kline cache, exposing exit-review/hold-bias/data-gap actions in Markdown and portal. No automatic exit.
- [x] P1-E Kline 30-day acceptance status: research-store query now reports per-interval coverage days plus `kline_acceptance` for key intervals `15m/30m/1h`; portal shows `ok/coverage_gap/no_klines`, missing intervals, and short intervals. No Binance API call.
- [x] P1-C conservative depth fill assumptions: `core.replay_fill` and OPEN_SKIPPED counterfactual reports can cap consumed order-book levels, discount visible liquidity, and show OB available quantity/fill ratio in Markdown/JSON/portal. No Binance API call or live order behavior change.
- [x] P1-C/P1-D OPEN_SKIPPED fill stress grid: counterfactual reports derive `base/conservative/stress` scenarios from completed fill payloads, showing fill-ratio caps, extra entry impact, filled/unfilled quantity, partial count, net PnL, and delta versus base in Markdown/portal. No Binance API call or live behavior change.
- [x] P1-C/P1-E queued depth snapshot planner: `research_depth_backfill.py` defaults to plan-only, can submit `/fapi/v1/depth` intents to central queue in staged runs, and can ingest DONE responses into `research_store/depth_snapshots` / `runtime/depth_cache`; research-store query and portal show depth coverage. No Binance API call in offline pass.
- [x] P1-C queue-ahead / market-impact replay assumptions: `core.replay_fill` and OPEN_SKIPPED counterfactual reports can subtract visible queue-ahead quantity before our fill and add side-aware entry market impact bps. Portal shows impact and queue-ahead fields. No Binance API call or live order behavior change.
- [x] P1-C/P1-E rollout/recovery replay research-store Kline source: `core.replay_kline_source` loads local/mirrored `research_store/klines` partitions first, then A/v11 rollout replay and recovery-position replay fall back to `runtime/kline_cache`. No Binance API call; long-window replay now benefits from staged Kline backfill once ingested.
- [x] P1-C rollout/recovery local depth-cache entry fills: A/v11 rollout replay and recovery-position replay can consume optional local/mirrored `runtime/depth_cache` snapshots through `core.replay_depth_cache`, reporting order-book fill count/source/age and depth slippage. Missing depth keeps synthetic entry; no Binance API call or live order behavior change.
- [x] P1-B/P1-C B/v16 protective-exit replay parity: `core.replay_fill` now has optional close-price hard-bottom and profit-retrace exits, and B/v16 rollout replay enables the live `10%` leveraged hard-bottom plus `30 USDT` / `25%` profit-retrace guard. No Binance API call or live behavior change.
- [x] P1-C/P1-E replay readiness review: `replay_readiness_review.py` now aggregates existing research-store, A/v11 rollout, B/v16 rollout, and strategy-truth JSON artifacts into one report-only readiness gate. Portal/Aliyun/release/sync/live-context paths show whether Kline/depth coverage, rollout replay, and recovery replay are ready for operator continue/narrow/rollback review. No Binance API call, service start, replay recomputation, approval, rollback, or live behavior change.
- [x] P1-E research-store retention planner: `research_store_retention.py` now scans `research_store` partitions, reports hot/warm/archive-candidate status, and only moves old partitions to ignored `research_store_archive/` when explicitly run with `--apply`. Portal/Aliyun/release/sync/live-context paths include the report. No deletion, Binance API call, or live behavior change.
- [x] P1-E research-store compaction planner: `research_store_compaction.py` now reports large/many-row/many-row-group/format-conversion candidates and only rewrites a partition after backing it up when explicitly run with `--apply`. Portal/Aliyun/release/sync/live-context paths include the report. No deletion, Binance API call, or live behavior change.
- [ ] Remaining P0-B: final staged fresh-run must generate new post-instrumentation rows to prove exact coverage/pass rate; old rows without exact cases remain coverage gaps, not accepted parity.
- [ ] Remaining P1-C/P1-E: true staged Kline backfill to 30+ day acceptance, staged depth sampling coverage, post-ingest replay readiness moving from `data_gap`/`waiting_for_samples` to `ready_for_operator_review`, post-ingest rollout/recovery replay review on real long-history/depth partitions, optional compaction apply on real large partitions followed by query/readiness verification, and a real queue-priority/market-impact engine beyond current static assumptions.
- [ ] Remaining P1-B/P1-D: wait for enough post-refresh samples, then review full paper fill/slippage simulation on real long-history/depth partitions, mature PF/cost/failure evidence, and an explicitly approved automatic rollback procedure before any automation can run. Dry-run execution checklist and automation guard now exist, but apply/automation remain disabled.
