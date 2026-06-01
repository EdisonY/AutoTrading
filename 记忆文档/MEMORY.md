# MEMORY.md - 长期记忆

## 2026-05-31 下一阶段优化目标与 report 决策化
- 用户要求把整体优化建议作为下一目标记录并开始推进，同时重新梳理 report：决策者需要看到的是经过筛选和思考后的简洁结论，核心关心盈亏、策略是否进化、系统是否有风险、是否有可执行优化。
- 已确立下一阶段目标：从“调阈值系统”升级为“策略进化系统”。优先级为：N1 决策者入口页重构，N2 统一 replay/live 同路径，N3 Parquet/DuckDB 研究仓，N4 灰度/回滚化进化门禁，N5 NautilusTrader/Qlib/Freqtrade/vectorbt 小型 PoC。
- 入场门槛决策：不继续全局盲目放宽。A/v11 稳态监控，不再放宽；B/v16 已全量开放两个已验证候选，先观察；C/v14 已进入受控扩样窗口，先看样本和 PF，不立刻二次放宽。后续如果要更多样本，必须绑定观察窗口、样本门槛和自动回滚条件。
- report 设计原则：第一屏必须先给结论和下一步动作；详情表、日报、反事实、哨兵、研究审阅台只作为下钻。入口页应把三策略分别标成“稳态监控/观察全量候选/受控扩样/暂停扩展/需要回滚”等可决策状态。
- N3 也已起步：新增 `部署工具/research_store_export.py`，可把 SQLite `events`、`sentinel_scans`、`account_snapshots` 按日导出到 Git 忽略的 `research_store/`，支持 Parquet/JSONL，并写 `manifest_latest.json`。这只是离线研究仓基础，不影响实盘。
- N2/N3 继续推进：新增 `core/replay.py` 作为统一 replay/live 事件归一模型；新增 `部署工具/research_store_query.py` 作为 DuckDB 查询入口，面向策略漏斗、OPEN_SKIPPED gate、哨兵贡献和账户概览。本机当前缺少 `duckdb/pyarrow` 包，已写入 `requirements.txt`；脚本已通过 py_compile，完整查询需在安装依赖后运行。
- N2/N3 又推进一步：`counterfactual_open_skips.py` 已开始通过 `core.replay` 归一 OPEN_SKIPPED 并把 `replay_gate` 写入反事实 JSON/DB payload；同时它已修正为读取去重后的 `*/events` 权威来源，不再只看旧 `*/decisions` 镜像。`portal_dashboard.py` 已把研究仓摘要接入入口页，用于快速看样本漏斗、主要否决层和 DuckDB/Parquet 是否新鲜。剩余重点是把 A/B/C 实盘门控抽成纯函数、让 replay 与 live 真正同路径。
- 线上验证发现并处理：阿里云分析镜像一度停在 2026-05-29，`shadow_sync_from_tencent.py` 全量同步会超时。已用 SQLite slim/tiny 快照 + SSH/base64 有界传输恢复本轮报告链路；2026-05-31 12:22 CST 入口页已显示新鲜反事实：24h OPEN_SKIPPED `474`、完成样本 `464`、60m 模拟 PnL `+905.10 USDT`，并显示研究仓样本漏斗。后续必须把常规 Tencent→Aliyun 同步改成稳定的 slim/增量流式方案。
- 常规同步方案已落地：`shadow_sync_from_tencent.py` 现在默认只同步 3 日 bounded SQLite mirror，写入项目内 `server_logs_tencent`，并强制 quick_check/新鲜度校验；`sync_aliyun_reports_to_tencent.py` 现在优先同步 index、counterfactual、research-store、runtime 关键文件，默认短超时/重试/错误上限，市场日报等大文件改为可选。2026-05-31 18:03 CST live pull：六服务 active，账户浮盈约 `+19.15 USDT`，13 持仓，attention `P0=0/P1=0/P2=5`；入口页显示 24h 反事实完成样本 `506`，60m 模拟 PnL `-0.79 USDT`。
- N4 灰度/回滚化进化门禁开始落地：`strategy_evolution_gate.py` 会读取 full-live 手工批准记录，已全量放开的候选不再从视野里消失，而是进入 `full_live_monitoring`。如果出现尺寸违规、硬顶风险、策略账户大浮亏、影子/扣费后表现弱于原版、硬顶触发增加或 OPEN_FAILED 压力，会升级成 `rollback_watch`/`rollback_required`；`decision_attention.py` 会把这类项作为“策略回滚”重新推到入口页。自动代码回滚、24h/72h/7d 实盘窗口和 regime 分层仍是后续 N4 工作。
- 跨云同步继续收敛：2026-05-31 19:46 CST 验证发现 3 日同步在当前链路上仍可能超过 180s，而 1 日 bounded sync（5000 条哨兵、500 条账户快照）能稳定完成且数据新鲜约 0.04h。因此小时级入口刷新应走 1 日小镜像；每日 shadow review 可以先试 3 日，失败后自动降级 1 日，避免整条报告链卡死。
- N4 又补一层：`strategy_evolution_gate.py` 现在会从事件库为 full-live 候选生成 post-approval 24h/72h/168h 实盘观察窗，统计开仓、平仓、强平、OPEN_FAILED、CLOSE_FAILED、OPEN_SKIPPED 和已实现 PnL。关闭确认失败会直接触发 `rollback_required/P0`，24h OPEN_FAILED 达阈值会触发 `rollback_watch/P1`。后续还要补 regime 分层、窗口收益质量阈值和自动回滚执行。
- N4 regime 第一版已接入：post-approval 实盘窗口会按平均绝对涨跌、速度、成交额、方向偏斜和强平密度标成 `high_volatility` / `trend` / `low_liquidity` / `range`；入口页 executive summary 会直接显示已放开候选 24h regime 分布、OPEN_FAILED 和 CLOSE_FAILED，方便判断候选表现是策略问题还是行情环境问题。
- N4 窗口质量阈值第一版已接入：post-approval 实盘窗口会估算 0.15% 费用/滑点后的 PnL，并计算强平率、开仓失败率和样本成熟度。关闭确认失败直接 P0；样本足够后扣费后亏损过大、强平率过高或开仓失败率过高会升 `rollback_watch/P1`。入口页会显示已放开候选 24h `quality` 分布。
- N3 研究仓增量导出已推进：`research_store_export.py` manifest 现在记录每个 table/date 分区的 `rows/max_ts/path/status`。下一次导出会先比较 watermark，未变化分区直接 `skipped_unchanged`，减少重复读写 Parquet/JSONL；如需重建可用 `--force`。
- N3/N4 继续收口：`data_maintenance.py` retention 模式会清理独立 `sentinel_scans` 旧分区，避免哨兵扫描表长期撑大 SQLite；入口页第一屏新增“进化最高优先级”提示条，顺序为回滚/劣化 > 已验证更优方案 > 已放开候选观察质量。`portal_dashboard.py` 账户快照现在只在 JSON 新鲜时优先使用，过期 JSON 会退回 SQLite 或明确标记过期，避免阿里云手动重建入口页时误显示旧账户盈亏。
- N4 最低实盘样本数已补：已放开候选的 post-approval 24h/72h/168h 窗口分别要求 closed samples `20/50/100`；不足时 quality 为 `maturing`，并在 full_live_monitoring blocker 中显示，不会被误判为足够成熟。
- N3 `klines/features` 研究数据集第一版已接入：腾讯同步会带上最新 `runtime/kline_cache`，阿里云 `research_kline_features.py` 会把缓存 K线导出为 `research_store/klines`，并生成 `features`（1/3/10 bar return、body、range、quote_volume 等），研究仓摘要和入口页展示 K线/特征覆盖度。它是近期缓存覆盖，不是完整历史 K线仓，后续还要补长历史归档。

## 2026-05-31 B/v16 全量放开与 C/v14 扩样
- 用户明确要求“放开”，核心原因是当前样本量太少，无法有效优化策略；同时要求解决 C/v14 开仓少，并保证 A/v11 无异常。
- B/v16 两个 P0/P1 进化候选已获用户全量审批并落地为实盘规则：`EXP-20260527-v16-atr-stop-bands` 使用 ATR/Price 分档止损（低波动 2.5×ATR、常规 2.0×ATR、高波动 1.5×ATR），`EXP-20260527-v16-overheat-cap-85` 把过热封顶降到 85。B/v16 不提高单仓保证金、不取消同币种禁止叠仓、不取消余额保护。
- C/v14 进入 `v14_sample_expansion_2026_05_31` 扩样模式：每周期/赛道上限 2→3，1h 入场门槛 55→50，15m 确认门槛 35→25，空头额外惩罚 15→10，高分 1h 候选允许弱确认或无确认小范围放行。风控闭环保持：同币种禁止重复开仓、总仓位/方向/余额保护、交易所规则预检、开仓确认、强平/关闭必须确认仓位消失。
- A/v11 本次不放松入场和替换规则，只作为异常监控对象：继续看 100 USDT 保证金纪律、同币种禁止叠仓、硬底止损和 replacement-quality guarded small-live 表现。
- 已部署并验证：Tencent `strategy-b` release `20260531-020144-strategy-b-3a5f427`、`strategy-c` release `20260531-020214-strategy-c-3a5f427`、`research` release `20260531-020241-research-3a5f427`、`portal` release `20260531-020348-portal-3a5f427`，Aliyun `shadow` release `20260531-020337-shadow-3a5f427`。2026-05-31 02:04 CST live context：六个预期服务 active，system alerts `ok/0`，attention `P0=0/P1=0/P2=5`，A/v11 当前 4 仓保证金约 99.6-101.0 USDT，无 sizing abnormality。
- 后续复盘重点：B/v16 OPEN/CLOSE 样本量、硬底止损率、真实 PnL 与 close-confirm 失败；C/v14 入场候选→开仓转换率、OPEN_SKIPPED 分层、样本扩张后的胜率/PF；A/v11 sizing violation 必须保持 0。

## 2026-05-31 运行复盘与事件库去重
- 2026-05-31 01:41 CST live context：Tencent 六个预期核心服务全部 active，system alerts `ok/0`，三账户当前浮亏约 `-4.71 USDT`，持仓 `7`。A/v11 4 仓、B/v16 2 仓、C/v14 1 仓；A/B/C 当前均无 sizing violation，硬止损风险计数为 0。
- 当前 P0=2 不是服务器故障，而是 B/v16 策略进化候选：`EXP-20260527-v16-atr-stop-bands` 与 `EXP-20260527-v16-overheat-cap-85`，状态 `verified_upgrade_ready`，仍需明确审批后才可扩展。
- 发现并修复 SQLite 事件库重复计数：A/B/C scanner 过去把同一 OPEN/CLOSE/OPEN_FAILED/OPEN_SKIPPED/SIGNAL 同时从 raw event/signal 和 decision source 写入 `events`，B/v16 还把哨兵逐币扫描写入 `events`。已改为 JSONL 决策日志保留，SQLite 只保留权威事件源；B/v16 哨兵扫描写入 `sentinel_scans`。
- 已在 Tencent 一次性清理 `events` 历史镜像：`decision_mirror_deleted=205450`，`sentinel_event_mirror_deleted=24665`，`quick_check ok`。2026-05-30 至 2026-05-31 01:40 去重后口径：A/v11 OPEN 93 / CLOSE 86 / OPEN_FAILED 3 / OPEN_SKIPPED 77；B/v16 OPEN 55 / CLOSE 53 / FORCED_CLOSE 7 / OPEN_FAILED 2 / OPEN_SKIPPED 77；C/v14 OPEN 1 / OPEN_SKIPPED 44。
- 已把 `crypto-data-maintenance.service` 的持久化命令改为每小时运行 `--purge-event-store-mirrors`，并把 service/timer 文件纳入 Git，后续 agent 不应再用未去重的 `events` 原始计数直接复盘。
- 报告入口已重建：`reports/index.html` 不再包含 `no such table` 或 `无实时账户快照`。注意 2026-05-31 01:xx 复盘早于 2026-05-30 日报正常生成窗口，当前 `market_review_latest` 仍是 2026-05-29 全日文件，不能把它当作 2026-05-30 全日复盘。

## 2026-05-30 运行续检、开仓尺寸确认与关注台账口径修复
- 用户要求继续未完成的整体运行检查。最终 live context（2026-05-30 09:13 CST）显示 Tencent 六个预期服务全部 active，system alerts `ok/0`，账户浮盈约 `+16.47 USDT`，持仓 `6`，attention `P0=0/P1=2/P2=3`。
- 已完成开仓后真实数量确认：`ExecutionEngine.open_position()` 在请求确认时用交易所持仓数量覆盖订单回报数量；B/v16、C/v14 本地持仓 size 用成交/确认数量；C/v14 开仓也启用 position confirmation。
- A/v11 增加 post-open sizing guard：如果确认后的初始保证金偏离 100 USDT 目标区间且不是 minNotional 例外，会立刻尝试关闭新仓并记录 `OPEN_SIZING_MISMATCH_CLOSED` 或 `OPEN_SIZING_MISMATCH_FAILED`。系统告警和清理脚本已保护/展示这些事件。
- Aliyun `shadow_sync_from_tencent.py` 已修复：镜像路径回到项目根 `server_logs_tencent`，同步 compact runtime/report 文件，默认不再拉 legacy JSONL，生成瘦身 SQLite 快照并 quick_check，SSH stream 失败时 scp fallback；同时修掉旧 Python 对 f-string backslash 的 SyntaxError。
- `decision_attention.py` 的 summary counts 现在只统计 `status=open` 的事项，`cleared_pending_review`、`archived`、`resolved` 不再把历史 P0 算进当前 P0。Tencent 远端重建后显示 open P0=0。
- 未做：没有批准或部署任何 B/v16 P1 策略候选；C/v14 仍有 P2 stale-open 关注项，应继续看过滤层/反事实，不要直接放宽阈值。

## 2026-05-27 双服务器架构优化 + 长期计划扩展
- 用户明确要求：策略间资金无法动态分配，放弃此项；结合服务器内存和存储容量对新增功能有限制；币安API调用不能被ban。
- 阿里云服务器（39.105.156.210）资源闲置，但无法调用币安实盘API（被墙）。决定将阿里云作为分析节点，承担所有不需要API的离线计算任务。
- 长期计划已重写为双服务器架构版本：腾讯云只保留6个API依赖的常驻服务，阿里云承担真相台账、命令中心、复盘、反事实、进化门禁、哨兵评估等全部分析任务。
- 资源约束已写入计划：新增进程RSS<50MB、存储红线15GB、API weight<600/min（50%安全线）。
- 新增Phase 0.5（架构迁移）、Phase 9（实盘过渡验证）、VPB评估实验、哨兵覆盖度审计、回滚触发条件。
- 执行顺序：0→0.5→1→2→6→3/4/5→7→8→9。
- Phase 0.5 已完成：腾讯精简为7服务+1timer，阿里云2个timer（每日全量+每2小时轻量），反向同步已上线。
- Phase 1 已完成：`strategy_truth_ledger.py` 分离主动策略PnL与恢复仓PnL。本地测试结果：A/v11净亏PF=0.01，B/v16净赚PF=5.9，C/v14无数据。
- Phase 2 已完成：`portal_dashboard.py` 新增"策略质量看板"，直接展示各策略主动PnL、恢复仓PnL、PF、胜率、盈亏比。
- Phase 6 已完成：`sentinel_quality_review.py` 评估哨兵贡献。本地测试：17,982哨兵决策，开仓率0.1%，过滤率68%。涨幅榜10,097次扫描仅8次开仓。前向收益计算待补充。
- Phase 3/4/5 已完成：新增10个影子实验配置到 `core/experiment.py`。A/v11：15m阈值115/120、浮动止损回撤0.8/1.0。B/v16：分档止损、过热封顶85。C/v14：候选压缩long65/70、过滤消融（赛道/趋势）。实验总数13个。
- Phase 7 已完成：恢复仓管理。真相台账增加恢复仓详细审查和5种退出策略shadow测试（4h/8h/24h时间退出、2%回撤、反向信号）。
- Phase 8 已完成：晋级门禁硬化。手续费调整0.15%、P0需≥50样本+调整后正PnL、P1需≥30样本、回滚触发条件（PF衰减<0.8x、硬顶率>1.5x）。
- Phase 9 已完成：实盘过渡验证清单。当前结果：A/v11 2/6、B/v16 4/6、C/v14 2/6。无策略满足全部条件，实盘过渡需用户明确审批。

## 2026-05-27 Polymarket 下线 + 内存/C-v14 复核
- 用户明确要求删除所有 Polymarket 相关文件代码并停掉相关服务。已停止/禁用/移除腾讯 `polymarket-monitor.service`，删除 `/opt/polymarket-lab`，并删除本地 `polymarket_lab` 代码与两套部署脚本。
- 总入口、系统告警、实时上下文拉取、持久关注台账都已移除 Polymarket 当前模块。历史记忆保留曾经做过的 Polymarket 研究结论，但后续 agent 不应把它当成当前活跃系统，除非用户明确重新开启。
- 下线后服务器内存复核：MemAvailable 约 1.4GiB，SwapTotal 2GiB、SwapUsed 约 210MiB，近 12 小时 kernel journal 未见新的 OOM。常驻交易服务的单进程 RSS 约 40-49MB，内存压力不来自三策略本身。
- C/v14 现状：昨日 2026-05-26 复盘显示 `入场候选 124 / 原始信号 38930 / 开仓 3 / 跳过 101`。这说明旧“信号太多”主要是原始分析噪声，但真实入场候选到开仓约 2.4%，仍偏保守；主要卡点仍是策略层 `can_trade=False`、分数阈值、15m 确认和同币种/仓位风控。此次未直接放宽实盘条件。

## 2026-05-27 A/v11 替换策略小范围观察 + P级事项数据库化
- 用户确认：继续推进 `A/v11 EXP-20260523-v11-replacement-quality`，但只按“扩展审核/小范围增强”走，不全量放开；其它 P0 全部归档。
- 已把 A/v11 满仓替换参数收窄到实验验证口径：`STRONG_SIGNAL_THRESHOLD=112`、`EVICT_SCORE_GAP=25`、盈利 `>=2%` 仓位硬保护、策略版本 `small_live_v11_replacement_quality_v1`。不增加总仓位、不取消同币种禁止叠仓、不降低入场阈值。
- 已写入人工审批记录：`research_memory/approvals/approve_small_live_A_v11_replacement_quality_2026-05-27.json`，scope 为 `shadow_plus_small_live_guarded`。
- 已修复研究审批识别：`apply_research_approval.py` 和 `experiment_runner.py` 现在支持按 `candidate_id`、`experiment_id`、`family_id` 三种键读取人工动作，固定实验也能被门禁识别。
- P级关注项不再以 JSON 为主存储。`decision_attention.py` 现在将 `attention_items` 与 `attention_acknowledgements` 写入 `runtime/event_store.sqlite3`；`research_memory/attention/open_items.json` 只是入口页/跨机同步缓存。
- 新增 `部署工具/acknowledge_attention_items.py`：可把用户确认归档写入 SQLite 和导出 JSON。服务器已归档 `入口页刷新失败`、`总入口页面偏旧`、`近期发生 OOM` 三个非 A/v11 P0。
- 线上验证：2026-05-27 01:14 服务器统一门禁跑通，`P0=0`，A/v11 变成 P2 `small_live_monitoring / keep_small_live_monitoring`；A/v11 replacement-quality 最新正式窗口增量约 `+844.0191`。所有核心服务仍 active。
- 后续观察重点：A/v11 的 `EVICT_CLOSE`、`OPEN_RETRY_AFTER_EVICT`、真实 PnL、硬顶风险、错失盈利；未满观察窗口前不要继续扩大战略替换权限。

## 2026-05-17 ~ 2026-05-24 历史里程碑摘要（已精简）

以下为精简后保留的关键结论，详细过程见各日期 CHANGELOG.md 条目。

### 策略版本演进（截至 2026-05-24）
- v13 已下线清理，v16 接管 Account B。v12 已停用。
- A/v11：半木夏 MACD+SuperTrend，满仓替换机制（STRONG_SIGNAL_THRESHOLD=112），同币种禁止叠仓，100 USDT 保证金纪律。
- B/v16：CVD/OFI 订单流策略，实盘 aggTrades 数据，硬底止损 10%，小仓阶段保护已临时关闭。
- C/v14：四维度评分（趋势/动量/量价/结构），分档 ATR 止损，BTC 趋势过滤，同赛道限制 2 仓。
- 三策略统一：4x 杠杆，同币种禁止叠仓，可用保证金保护 max(300USDT, 25%权益)。

### 架构升级（2026-05-24 ~ 2026-05-25）
- SQLite 事件库 `event_store.sqlite3` 已上线，三策略 JSONL+SQLite 双写。
- 统一行情缓存 `market_data_cache.json` 已上线（每 15 秒刷新）。
- 账户快照服务已上线（每 30 秒采集三账户）。
- 哨兵事件总线已上线，`sentinel_scanner.py` 为公共模块。
- 数据维护 timer 已上线（每小时归档/清理）。
- 入口页已从 JSONL 全量扫描切到 SQLite 聚合优先。

### 研究系统（2026-05-23）
- 研究经验库已上线：cases/hypotheses/lessons/snapshots 目录。
- 影子实验闭环：候选假设 → 影子实验 → 晋级门禁 → 人工审批。
- 研究审阅台 + 人工审批台已上线。
- 阿里云影子机已同步全套闭环脚本。

### Polymarket（2026-05-24 上线，2026-05-27 下线）
- 曾作为独立只读研究系统运行，连续监控 216 轮无套利机会。
- 2026-05-27 按用户要求下线，代码和服务已清理。

### 已修复的关键问题
- 双向持仓 key bug（2026-05-08）：(symbol, side) 复合 key
- v16 精度修复（2026-05-13）：ExchangeInfoCache 按 stepSize 取整
- A/v11 保证金尺寸修复（2026-05-25）：按目标保证金 100USDT 计算数量
- A/v11 同币种叠仓门控（2026-05-25）：禁止同币种重复开仓
- C/v14 信号口径修正（2026-05-26）：只记 1h 入场候选为 SIGNAL

### 2026-05-28 运维修复
- event_store.sqlite3 从 1.7GB 清理到 85MB（删除 SENTINEL_SCANNED/SIGNAL/EVENT 旧数据，VACUUM）
- 哨兵扫描分离到独立表 `sentinel_scans`（按 date 分区），不再写入 events 表
- 分析流水线从阿里云迁回腾讯本地运行（SQLite 85MB，全流水线 <1s 完成）
- A/v11 Testnet 账户被平台封禁（-1109），用户注册新账户，API key 已更新
- portal_dashboard.py 修复 `{{}}` f-string 语法错误
- 阿里云流水线 timeout 从默认 90s 增到 900s，sync 步骤加 timeout 600
- 关注项确认 API 服务部署在阿里云 8090 端口（attention_api_server.py）
- 哨兵 400 修复（2026-05-24）：testnet 不可用币种过滤
- Binance 下单预检硬化（2026-05-27）：OPEN_SKIPPED vs OPEN_FAILED 归因

### 2026-05-29 双服务器报表链路补齐
- 腾讯入口页服务口径已修正：`crypto-portal-refresh.service` 和已迁移的 counterfactual/evolution/market-review timer 不再作为腾讯故障或 P0 关注项出现。
- Aliyun 分析链路已补齐：`shadow_sync_from_tencent.py` 使用 SQLite backup API 生成一致 DB 快照并 quick_check；`sentinel_quality_review.py` 已确认读取 `sentinel_scans`；`counterfactual_open_skips.py` 参数已修到当前 CLI；attention ledger 在 portal 生成前重建；truth/sentinel runtime JSON 和 attention JSON 会反向同步回腾讯。
- 2026-05-29 02:12 CST 最终状态：腾讯 A/B/C、哨兵、账户快照、系统告警均 active；系统告警 0；关注项 P0=0；账户浮盈约 +91.31 USDT，持仓 6。
- 剩余关注：后续继续通过 system alerts/data maintenance 监控 SQLite 增长。2026-05-29 02:16 CST 已复核 Tencent DB 约 212MB、Aliyun mirror 约 215MB，均 quick_check ok 且 freelist_count=0，当前不需要立即 VACUUM 或破坏性清理。

### 用户偏好（持续更新）
- 中文回复，绝对路径，简洁直接
- 绿涨红跌配色（--up:#22c55e, --down:#ef4444）
- Windows/PowerShell 环境，curl 需用 curl.exe
- 不要教学/术语/健康表等解释性内容
- 所有变更必须同步 Git + CHANGELOG.md
- 2026-06-01：当前运行环境已改为无需确认提权；后续操作应直接执行短超时命令，避免弹窗确认导致卡住。新增 replay 特征对齐数据集作为 N2/N3 桥梁，后续继续抽 A/B/C 纯策略门控与完整 replay 引擎。
- 2026-06-01：腾讯账号快照出现 Binance testnet `HTTP 418 / -1003 Way too many requests`，曾导致入口页拉到误导性 0 仓快照。已将账号快照改为 API 错误时不覆盖最新有效快照，并解析 `banned until` 自动退避；告警显示为“账户快照API冷却中”。后续仍需继续做 API budget/websocket 化。
- 2026-06-01：系统告警补强 Binance API 压力来源识别。`system_alerts.py` 会按最近 30 分钟 systemd journal 统计 418/429/-1003/Too-many-requests 并按服务聚合，入口页“自动告警”卡直接显示 API 限流次数和来源，避免只看到账户冷却而不知道是否有 scanner 也在触发限流。
- 2026-06-01：受控扩样可视化继续推进。`strategy_evolution_gate.py` 新增 `expansion_readiness`，把已全量放开的候选按 24h post-approval 窗口汇总成熟/继续收样/暂停复核、样本缺口和每候选动作；`portal_dashboard.py` 第一屏直接显示“扩样成熟度”，用于判断继续收样还是暂停扩张。门禁还补了 approval-only 决策记录，避免某节点缺少实验行时把已批准 full-live 候选从入口页隐藏。

---
## 2026-05-29 全局运行自检与账户方向口径修复
- 触发：用户要求整体检查项目、策略运行、服务器运行并自动修复问题。
- 发现并修复：Aliyun 每日 `crypto-shadow-review.timer` 原脚本只刷新 evolution/portal 的一部分，没有在 portal 前重建 decision attention，导致已验证的 P0 没有及时进入入口关注台账。现已新增并部署完整 `aliyun_shadow_review.sh`：同步腾讯 SQLite -> truth ledger -> sentinel review -> research memory -> signal quality -> experiments -> counterfactual -> evolution gate -> decision attention -> research dashboard -> portal -> 反向同步腾讯。
- 发现并修复：Binance testnet 个别持仓行 `positionSide` 与真实经济方向冲突，例如 C/v14 FIL 原始 `positionSide=LONG` 但 `positionAmt/notional/raw upnl` 显示实际是 SHORT；B/v16 NEAR/GRASS 也有类似冲突。新增 `core/position_utils.py`，统一用“原始浮盈与 entry/mark 匹配”优先推断有效方向，账户快照、日报当前持仓、共享模型、开仓后确认持仓都改用同一口径。
- 当前验证：2026-05-29 02:56 CST 拉取 live context 显示腾讯六个预期服务全部 active，系统告警 0，账户浮盈约 +28.46 USDT，持仓 8，关注项 P0=1/P1=1/P2=10。P0 是策略进化候选提醒，不是服务器故障。
- 部署记录：腾讯 account、A/v11、B/v16、C/v14、portal/system-alerts 已部署；Aliyun shadow 已部署。后续 agent 不要再按原始 `positionSide` 单独解释当前持仓方向，必须通过 `core.position_utils.infer_position_side()`。
---
## 2026-05-29 A/v11 full trailing approval + B/C close confirmation loop
- User approved both A/v11 P0 trailing-pullback candidates for full rollout: `EXP-20260527-v11-trailing-pullback-0p8` and `EXP-20260527-v11-trailing-pullback-1p0`. Because live code can only run one 15m pullback parameter at a time, the selected live value is the wider `15m=1.0 ATR`; `30m` remains `0.8 ATR`. The 0.8 candidate remains durable approved evidence and a rollback option.
- Close/forced-close rule: a strategy must not treat an exchange order response as sufficient proof of closure. It must confirm the matching symbol/side position is gone; if still present, retry with remaining quantity; if still present after retry, write `CLOSE_FAILED` or `FORCED_CLOSE_FAILED`, keep local state, and surface a bad system alert on the command center.
- B/v16 and C/v14 must not directly trust raw Binance testnet `positionSide`; exchange-side count, restored positions, duplicate-position display, hard-stop loss calculation, and close direction must use `core.position_utils.infer_position_side()` / `leveraged_loss_pct()`.
- Aliyun daily shadow-review must run `daily_market_review.py` so `market_review_latest.md/html` does not stay on an old date.
- Final 2026-05-29 noon verification: all six Tencent live services were active and command-center attention P0 was cleared after approved A/v11 trailing candidates were resolved from the attention ledger. System alert remains bad because B/v16 legacy `BCHUSDT` long, `ETHUSDT` long, and `FHEUSDT` short still reject close attempts with Binance Testnet `-4061`; this is an exposed unresolved live-risk item, not a silent local-state issue.
