# AutoTrading 双服务器架构优化 + 长期执行计划

## 2026-05-31 下一阶段目标：从调参系统升级为策略进化系统

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
- [ ] A/B/C 抽出纯策略函数和门控函数，实盘 scanner 只负责编排和下单。
- [ ] replay 引擎从 SQLite/Parquet 读取历史事件、K线、哨兵上下文，重放 OPEN_SKIPPED/OPEN/CLOSE。
- [x] `counterfactual_open_skips.py` 已先把 OPEN_SKIPPED 归一到 `core.replay.ReplayEvent` / `ReplayDecision`，反事实过滤层分组优先使用统一 replay gate。
- [ ] 反事实评估的成交/出场仿真仍需继续迁到完整 replay 引擎，不再各脚本单独复刻过滤逻辑。
- [ ] 每个实盘 OPEN_SKIPPED 必须能回答：如果放行，按同一出场规则会怎样。

验收：
- 给定同一时间、同一币种、同一上下文，replay 与 live 对入场/否决结论一致。
- C/v14 的“信号多、开仓少”可被分解到明确门控层。

### 新增阶段 N3 - Parquet/DuckDB 研究仓

**目标**：SQLite 保留在线当前态和关注项，研究查询迁移到按日 Parquet + DuckDB，提升长期回测和复盘速度。

关键技术节点：
- [x] 新增 `research_store/` ignored 数据目录，先按日导出 `events`、`sentinel_scans`、`account_snapshots`。
- [x] 新增导出脚本：`部署工具/research_store_export.py` 可从 SQLite 只读导出 partitioned Parquet/JSONL，并写 manifest。
- [x] 新增 DuckDB 查询脚本起点：`部署工具/research_store_query.py` 可从 exported research_store 生成策略漏斗、OPEN_SKIPPED gate、哨兵贡献和最新账户概览。
- [x] `portal_dashboard.py` 已读取 `runtime/research_store_summary_latest.json`，在入口页功能状态、详情入口和样本漏斗区展示研究仓摘要。
- [ ] 数据维护 timer 只保留近期 SQLite 明细，长期研究读 Parquet。
- [ ] 后续补 `klines/features` 研究数据集和 watermark 增量导出，避免重复扫描大表。

验收：
- 30 天策略漏斗查询在秒级完成。
- SQLite 增长不再影响日报和入口页生成。

### 新增阶段 N4 - 进化门禁升级为灰度/回滚系统

**目标**：系统发现更优方案后，不只提醒，还能进入可审计的灰度、观察、回滚流程。

关键技术节点：
- [ ] 门禁增加 regime 分层：趋势、震荡、高波动、低流动性。
- [ ] 加入费用/滑点/未成交模拟。
- [ ] 每个候选必须有 24h/72h/7d 观察窗口和最低样本数。
- [ ] 放开后自动生成 rollback watch：PF 衰减、硬止损率升高、OPEN_FAILED、尺寸异常、关闭确认失败。
- [ ] 首页最高优先级展示“已验证更优方案”或“已放开候选正在劣化”。

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
- [ ] 新增 `部署工具/sentinel_quality_review.py`
- [ ] 产出：
  - `runtime/sentinel_quality_latest.json`
  - `reports/sentinel_quality_latest.md`
  - SQLite表：`sentinel_forward_returns`

**关键字段**：
- sentinel_reason: gainer/loser/volume_spike/velocity_spike
- rank, 24h_change, velocity, quote_volume, first_seen, repeated_count
- forward_returns: 15m/30m/60m/120m
- strategy_response: opened/filtered/rejected/no_signal
- profitable_after_fee: bool

**实验**：
- [ ] `sentinel-score-bonus-shadow`：测试+5/+10/+15加分，仅限有正向收益证据的哨兵类型

**约束**：
- 无正向前向收益+足够样本 → 不上线哨兵加分
- 哨兵覆盖度审计：统计过去30天涨幅>20%的币种，检查多少在扫描范围内

**验收**：
- 命令中心显示哪些大行情被错失及原因：未扫描/扫描无信号/策略拒绝/风控拒绝/执行拒绝

---

### Phase 7 - 恢复仓管理

**目标**：分离和管理恢复仓，不与策略alpha混淆。

**运行位置**：阿里云

**实现**：
- [ ] 在 `strategy_truth_ledger.py` 中添加恢复仓标签
- [ ] 新增恢复仓独立审查：
  - 持仓年龄、当前PnL、接管后MFE/MAE
  - 平仓原因、策略是否会开同样仓位

**候选退出策略（shadow测试）**：
- [ ] 时间退出：4h/8h/24h
- [ ] 接管后浮动止损
- [ ] 仅在原策略给出反向信号时平仓
- [ ] 当前实盘退出规则（基线）

**约束**：
- 恢复仓永不计入主动策略alpha
- 无shadow证据不部署自动退出规则（朴素时间退出可能截断大赢家）

---

### Phase 8 - 晋级门禁硬化

**目标**：让策略进化门禁严格到可用于真实决策。

**运行位置**：阿里云

**实现**：
- [ ] 扩展 `strategy_evolution_gate.py`：
  - 按策略和变更类型的最小样本检查
  - 3/7/14/30天窗口一致性
  - 手续费/滑点调整
  - 行情状态分层：趋势日/震荡日/高波动日/低流动性尾部币
  - 当前账户风险检查
  - 回滚触发定义

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
