# MEMORY.md - 长期记忆

## 2026-05-23 研究闭环 P2/P3 继续推进
- 已将经验库候选接入 `F:\AutoTrading\部署工具\experiment_runner.py`：`research_memory/hypotheses/candidates_latest.jsonl` 会自动转成影子实验，不再只跑固定三条。
- 已新增统一追踪字段：`candidate_id`、`source_cases`、`change_type`、`gate_passed`，并写入实验结果与报告。
- 已新增晋级记录落库：`research_memory/promotions/reviews_latest.jsonl`，把 `candidate_id -> experiment_id -> promotion_status` 串起来。
- 已更新 `experiment_report.py`：实验总览和逐实验说明里会显示候选来源与源案例摘要。
- 阿里云影子机已同步这套闭环脚本，定时任务现在同时跑日志同步、研究记忆、信号基线、影子实验和实验报告。
- 已新增静态研究审阅台 `F:\AutoTrading\部署工具\research_review_dashboard.py`，输出 `F:\AutoTrading\reports\research_review_latest.html`，包含候选队列、晋级门禁、证据链、策略知识图谱、策略经验库和快照/回放联动。
- 研究审阅台已补“样本回放条”：每个源案例会展示当日价格区间、入场/离场在区间中的位置、日涨跌/振幅、持仓时间、PnL，以及顺势/反向标签，方便从候选直接追到具体样本证据。
- 市场快照读取路径已补全：优先读 `research_memory/snapshots/market_snapshot_latest.json`，兼容腾讯同步镜像 `server_logs_tencent/reports/market_snapshot_latest.json`、本地 `reports/market_snapshot_latest.json`，并回退到 `research_memory/snapshots/market_moves_*.json`。阿里云仍只消费腾讯同步快照，不直接请求币安。
- `F:\AutoTrading\部署工具\deploy_shadow_aliyun.py` 已同步接入 `research_review_dashboard.py`，阿里云定时跑完影子实验后会自动刷新 `research_review_latest.html`。
- 已新增总入口页 `F:\AutoTrading\部署工具\portal_dashboard.py`，输出 `F:\AutoTrading\reports\portal_latest.html` 和 `F:\AutoTrading\reports\index.html`，统一汇总账号盈亏快照、每日复盘、研究审阅台、影子实验报告、信号质量报告、经验库、候选假设和市场快照。
- `deploy_review_optimizations.py` 与 `deploy_shadow_aliyun.py` 已接入总入口页生成流程，后续腾讯主节点和阿里云影子机都能自动刷新统一入口。
- 总入口页链接已统一改成 `file:///` 绝对文件链接，避免从 `reports/index.html` 打开时相对路径失效。
- 研究闭环继续升级：`core/research_memory.py`、`core/experiment.py`、`experiment_runner.py`、`research_memory_builder.py` 已加入 `family_id` / `generation` / `governance_status` 等实验族字段，`experiments/families_latest.json` 和 `research_memory/promotions/families_latest.json` 会聚合每个实验族的版本、最佳结果和推荐治理动作。
- `research_review_dashboard.py` 已新增“实验族管理”和“人工审批台”，静态页面里可以直接选择批准小仓、继续观察、拒绝候选或归档实验族，并生成标准审批 JSON。
- 已新增 `F:\AutoTrading\部署工具\apply_research_approval.py`，可把页面里复制出来的审批 JSON/JSONL 写入 `research_memory/approvals/`，同时更新候选状态，形成可回写的人工审批闭环。
- `deploy_shadow_aliyun.py` 已同步上传新脚本与新页面，阿里云影子机现在会同时生成 `research_review_latest.html` 和 `portal_latest.html`。
- 已新增全局巡检脚本 `F:\AutoTrading\部署工具\global_status_check.py`，可检查腾讯实盘机三策略服务、dashboard 停用状态、市场复盘 timer、脚本编译、日志心跳、账号快照，以及阿里云影子机 timer、报告产物和脚本编译。
- 2026-05-23 03:48 全局巡检结论：腾讯三策略 `crypto-scanner` / `crypto-scanner-v14` / `crypto-scanner-v16` 均 active/running/enabled；四个 dashboard 服务均 inactive/disabled；`crypto-market-review.timer` active/enabled/persistent，下一次 2026-05-24 02:05；阿里云 `crypto-shadow-review.timer` active/enabled/persistent，下一次 2026-05-24 02:20；腾讯和阿里云磁盘/内存充足，脚本 py_compile 通过。
- 当前仍要继续优化的点：让腾讯侧定时稳定生成并同步更完整的 `market_snapshot_latest.json`，再把审批动作从静态记录升级为更直观的人工确认入口。

## 2026-05-23 研究经验库 P0/P1 落地
- 已新增机器可读研究经验库：`F:\AutoTrading\core\research_memory.py` 与 `F:\AutoTrading\部署工具\research_memory_builder.py`。
- 经验库输出目录：`F:\AutoTrading\research_memory\cases\`、`hypotheses\`、`lessons\`、`snapshots\`。
- 已按 P0 拆分落盘：`cases_YYYY-MM-DD.jsonl`、`loss_cases_*.jsonl`、`missed_cases_*.jsonl`、`reverse_cases_*.jsonl`、`big_move_cases_*.jsonl`。
- 已按 P1 生成基础归因：亏损/反向/错过大行情样本都带 `market_stage`、`attribution`、`decision_category`、`lesson`、`confidence`，并记录入场/离场/K线窗口摘要。
- 已生成候选假设文件：`research_memory/hypotheses/candidates_*.jsonl` 与 `candidates_latest.jsonl`，当前主要候选是反向/硬顶阶段保护实验。
- 已新增聚合层：`lessons/symbol_lessons.jsonl`、`lessons/factor_lessons.jsonl`、`lessons/strategy_lessons.md`、`snapshots/daily_summary_*.json`。
- 已用 `2026-05-22` 样本落地验证，得到 119 个案例和 1 个候选假设。
- 当前结论：经验库与归因层已可用，但还只是“研究台”，未接入自动改码与自动晋级。

## 2026-05-19 AutoTrader 启发的工程化优化
- 已按 AutoTrader 项目的启发完成可落地优化，重点是配置化、统一数据模型、决策漏斗、纸面 broker 和复盘可视化；未直接引入其 GPL 代码，也未改动正在运行策略的开仓主路径。
- 新增 `F:\AutoTrading\config\v11.toml`、`v14.toml`、`v16.toml`，记录三策略核心参数，供复盘/回测/后续参数对比使用；当前 scanner 仍以自身常量为运行源，避免一次性改变实盘运行风险。
- 新增 `F:\AutoTrading\core\models.py`、`strategy_config.py`、`review_analytics.py`、`paper_broker.py`：统一 `SignalRecord` / `DecisionRecord` / `TradeRecord` / `PositionSnapshot`，支持策略决策归因分类与本地纸面撮合。
- 增强 `F:\AutoTrading\部署工具\daily_market_review.py`：每日复盘现在同时生成 Markdown 与 HTML，并加入“大行情决策漏斗”，把未开仓归因为总持仓限制、方向限制、余额保护、确认周期过滤、分数阈值、冷却、下单失败、前置过滤无记录等类别。
- 已部署到腾讯云 `/opt/crypto-auto-trader/core/`、`/opt/crypto-auto-trader/config/`、`/opt/crypto-auto-trader/daily_market_review.py`、`account_snapshot_html.py`；远端 `py_compile` 通过，并成功生成 `/opt/crypto-auto-trader/reports/market_review_2026-05-18.md/html`。
- 本次部署未重启策略服务；验证后 `crypto-scanner` / `crypto-scanner-v14` / `crypto-scanner-v16` 均 active，所有 dashboard 仍 inactive。
- **原生 DecisionRecord（2026-05-19 21:14 部署）**：三个 scanner 已原生同步写入 `logs/decisions.jsonl`、`logs_v14/decisions.jsonl`、`logs_v16/decisions.jsonl`，覆盖 `SIGNAL`、`OPEN`、`OPEN_SKIPPED`、`OPEN_FAILED`、`CLOSE/FORCED_CLOSE`，字段包含 `strategy/symbol/status/category/side/score/raw_score/timeframe/reason/source/raw`。复盘优先读取 `decisions.jsonl`，旧 `events/signals` 作为兼容回退。
- 原生决策日志部署脚本：`F:\AutoTrading\部署工具\deploy_decision_records.py`。远端 `py_compile` 通过并重启三策略成功；验证时三策略 active，dashboard 全部 inactive。远端决策日志初始行数验证：A/v11=8、C/v14=66、B/v16=6；已成功生成 `/opt/crypto-auto-trader/reports/market_review_2026-05-19.md/html`。
- **三层架构成型（2026-05-19 22:00 部署）**：已新增并上线 `core/strategy_engine.py`、`core/risk_engine.py`、`core/execution_engine.py`。三策略保持各自内部算法不变，但信号调用已统一经由 `StrategyEngine`，总持仓/方向/余额准入已统一经由 `RiskEngine`，真实开仓/平仓/硬顶强平已统一经由 `ExecutionEngine`。统一复盘层继续读取 `decisions.jsonl` 并生成 Markdown/HTML。当前三策略 active，dashboard 仍 inactive。
- 当前远端决策日志行数约为：A/v11=64、C/v14=682、B/v16=51；日报 `/opt/crypto-auto-trader/reports/market_review_2026-05-19.md/html` 持续可生成。

## 2026-05-17 v13下线清理 + v16策略优化
- v13 已正式下线并清理：本地删除 `F:\AutoTrading\策略文件\scanner_v13.py`；服务器停止/禁用并移除 `crypto-scanner-v13.service`，删除 `/opt/crypto-auto-trader/scanner_v13.py`、`run_scanner_v13.sh`、`logs_v13/`、`scanner_data_v13/`、`backtest_v13*.py`、`backtest_results/v13v2_*`、`__pycache__/scanner_v13*.pyc`。
- 服务器残留扫描：`/opt/crypto-auto-trader` 下 `*v13*`、`logs_v13`、`scanner_data_v13` 无结果。
- v16 已执行优化并部署：硬底止损返回 `hit=True`；`analyze_symbol(symbol, bar)` 使用真实周期；15m 不再独立开仓，仅作为 1h 同向确认；`SL_MULT=2.0`、`MAX_LOSS_PCT=10.0`、`TRAIL_ACTIVATE=1.0`；盈利保护调整为 `30 USDT` 后回撤 `25%`；OFI/RSI/EMA 权重下调；不新增黑名单，仅对历史差币观察降权 `10` 分。
- Dashboard B 只读取 v16 数据，不再回退 v13/v12；B Dashboard 与三账号 Dashboard 标签已改为 v16。
- 验证通过：本地和服务器 `py_compile` 通过；`crypto-scanner-v16`、`crypto-dashboard-b`、`crypto-dashboard-dual` active；8082 health 正常。

## 2026-05-17 v14 15m 与硬顶尾部优化
- v14 已优化并部署：15m 不再独立开仓，改为 1h 开仓的同向确认；`ENTRY_TIMEFRAMES=["1h"]`，`CONFIRM_TIMEFRAME="15m"`，`CONFIRM_MIN_SCORE=25`，`SCORE_THRESHOLDS={"15m":65,"1h":40}`。
- 硬顶尾部已收紧：`MAX_LOSS_PCT=12.0`；新增 `_enforce_hard_stop_on_exchange()`，每轮直接扫交易所持仓并强制平掉超硬顶亏损仓，避免本地内存未恢复满仓导致漏管。
- 保护参数提前：阶梯止损 `1.8ATR→成本 / 3.0ATR→1.5ATR / 5.0ATR→2.2ATR`；`TRAILING_ACTIVATE={"15m":1.0,"1h":1.2}`；`TRAILING_PULLBACK={"15m":0.8,"1h":1.0}`；盈利保护 `30USDT` 后回撤 `25%`。
- 验证通过：本地和服务器 `py_compile` 通过；`crypto-scanner-v14` active；C/v14 复查无超过新硬顶 12% 的亏损仓。

## 项目：crypto-auto-trader（半木夏量化交易系统）

### 架构
- **主运行服务器** 129.226.151.144 (腾讯云新加坡 Ubuntu 22.04, 用户 ubuntu)，密码: Caosinima00.
- **旧阿里云服务器** 39.105.156.210 (CentOS 8.2, root) 已空闲：所有 `crypto-*` systemd 服务 disabled，当前无 scanner/dashboard 进程。
- 腾讯云 Python 运行环境：`/opt/crypto-auto-trader/.venv/bin/python` / 服务器路径: `/opt/crypto-auto-trader`
- **币安 Binance Testnet** 交易与实时数据（纯币安，已移除所有OKX代码）
- 币安 API 用原生 urllib + HMAC-SHA256 签名（不用 ccxt）
- **止盈止损由 scanner 自身追踪**（Binance Testnet 不支持 STOP_MARKET，报-4120）
- **重启后自动从交易所恢复持仓到内存**（SimPosition，默认ATR=2%价格）
- 三个交易客户端：binance_client.py（账号A）、binance_client_v2.py（账号B）、binance_client_v3.py（账号C），均返回Binance原生格式
- **get_balance格式不统一**：账号A/B返回dict（assets列表），**账号C返回list**（直接遍历），dashboard API已兼容两种格式
- binance_client方法参数：open_long/open_short(symbol, quantity, leverage, tp, sl)，close_position(symbol, pos_side, quantity=0)

### 策略参数
- **双策略：半木夏+VPB量价突破**（v13已禁用VPB，待优化后重新集成）
- 半木夏：MACD(13,34,9) + SuperTrend(ATR=10, 乘数=3.0) 条件加权
- VPB：量能异动(20-40分) + 价格突破(25分) + K线形态(0-20分) + 资金费率(0-15分) + EMA方向(10分) + 量能持续(5分)
- VPB阈值：15m>=60 / 30m>=55，量能上限5x
- VPB止损1.2×ATR / 止盈4.5×ATR
- 杠杆4倍全仓(CROSSED)
- 每笔100 USDT开仓（v13: 动态投入 90-99分=$150, 80-89分=$75）
- 高波动过滤：ATR/Price>5%跳过
- ATR=0异常币种自动黑名单
- MAX_LOSS_PCT=30%强制平仓
- 冷却期120分钟（v13）+15分钟（开仓失败后）
- **A/B对照测试**：v11和v13使用不同策略参数，不互相同步，用于对比不同参数下的表现
- **A/B/C三账号**：v11(v11半木夏)、v13(v13半木夏优化)、v14(四维度新策略) 三种策略并行测试

### 账号A配置（v11策略）
- API Key: GVj9IiAxSFBR3rxIfaYrpY8ejnaYEyrIAAxsDIC96fU9OihvTi2o0HpTdlWDd7nh
- API Secret: zJzHayctHUzUlsGz6ct8yNCHm8wT7RhgP4fFYinAZedP4DPj3WxeTunkwFWeV4bd
- Scanner: scanner.py（v11），日志 logs/ 和 scanner_data/
- BinanceClient: binance_client.py
- Dashboard: crypto-dashboard.service，端口8081，前端web/dashboard_static/
- 双向持仓(Hedge Mode)，4倍全仓
- 余额: ~2899 USDT（Testnet，2026-04-28）

### 账号B配置（v13策略，当前）
- API Key: vGlIBNOECTVfOeEdPMSbSNzgJN7MM2I7dN3DhAapwlLfo6adna8wu6UAnXaGvNJv
- API Secret: 3qDiJaLSBG5pjipfV42qSMZwdvZcuoQf2Zo9TxTnr6ZchLMGqr4gZo3JVpHKiPqV
- Scanner: scanner_v13.py（v13），独立日志 logs_v13/ 和 scanner_data_v13/
- BinanceClientV2: binance_client_v2.py
- Dashboard: crypto-dashboard-b.service，端口8082，前端web/dashboard_static_b/
- 双向持仓(Hedge Mode)，4倍全仓
- v13特点：去掉EVICT、禁用VPB、不强制共振、110+分跳过、动态投入、多头+10分门槛、浮动止损放宽(1.2/1.5×ATR)、止盈收紧(3.0/4.0×ATR)、冷却期120分钟、MAX_LOSS=30%、同币种止损保护(≥2次→24h)

### 账号C配置（v14策略，2026-05-02新增）
- API Key: JHrzk9ZAmjn3f6dEztm7eYEfwTlOvLGTP3PdiJJS5XLU9gd2ZezHZAXV09oiB67f
- API Secret: tlfSQBj0n9vcnpKdHBnSu5rvCH5QnD9osQenweJukSndwNy9JoifKZBc5uhMn9CZ
- Scanner: scanner_v14.py（v14），独立日志 logs_v14/ 和 scanner_data_v14/
- BinanceClientV3: binance_client_v3.py
- Dashboard: crypto-dashboard-c.service，端口8084，前端web/dashboard_static_c/
- 双向持仓(Hedge Mode)，4倍全仓
- **get_balance返回list格式**（与A/B的dict不同），dashboard API已兼容
- v14策略：四维度评分（趋势/动量/量价/结构各25分，满分100）+ 系统性否决机制
- 扫描周期：15m+1h双周期（1h过滤噪声）
- 阶梯式移动止损：2.5×ATR→成本价，4.0×ATR→2.0×ATR，6.0×ATR→2.5×ATR
- 初始止损：1.8×ATR（15m）/ 2.0×ATR（1h）
- **v14优化（2026-05-03）**：空头门槛+15（SHORT_PENALTY）、6h涨幅>8%追高保护
- **v14优化（2026-05-06）**：分档ATR止损（低波2.3/中波2.0/高波1.5）+ 同赛道分散≤3仓 + 阈值25 + 连续亏损冷却120min（5次触发）+ SCORE_MAX=80
- **v14优化（2026-05-08）**：BTC大盘趋势过滤（bull→空头+15/bear→多头+15）+ 浮动止损放宽（activate=2.0, pullback=2.0/2.5）+ 硬底恢复30%（用户要求）+ 震荡仓清理（>48h且±3%内平仓）+ 手动平仓9个超亏仓
- 余额: ~158 USDT可用（Testnet，2026-05-08）

### Scanner 版本演进
- **2026-05-17 全局风控与工程优化（P0/P1/P2/P3，已部署）**：
  - P0/P1 已完成并部署：`scanner.py` / `scanner_v14.py` / `scanner_v16.py` 均加入全账户总持仓上限、可用保证金保护、15m降权、空头额外门槛；未新增黑名单。
  - A/v11：`MAX_TOTAL_POSITIONS=30`，`MAX_POSITIONS_PER_TF=2`，`SCORE_THRESHOLDS={"15m":105,"30m":90}`，`SHORT_ENTRY_PENALTY=20`，开仓后保留可用保证金 `max(300USDT, 25%权益)`。
  - B/v16：`TIMEFRAMES=["1h","15m"]`，`MAX_TOTAL_POSITIONS=20`，`SCORE_THRESHOLDS={"1h":40,"15m":55}`，`SHORT_ENTRY_PENALTY=10`，开仓后保留可用保证金 `max(300USDT, 25%权益)`。
  - C/v14：`TIMEFRAMES=["1h","15m"]`，`MAX_TOTAL_POSITIONS=20`，`MAX_POSITIONS_PER_TF=2`，`SCORE_THRESHOLDS={"15m":55,"1h":35}`，`SHORT_ENTRY_PENALTY=15`，开仓后保留可用保证金 `max(300USDT, 25%权益)`。
  - P2 已完成并部署：v11/v14/v16 增加盈利回撤保护（浮盈超过50USDT后，从最高浮盈回撤35%触发平仓）、单方向持仓上限 `MAX_POS_PER_SIDE=12`、止损/硬底后更长冷却；v14同赛道上限从3降为2。
  - P3 已完成并部署：v16 补齐 `scanner_data_v16/events.jsonl`、`logs_v16/signals.jsonl`、`logs_v16/system.jsonl`；Dashboard B 已改为优先读取 v16 数据并恢复 `crypto-dashboard-b.service` active/enabled。
- **v14（当前，账号C，2026-05-12优化）**：四维度评分+资金费率信号+黑名单优化+硬底20%
- **v16（账号B，2026-05-12新增+v2升级，2026-05-13 v3持久化）**：CVD累积量差(20分)+Delta Divergence(20分)+ORB开盘突破(15分)+RSI(15分)+资金费率(10分)+EMA(10分)，阈值35-90，止损2×ATR，止盈4×ATR，复用v13 API Key
  - **v3 (2026-05-13)**: 持仓持久化(`positions_v16.json`) + `(symbol,side)`复合key + 交易所余额初始化 + 重启恢复 + ExchangeInfoCache精度修复(-1111) + Python 3.13兼容(utcnow→timezone.utc)
  - **2026-05-13**: CVD信号改用实盘数据（通过SSH隧道代理），MIN_VOL_RATIO=1.2→0.3
  - 实盘CVD架构（2026-05-18迁移后）：腾讯云新加坡服务器可直连 `https://fapi.binance.com`，v16 直接读取实盘 `aggTrades` 计算 CVD；本地 `binance_proxy.py`/SSH反向隧道仅作为阿里云旧架构备用。
  - 阿里云无法直接访问 `fapi.binance.com`（被墙），已不再作为主运行节点。
  - **精度修复**：ExchangeInfoCache缓存705交易对的qtyPrecision/pricePrecision/stepSize，`round_qty()`按stepSize向下取整
- **v13回测（2026-05-12）**：阈值80+原始信号函数+44币+3个月→257笔，胜率31.1%，净亏99%。**阈值80信号质量太低**。实盘+$209主要靠交易所自动平仓+$289
- **v11（当前，账号A）**：去掉固定止损（只用浮动止损+最大亏损兜底），冷静期统一30分钟，MAX_LOSS=30%（2026-05-08从50%改为30%）+ 双向持仓独立监控修复 + 震荡仓清理 >48h
  - **2026-05-20 满仓冻结修复（已部署）**：v11 恢复保守“强信号替换弱仓”机制，解决满仓后策略无法表达的问题。普通信号满仓仍不开；强信号 `abs(score)>=112` 才允许等量释放弱仓后重试开仓。保护条件：盈利仓 `pnl>=2%` 不释放、持仓年龄 `<30min` 不释放、新信号需比旧仓分数高 `25` 分（恢复仓 `score=0` 例外）、被释放币种冷却 `90min`。
  - **2026-05-20 v11 恢复仓位可见性修复（已部署）**：启动同步交易所持仓时不再受 `MAX_POSITIONS_PER_TF=2` 限制，所有真实持仓都会恢复到本地 `self.positions`，确保硬顶、尾部管理、弱仓替换能看到全量仓位；每周期上限只限制普通新开仓。
  - **2026-05-20 v11 周期池满仓修复（已部署）**：外层开仓循环不再在周期池满时直接跳过所有信号；周期池满时普通信号继续跳过，强信号进入 `force_replacement`，先释放弱仓再开新仓。共振加分后的最终分数参与强信号判断；若新方向已达 `MAX_POS_PER_SIDE`，只允许释放同方向弱仓，避免单边暴露扩大。
- v12: 30m阈值90，EVICT恢复，REQUIRE_RESONANCE=True（已停用，被v13替代）
- v10: 15m+30m双周期，开仓阈值80，去掉EVICT，持仓上限6
- v9: 动态扩容阈值90+盈利保护+市场趋势过滤
- v8: VPB5m降权+量能上限5x+MACD背离/ST翻转提权+ADX降权+最大亏损50%

### SimPosition 数据类
- 字段：symbol, side, entry_price, size, leverage, stop_loss, take_profit, atr_at_entry, entry_time, entry_score, entry_reason, timeframe, resonance, trailing_sl/active, pos_id, sl_mult, tp_mult, trail_activate, trail_pullback
- 交易所订单字段：**order_id**（交易所订单ID）, **exchange_qty**（交易所下单数量，float，基础币数量）
- 注意：旧版字段名okx_ord_id/okx_sz已于2026-04-29替换

### 关键文件
- `binance_client.py` — 账号A客户端（urllib+HMAC签名，返回Binance原生格式）
- `binance_client_v2.py` — 账号B客户端（同上，Account B API Key）
- `binance_client_v3.py` — 账号C客户端（同上，Account C API Key）
- `scanner.py` — v11扫描器（账号A，对照组不改）
- `scanner_v12.py` — v12扫描器（已停用，被v13替代）
- `scanner_v13.py` — v13扫描器（账号B，已停用被v16替代）
- `scanner_v16.py` — v16扫描器（账号B，实盘CVD+Delta+ORB+RSI+FR+EMA）
- `binance_proxy.py` — 本地Binance实盘API转发代理（SSH隧道配套）
- `scanner_v14.py` — v14扫描器（账号C，四维度新策略）
- `strategy_breakout.py` — VPB量价突破策略
- `daily_review.py` — 每日复盘脚本
- `web/dashboard_api.py` — Dashboard API（账号A，端口8081）
- `web/dashboard_api_b.py` — Dashboard API（账号B，端口8082，2026-05-17已改为优先读取v16数据，兼容v13/v12）
- `web/dashboard_api_c.py` — Dashboard API（账号C，端口8084，读取v14数据）
- `web/dashboard_dual_api.py` — 双账号对比Dashboard（端口8083）
- `web/dashboard_static/index.html` — 账号A前端（蓝色主题，8082样式）
- `web/dashboard_static_b/index.html` — 账号B前端（紫色主题）
- `web/dashboard_static_c/index.html` — 账号C前端（绿色主题 #064e3b）
- `web/dashboard_static_dual/index.html` — A/B对比前端（全维度对比）
- `cloud/analyzer/auxiliary.py` — 指标库

### 服务器部署
- 主节点 IP: 129.226.151.144, 用户: ubuntu, OS: Ubuntu 22.04 LTS
- Python: `/opt/crypto-auto-trader/.venv/bin/python`
- 项目: `/opt/crypto-auto-trader`
- systemd: `crypto-scanner.service`(v11) / `crypto-scanner-v14.service`(v14) / `crypto-scanner-v16.service`(v16) 均 enabled+active；Dashboard 服务 `crypto-dashboard*` 按用户要求全部 disabled/inactive。
- 旧阿里云 IP: 39.105.156.210，所有 `crypto-*` 服务 disabled，无运行进程；可作为冷备/复盘/回测机。
- v12/v13已停用。
- deploy.py 一键部署 / deploy_dashboard_dual.py 双Dashboard部署 / pull_logs.py 拉取日志
- SSH免密: 已配置

### 已知问题（已修复）
- **2026-05-17 P0/P1/P2/P3已部署**：解决保证金不足反复下单、15m过度交易、空头门槛偏低、v16复盘日志缺失、Dashboard B inactive/读取旧数据等问题。服务器编译通过，`crypto-scanner` / `crypto-scanner-v14` / `crypto-scanner-v16` / `crypto-dashboard-b` 均 active。
- **2026-05-19 v16恢复开仓能力优化**：将 `MIN_VOL_RATIO` 改为动态门槛（主流币更低、普通币 0.6、高波动 0.8），把 15m 从硬性一票否决改为“反向强否决 / 弱确认放行 / 高分无确认可放行”，CVD+OFI 改为同向强共振优先、背离降权，补了 `SCAN_STATS` 聚合日志与低仓位恢复模式；验证通过后 v16 重新开始开仓。
- **2026-05-19 每日市场复盘功能**：新增 `部署工具/daily_market_review.py`，可自动生成昨日市场涨跌排行榜，逐个比对 A/v11、B/v16、C/v14 是否开仓、方向是否正确、未开仓原因，并汇总各策略昨日最大盈亏交易的入场原因、持仓时间和离场时机；已成功生成 `reports/market_review_2026-05-18.md`。
- **2026-05-20 复盘口径升级（已部署）**：`daily_market_review.py` 默认同时输出当日全量已平仓PnL、剔除恢复仓PnL、恢复仓贡献、当前未平仓浮盈/仓位，以及累计全量/剔除恢复仓/恢复仓PnL和剔除恢复仓盈亏因子。策略进化优先参考“剔除恢复仓PnL + 当前浮盈”，恢复仓只作为账户接管后的平仓结果，不作为策略入场质量证据。已用完整 527 个 USDT 永续样本重新生成 `reports/market_review_2026-05-19.md/html`。
- **2026-05-21 复盘HTML可读性升级（已部署）**：`daily_market_review.py` 的 HTML 渲染器为 PnL/涨跌正负、开仓正确、反向开仓、未开卡点、总持仓限制、确认周期过滤、前置过滤、候选信号自动上色；修复“无反向开仓”误标红、日期数字误染色、入场原因含 `|` 导致表格裂列的问题。已重新生成并拉回 `复盘报告/market_review_2026-05-20.md/html`。
- **双向持仓key bug（2026-05-08修复）**: positions dict用symbol做key导致同币种双向持仓(long+short)只有一个被监控。已修复为(symbol, side)复合key。影响SIRENUSDT空头亏-116%未被止损。
- 阿里云安全组未开放8081/8082端口，外部暂无法访问Dashboard
- Binance Testnet偶发SSL瞬断（UNEXPECTED_EOF_WHILE_READING），不影响运行
- 小币种可能报-4131 PERCENT_PRICE（Testnet流动性不足）
- TradFi-Perps合约报-4411，自动加入黑名单
- **-1111 Precision错误（2026-05-13修复）**: v16 `round(qty, 4)` 对qtyPrec=0的币种(CHR/MBOX/VELVET等)报-1111。ExchangeInfoCache按stepSize取整修复。

### 研究系统后续待做
- P2：把 `research_memory` 直接喂给 `experiment_runner.py`，让候选假设自动生成影子实验，而不是手动挑实验。
- P2：把 `symbol_lessons` / `factor_lessons` 接到日报和回放页里，做到每个策略的高频问题可回查。
- P3：做半自动闭环，候选假设 -> 影子实验 -> 晋级门禁 -> 人工审批 -> 小仓上线。
- P3：把经验库的 `candidate_id`、`case_id`、`experiment_id` 三者串起来，形成统一追踪链。
- P3：把经验库同步到阿里云影子机定时任务里，独立于实盘执行。

### 用户偏好
- 中文回复，绝对路径，简洁直接
- 用户偏好绿涨红跌配色（--up:#22c55e, --down:#ef4444），Dashboard已全部统一
- Windows/PowerShell 环境，curl 需用 curl.exe

## 2026-05-21 分析阶段节点 + 复盘报告交互化
- 已按用户要求进入分析阶段：远端 `crypto-scanner` / `crypto-scanner-v14` / `crypto-scanner-v16` 全部停止并禁用，策略暂不重启、不新增交易；Dashboard 仍保持停用状态。
- 已完成全账号清仓复核：A/v11、B/v16、C/v14 当前持仓均为 0；后续复盘只做分析与归因，策略参数和实盘开仓逻辑暂不继续改动。
- `F:\AutoTrading\部署工具\daily_market_review.py` 已升级并部署到 `/opt/crypto-auto-trader/daily_market_review.py`：市场涨跌榜单移动到底部并改为可折叠展开；“三策略是否抓到大行情”和“大行情逐币卡点”合并为“ 大行情捕捉与卡点归因”单块。
- “各策略最大盈利/最小PnL交易”已替换为各策略全部交易/持仓明细：默认展示每笔摘要，点击单笔交易展开入场原因、离场原因、持仓时间、当天 15m K线，并在K线上标注入场和离场点。
- 反向开仓复盘已强化为“阶段归因 + K线定位”：真实反向仓默认展开K线，恢复仓反向样本单独列出，便于区分策略入场错误与账户接管风险。
- 已在腾讯云远端重新生成并拉回 `F:\AutoTrading\复盘报告\market_review_2026-05-20.md`、`F:\AutoTrading\复盘报告\market_review_2026-05-20.html`、`F:\AutoTrading\复盘报告\market_review_latest.html`；远端 `py_compile` 通过，HTML 静态检查确认存在交易卡、K线 SVG、反向仓高亮卡和底部折叠榜单。
- 2026-05-21 13:51 按用户要求重新启动全部策略：`crypto-scanner`(A/v11)、`crypto-scanner-v14`(C/v14)、`crypto-scanner-v16`(B/v16) 均已 `active/enabled`；Dashboard 服务仍保持 `inactive/disabled`。启动后复核：A/v11 持仓 0，C/v14 持仓 0，B/v16 已新开 2 仓（DOGEUSDT long、FHEUSDT short），进入真实运行阶段。
- 2026-05-22 生成 2026-05-21 复盘：远端成功生成并拉回 `F:\AutoTrading\复盘报告\market_review_2026-05-21.md/html`。昨日 A/v11 全量/剔除恢复仓 PnL -16.93（34笔，胜率35.3%），B/v16 +29.60（24笔，胜率54.2%），C/v14 -30.83（16笔，胜率31.2%）；三者昨日均无恢复仓贡献。重点问题：v14 HOME/BEAT/VELVET 硬顶亏损集中，v16 有 HYPE/TAG 反向短空强上涨币，v11 有 BUS 反向做多大跌币及多笔短线浮动止损噪音。
- 2026-05-22 复盘 K 线窗口升级：`F:\AutoTrading\部署工具\daily_market_review.py` 已从“全天压缩图”改为“交易窗口图”。每笔交易图现在围绕真实入场/离场时间动态截取 15m K 线，按持仓时长自动分配前后上下文，HTML 底部文案显示为 `交易窗口: 前X根 / 后Y根 15m K线`，便于同时观察进场前铺垫、持仓过程和离场后走势验证。新版已重新生成并落盘到 `F:\AutoTrading\复盘报告\market_review_2026-05-21.html`。

## 2026-05-23 哨兵扫描审计链路
- 已部署轻量高频异动哨兵：`crypto-market-mover-sentinel.service` 每 15 秒拉取 Binance 永续 24h ticker，按涨幅榜、跌幅榜、突然加速、放量榜生成 `runtime/market_mover_watchlist.json`；A/v11、B/v16、C/v14 均会把新鲜哨兵名单并入扫描列表，但哨兵只扩大扫描范围，不直接开仓。
- 三套 scanner 的 `decisions.jsonl` 已补充哨兵审计字段：`sentinel`、`sentinel_reason`、`sentinel_change_pct`、`sentinel_velocity_pct`、`sentinel_quote_volume`、`sentinel_volume_delta`、`sentinel_scan_result`、`decision_stage`、`filter_layer`。后续可追踪“哪个策略扫了哪个哨兵币、哨兵触发原因、策略/确认/风控/执行哪一层放行或否决、最终是否形成候选/开仓”。
- `F:\AutoTrading\部署工具\signal_quality_review.py` 已新增“哨兵扫描追踪”“哨兵开仓/候选链路”“哨兵逐币明细”三块报告内容。报告会展示时间、策略、币种、周期、方向、分数、哨兵上下文（涨跌幅/加速度/成交额/增量）、阶段/层级/结果和结论原因。

## 2026-05-24 总入口驾驶舱 + 哨兵400修复
- `F:\AutoTrading\部署工具\portal_dashboard.py` 已从报告入口页升级为“当前总结”驾驶舱：首页直接展示账号复盘PnL、三策略运行健康、哨兵链路摘要、信号/开仓/硬顶率、影子实验与研究审阅台待处理项，并自动列出需要用户优先关注的问题。
- 最新本地入口已生成：`F:\AutoTrading\reports\index.html` / `F:\AutoTrading\reports\portal_latest.html`。当前摘要显示 2026-05-23 信号质量周期三策略合计 PnL -11.68 USDT，A/v11 +8.91，B/v16 -1.27，C/v14 -19.32；哨兵可追踪决策 2641 条；C/v14 信号量 15866 但开仓 6，需优先看确认层和低分开仓质量。
- 已定位“哨兵逐币 HTTP Error 400: Bad Request”：哨兵来自 Binance 实盘永续市场，扫描器拉 K 线使用 testnet，部分实盘新币/小币在 testnet 不存在，导致 A/v11、C/v14 拉 K 线时报 400 并污染逐币明细。
- 本地已修复 `F:\AutoTrading\策略文件\scanner.py`、`F:\AutoTrading\策略文件\scanner_v14.py`、`F:\AutoTrading\策略文件\scanner_v16.py`：合并哨兵名单前先读取当前 testnet USDT 合约池，只把扫描市场可用币种交给策略；不可用币种写“哨兵过滤”日志，不再进入逐币 K 线分析。
- 本地 `py_compile` 已通过；远端部署尝试因 SSH `Error reading SSH protocol banner` 失败，待 SSH 恢复后需要重新运行 `F:\AutoTrading\部署工具\deploy_market_mover_sentinel.py` 将三套 scanner 修复发布到腾讯云。
- 2026-05-24 用户明确批准 `HYP-2026-05-22-B-v16-reverse_trade-stage-guard` 进入小仓观察；审批记录已写入 `F:\AutoTrading\research_memory\approvals\manual_actions.jsonl`，候选状态改为 `approved_for_small_live`，范围 `shadow_plus_small_live`。已同步更新 `research_memory/promotions/reviews_latest.jsonl`、`reviews_2026-05-23.jsonl`、`families_latest.json` 和 `experiments/families_latest.json`，并刷新 `reports/research_review_latest.html` 与总入口。
- 按用户偏好，总入口已移除“现在的结论”和“建议你今天这样看”两块引导区，仅保留指标、当前总结、各模块摘要和详情入口。
- 2026-05-24 总入口再次按“决策者视角”重排：第一屏展示昨日复盘PnL、昨天00:00到当前镜像PnL、策略运行、待关注问题和哨兵决策；新增“策略决策总览”（每策略PnL/平仓/胜率/信号/开仓/持仓/哨兵/最后活动/判断）、“功能运行状态”（数据同步/服务器镜像、三策略服务、哨兵链路、研究小仓审批）、“策略信号与可优化点”（最差/最好交易、确认过滤、分数否决、哨兵400）。详情入口移动到后面。
- 2026-05-24 按优先级完成一轮修复：1）腾讯云 SSH 连接恢复，并给 `deploy_market_mover_sentinel.py` 增加 5 次重试、`banner_timeout=60`、`auth_timeout=30`；2）已重新部署三套 scanner 和哨兵，远端四个服务 `crypto-market-mover-sentinel` / `crypto-scanner` / `crypto-scanner-v14` / `crypto-scanner-v16` 均 active，A/C 近期日志尾部未再出现新 HTTP 400；3）B/v16 已落地小仓阶段保护，默认启用 `V16_STAGE_GUARD_SMALL_LIVE`，新仓 `TRADE_SIZE=35USDT`，新增 `small_live_stage_guard`、`stage_guard_fail`、`trade_size_usdt` 审计字段；4）总入口问题卡已加 P0/P1/P2 分级；5）`experiment_runner.py` 已自动读取 `research_memory/approvals/manual_actions.jsonl`，重跑影子实验时会保留人工小仓审批，不再把 `approved_for_small_live` 覆盖回待审批。影子机 `/opt/crypto-shadow-lab` 已同步新脚本并重启 `crypto-shadow-review.timer`。

## 2026-05-24 Polymarket 独立系统方向
- 用户希望把 Polymarket 作为独立新系统慢慢实现，不作为 A/v11、B/v16、C/v14 的第四策略直接混入现有合约交易框架。后续建议目录为 `F:\AutoTrading\polymarket_lab`，先做只读研究与影子模拟，再考虑小仓实盘。
- 已阅读并讨论两篇 X 文章方向：1）Polymarket 结构化跟单，按领域筛选长期稳定盈利、低回撤地址，每个领域只跟踪少数代表地址，定期淘汰/替换；2）利用 Binance 等参考市场与 Polymarket 价格更新延迟做低延迟价差套利，文章声称历史延迟从约 10 秒收窄到 1-2 秒，真正竞争需要亚 50ms 级行情处理与 CLOB 下单。
- 系统定位：`polymarket_lab` 应先做“事件市场情报 + 聪明钱地址评分 + 影子跟单 + 价差机会记录”，不要一开始做自动实盘。核心输出进入独立报告页，展示市场、地址、机会、模拟盈亏、回撤、错过/误跟原因、可执行性评分。
- 第一阶段配置建议：`mode=shadow`；启用 `market_scanner`、`smart_money_tracker`、`copytrade_shadow`；禁用 `live_execution`；`binance_gap_arbitrage` 只记录不交易。初始风控可设单市场上限 20 USDT、单地址影子跟随上限 50 USDT、日最大亏损 30 USDT、最小盘口流动性 500 USDT、最小理论边际 3%、临近结算前 10 分钟不新开。
- 聪明钱地址评分初稿：近 60 天至少 30 笔、已实现盈利大于 500 USDT、最大回撤小于 25%、胜率大于 55%、盈利不能只来自单一极端事件；按体育/政治/宏观/crypto/文化等领域分桶，一个领域只保留少量候选 leader。
- 50ms 套利可行性结论：作为长期研究可以记录和验证，但不适合作为第一阶段主线。原因是 Polymarket CLOB API、网络路由、订单簿深度、成交排队、撤单延迟、签名与风控都可能吃掉理论价差；即使行情信号 50ms 内生成，实际成交质量仍需长期实测。
- 若未来尝试 50ms 级套利，需要条件：服务器靠近主要撮合/API 区域或优质低延迟云节点；稳定 WebSocket 行情接入 Binance/参考交易所和 Polymarket CLOB；本地常驻进程、异步事件循环、预加载市场映射、预计算订单参数；低延迟语言/框架优先 Python+uvloop 起步，瓶颈明确后关键路径可用 Rust/Go；完整延迟埋点记录 market tick -> signal -> order submit -> ack -> fill。
- 必备工程模块：市场映射器（Polymarket token/事件与 Binance symbol 映射）、盘口快照与增量校验、理论价格/概率转换、edge 与手续费/滑点模型、订单簿可成交量估算、Kelly/固定风险仓位、撤单与失败重试、影子撮合器、实盘熔断器、延迟分布报告。
- 合规与边界：只使用公开数据和官方 API，不做内幕信息、绕限制、账号规避或敏感信息获取；Polymarket 对地区/KYC/可用性有约束，实盘前必须确认官方规则。现阶段只推进只读与影子系统。
- 2026-05-24 已先做第一版只读探针：新增 `F:\AutoTrading\polymarket_lab\probe.py`、`config.example.json`、`README.md`。功能为拉取 Gamma 活跃市场和 CLOB 公共盘口，检查二元市场 Yes/No 是否存在 `buy_both`（两个 outcome ask 合计 < 1）或 `sell_both_inventory_required`（两个 outcome bid 合计 > 1）结构性毛套利，并输出 JSON/Markdown/HTML 报告到 `F:\AutoTrading\polymarket_lab\reports\`。
- 第一版实测结果：关键词样本 12 个、广谱高流动性样本 80 个均未发现达到 0.25% 阈值的结构性毛套利；从 250 个候选中按流动性挑前 80 复测，`markets_checked=80`、`opportunity_count=0`、`book_errors=0`。最接近盈利的盘口约差 0.1%（接近一跳报价），说明单次低频扫描捡不到明显无风险空间，后续若继续应做定时连续监控、机会持续时间统计和成交排队模拟。
- 2026-05-24 已按用户要求部署服务器端连续监控。新增 `F:\AutoTrading\polymarket_lab\monitor.py`、`F:\AutoTrading\部署工具\deploy_polymarket_aliyun.py`、`F:\AutoTrading\部署工具\deploy_polymarket_tencent.py`。监控为只读影子模式，每轮运行 `probe.py`，追加 `polymarket_monitor_summary.jsonl`，若出现机会则追加 `polymarket_opportunities.jsonl`。
- 阿里云优先节点 `/opt/polymarket-lab` 已部署 `polymarket-monitor.service`，但该节点当前到 `gamma-api.polymarket.com`、`clob.polymarket.com`、`fapi.binance.com` 的 HTTPS 出口均超时或 `Network is unreachable`，因此监控健康状态会明确 FAIL。腾讯云 fallback `/opt/polymarket-lab` 已部署并启用同名服务，第一轮真实扫描结果：Gamma 延迟约 42ms，`markets_checked=80`、`opportunity_count=0`、`book_errors=0`，服务 active。最新远端报告已拉回本地 `F:\AutoTrading\polymarket_lab\reports\polymarket_probe_latest.html/json/md` 和 `polymarket_monitor_summary.jsonl`。

## 2026-05-24 B/v16 小仓阶段保护临时关闭 + 系统性能审查
- 按用户要求先取消 B/v16 小仓阶段保护，后续需要时再恢复。已把 `F:\AutoTrading\策略文件\scanner_v16.py` 的 `V16_STAGE_GUARD_SMALL_LIVE` 默认值从 `1` 改为 `0`，保留环境变量开关；远端 `/opt/crypto-auto-trader/scanner_v16.py` 已部署并只重启 `crypto-scanner-v16`。远端最新 `SCAN_STATS` 确认 `small_live_stage_guard=false`、`trade_size_usdt=100.0`、`stage_guard_fail=0`。
- 关闭后 B/v16 当前仍 0 持仓，原因变为最新扫描批次 `no_signal=50`，不再是小仓阶段保护拦截。后续若仍长期空仓，需要看 v16 信号生成层/确认层，不是 stage guard。
- 已生成系统级代码与流程性能审查报告：`F:\AutoTrading\reports\system_performance_review_2026-05-24.md`。主要结论：最大瓶颈是三策略同步逐币拉 K 线、日志 JSONL 逐日膨胀、scanner 文件过大、账户状态重复查询、哨兵尚未事件驱动。建议近期 Python 内做日志分片/账户快照/异步行情缓存；中期做 `asyncio + aiohttp` 统一行情层；长期评估 Rust/Go 行情服务 + 本地缓存/事件总线，预期行情请求量下降 60-90%、单轮行情获取加速 3-6 倍、报告查询提升 10-50 倍。

## 2026-05-24 架构升级第一步：事件库 + 基线 + 清理
- 按“一次性彻底解决”的方向开始执行架构升级。新增 `F:\AutoTrading\core\event_store.py`，提供 SQLite 事件库表：`events`、`account_snapshots`、`baseline_runs`；新增 `F:\AutoTrading\部署工具\system_baseline.py` 采集本地/服务器基线；新增 `F:\AutoTrading\部署工具\event_ingest.py` 将近期 JSONL 审计事件灌入事件库。
- 本地已生成 `F:\AutoTrading\runtime\event_store.sqlite3`，并从 `F:\AutoTrading\server_logs_tencent` 灌入近期事件 21,574 条；服务器 `/opt/crypto-auto-trader/runtime/event_store.sqlite3` 已部署并灌入近期事件 25,141 条。服务器基线报告已生成并拉回 `F:\AutoTrading\reports\server_system_baseline_latest.json/md`。
- 清理：本地删除所有 `__pycache__` 与旧临时副本 `F:\AutoTrading\scanner.remote.tmp.py`；服务器删除 `/opt/crypto-auto-trader` 项目自身 `__pycache__`，未动 `.venv` 依赖缓存。未删除 `check_*.py`、`deploy_*fix.py` 等历史工具，后续需二次确认用途后再归档或删除。
- 验证：腾讯云 `crypto-scanner`、`crypto-scanner-v14`、`crypto-scanner-v16`、`crypto-market-mover-sentinel`、`polymarket-monitor.service` 均保持 active；新增脚本本地和远端 `py_compile` 通过。进度记录见 `F:\AutoTrading\reports\architecture_migration_progress_2026-05-24.md`。

## 2026-05-25 架构升级第二阶段：三策略 JSONL + SQLite 双写
- 已接入三策略双写：`F:\AutoTrading\策略文件\scanner.py`、`scanner_v14.py`、`scanner_v16.py` 的 `log_event`、`log_signal`、`log_decision`、`log_system` 保留原 JSONL 写入，同时通过 `core.event_store.EventStoreWriter` 写入 `runtime/event_store.sqlite3`。交易主路径不依赖 SQLite；写入失败会静默跳过，不影响下单/平仓。
- `F:\AutoTrading\core\event_store.py` 新增 `EventStoreWriter`：默认启用，可用 `EVENT_STORE_ENABLED=0` 关闭；SQLite 使用 WAL，timeout 2 秒，前 9 次偶发失败只重置连接，避免三策略并发写入时因一次 lock 永久熔断。
- 已部署到腾讯云并重启 `crypto-scanner`、`crypto-scanner-v14`、`crypto-scanner-v16`。远端 `py_compile` 通过，服务均 active。验证：事件库第一次从 25,141 增至 25,708；加固后再次从 25,941 增至 26,490，A/v11、B/v16、C/v14 均有新增 `events/decisions/signals/system` 写入 SQLite。
- 本地和服务器项目自身 `__pycache__` 已再次清理；未动 `.venv` 依赖缓存。下一步是入口页 SQLite 影子读取，对比 JSONL 与 SQLite 后再切默认读取。

## 2026-05-25 架构升级第三阶段：入口页 SQLite 影子对比
- `F:\AutoTrading\部署工具\portal_dashboard.py` 已新增 SQLite 事件库影子读取，读取 `runtime/event_store.sqlite3`，生成“SQLite事件库”功能状态卡和“SQLite 事件库影子对比”正文表。入口页当前主指标仍以 JSONL 为准，SQLite 只做覆盖/一致性验证。
- 对比表展示：事件库路径、总事件、最新写入年龄、基线次数、A/v11/B/v16/C/v14 的 SQLite `events/decisions/signals/system` 计数，以及 JSONL 信号数、JSONL 哨兵数、最新事件 ID。用于判断第二阶段双写是否可信。
- 本地已生成 `F:\AutoTrading\reports\portal_latest.html` 和 `F:\AutoTrading\reports\index.html`；腾讯云已部署 `/opt/crypto-auto-trader/portal_dashboard.py` 并生成远端入口页，随后拉回本地覆盖最新入口页。
- 远端真实验证：入口页显示 `/opt/crypto-auto-trader/runtime/event_store.sqlite3` 总事件 `34,850`、最新写入 `4 秒前`、`SQLite 双写已覆盖 A/B/C`。腾讯云 `crypto-scanner`、`crypto-scanner-v14`、`crypto-scanner-v16`、`crypto-market-mover-sentinel`、`polymarket-monitor.service` 全部 active。下一步是修正 SQLite 与 JSONL 的分类口径，然后让入口页默认读 SQLite、JSONL 作为回退。

## 2026-05-25 架构升级第四阶段：1-4 顺序完成
- 按用户要求无需再确认，已顺序推进 1）SQLite 口径统一 + 首页默认读 SQLite，2）JSONL 日志按日期分片，3）三账户客户端 2 秒 AccountSnapshot 缓存，4）统一行情缓存 + 哨兵事件驱动雏形。
- `F:\AutoTrading\core\event_store.py` 规范化 strategy 名称：按 source 前缀映射为 `A/v11`、`B/v16`、`C/v14`。`F:\AutoTrading\部署工具\portal_dashboard.py` 在 SQLite 覆盖 A/B/C 后默认用 SQLite 生成策略状态和决策统计，JSONL 作为回退；入口页主指标显示“数据口径”，并新增“统一行情缓存”状态卡。
- `F:\AutoTrading\core\audit_log.py` 新增日期分片写入器。三策略与哨兵保留原 JSONL，同时追加 `YYYY-MM-DD.jsonl` 分片，降低后续入口页/复盘读取成本。
- `F:\AutoTrading\交易客户端\binance_client.py`、`binance_client_v2.py`、`binance_client_v3.py` 增加余额和持仓短缓存，开仓/平仓后主动失效，减少同一扫描轮重复签名 API。
- 新增 `F:\AutoTrading\core\market_data_cache.py` 和 `F:\AutoTrading\策略文件\market_data_service.py`；腾讯云新增 `crypto-market-data-cache.service`，每 15 秒刷新 `/opt/crypto-auto-trader/runtime/market_data_cache.json`。三策略优先读取缓存的可交易合约与 Top 成交额，A/v11 同时读取 spike 候选，缓存失效时回退原 HTTP。
- 远端验证：腾讯云 `crypto-market-data-cache.service`、`crypto-scanner`、`crypto-scanner-v14`、`crypto-scanner-v16`、`crypto-market-mover-sentinel`、`polymarket-monitor.service` 全部 active；行情缓存覆盖 561 个可交易标的、Top 180，缓存年龄约 1.5 秒；SQLite 事件库从 40,664 增至 41,025；分片日志已生成；近 15 分钟未再发现哨兵逐币 HTTP 400。

## 2026-05-25 架构升级第五阶段：实时账户 + 事件总线 + 自动告警
- 已按用户要求继续推进：实时账户快照入库、首页账户盈亏切实时口径、哨兵事件总线化、scanner 低风险模块拆分、自动告警。
- 新增 `F:\AutoTrading\部署工具\account_snapshot_service.py`，腾讯云部署为 `crypto-account-snapshot.service`，每 30 秒采集三账号余额、可用余额、权益、浮盈亏、持仓、多空、名义仓位、硬顶风险，写入 SQLite `account_snapshots`、`runtime/account_snapshot_latest.json`，并刷新原 `复盘报告/account_snapshot_latest.html`。
- `F:\AutoTrading\部署工具\portal_dashboard.py` 首页已切实时账户口径：第一屏显示“实时账户浮盈亏”，新增“实时账户盈亏”表，优先读 SQLite 最新 `account_snapshots`；复盘/交易日志 PnL 仍保留在策略决策与复盘口径。
- 新增 `F:\AutoTrading\core\sentinel_event_bus.py`，哨兵会写 `runtime/sentinel_events.jsonl`，并批量写入 SQLite `source='sentinel/events'`。`core/market_watchlist.py` 优先读取事件流，旧 watchlist 文件作为回退。
- 新增 `F:\AutoTrading\core\sentinel_scanner.py`，三套 scanner 的哨兵字段映射、可交易市场过滤、扫描列表合并改为公共模块；交易主策略和执行逻辑未大改，属于低风险拆分。
- 新增 `F:\AutoTrading\部署工具\system_alerts.py`，腾讯云部署为 `crypto-system-alerts.service`，每 60 秒检查关键服务、行情缓存、SQLite 最新事件、账户快照新鲜度，输出 `runtime/alerts_latest.json`、`reports/alerts_latest.md`、`logs/alerts.jsonl`；首页新增“自动告警”状态卡。
- 远端验证：`crypto-account-snapshot.service`、`crypto-system-alerts.service`、`crypto-market-data-cache.service`、`crypto-scanner`、`crypto-scanner-v14`、`crypto-scanner-v16`、`crypto-market-mover-sentinel`、`polymarket-monitor.service` 全部 active；SQLite 事件库 `44,325` 条；A/B/C 快照各 6 条；哨兵事件总线 SQLite 记录 `914` 条；自动告警 `ok/0条`；近 10 分钟无 Traceback、ImportError、HTTP400。

## 2026-05-25 架构升级第六阶段：入口实时化、治理与安全收口
- 已完成用户要求的“修复顺序全部推进”：实时账户快照继续入库，入口页默认实时账户口径；新增 `crypto-portal-refresh.service` 每 60 秒刷新 `/opt/crypto-auto-trader/reports/portal_latest.html`，首页不再停留在凌晨复盘静态快照。
- `portal_dashboard.py` 已从 JSONL 全量扫描切到 SQLite 聚合优先、JSONL 仅回退。远端实测入口渲染从约 `8.17s / 269MB RSS` 降到 `1.40s / 25MB RSS`，并可在首页直接看到账号盈亏、持仓、服务状态、告警、策略信号、哨兵、影子实验、研究审阅台摘要。
- 修复每日复盘 OOM：`daily_market_review.py`、`signal_quality_review.py` 支持按日分片读取，不再整文件加载巨型 JSONL；`crypto-market-review.service` 已重新跑通 2026-05-24 复盘，Result=success，生成 `reports/market_review_2026-05-24.html/md`。
- 哨兵改为“watchlist 高频刷新 + 事件总线低频物化”：scanner 读取 `runtime/market_mover_watchlist.json`，事件总线只记录首次进入、突发加速、或 24h 涨跌幅跳变 >=3 个百分点的信号；纯“放量榜”轮换只进 watchlist，不刷事件。清理后 `sentinel/events` 重建为低噪声流，重启后约 42 条基线事件，后续只新增实质变化。
- `core/sentinel_event_bus.py` 尾读改为文件尾部块读取，避免每轮加载完整事件流；`core/kline_cache.py` 给三策略增加本地 K 线短缓存，减少三策略重复拉同币种 K 线。
- 数据治理已部署：新增 `data_maintenance.py` 和 `crypto-data-maintenance.timer`，大主 JSONL 已切按日分片并压缩归档；清理重复哨兵 SQLite 事件、账户 30 秒 HTML 噪声、旧哨兵/账户快照分片、大 stdout；交易主日志保留，避免持仓恢复和历史交易口径断裂。
- 远端旧 Dashboard 体系已下线并删除：`crypto-dashboard*` unit 文件和 `/opt/crypto-auto-trader/web` 已移除；本地旧 `Dashboard` 源码、重复日志缓存 `服务器日志`、`部署工具/server_logs`、`部署工具/server_logs_tencent`、`__pycache__` 已删除；保留当前活跃镜像 `F:\AutoTrading\server_logs_tencent`。
- 凭据安全收口：三个 Binance 客户端不再硬编码 API key/secret，改为 `BINANCE_A/B/C_API_KEY` 与 `BINANCE_A/B/C_API_SECRET` 环境变量；腾讯实盘节点使用 root-only `/etc/crypto-auto-trader/trading.env` 供 systemd 加载。部署/同步脚本移除明文 SSH 密码，腾讯和阿里云已禁用 SSH 密码登录并复核 key 登录正常。
- 阿里云影子机同步链路已恢复为 key 模式：生成阿里云到腾讯的专用 `autotrading_tencent_sync` key，`shadow_sync_from_tencent.py` 支持腾讯主 JSONL 已归档后的按日分片兜底；冒烟同步成功，`crypto-shadow-review.timer` active，`polymarket-monitor.service` active。
- 最终远端验收点：腾讯 `crypto-market-data-cache`、`crypto-account-snapshot`、`crypto-market-mover-sentinel`、`crypto-scanner`、`crypto-scanner-v14`、`crypto-scanner-v16`、`crypto-portal-refresh`、`crypto-system-alerts`、`polymarket-monitor` 全部 active；自动告警 `ok/0`；重启后 HTTP 400 计数为 0；账户实时快照显示 3 个账户、7 个持仓、未实现盈亏约 +10.72 USDT（2026-05-25 14:23 CST 时点）。

## 2026-05-25 架构容量自查与固定容量治理
- 自查结论：架构性能目标大体达成，入口页已实时化并切到 SQLite 聚合优先；行情缓存、账户快照、哨兵 watchlist、影子同步、自动告警主链路均运行。新增发现的缺口是 `crypto-data-maintenance.timer` 之前处于 inactive，且三策略 console INFO 日志仍会刷大 `stderr.log`，这会让固定磁盘容量下后期再次膨胀。
- 已修复：三策略支持 `SCANNER_CONSOLE_LOG_LEVEL`，腾讯 systemd drop-in 设为 `WARNING`，结构化审计仍写 SQLite/JSONL，console 不再输出逐币 INFO 刷屏。`logs_v14/stderr.log` 已从约 21MB 裁到约 0.63MB。
- 已把 `crypto-data-maintenance.timer` 改为 hourly 并 `enable --now`。维护策略：每小时保留 7 天热 JSONL 分片，超过 7 天压缩到 `archive/daily_shards`；压缩归档保留 90 天；非交易 SQLite 原始事件保留 14 天，交易事件保留；账户快照保留 30 天；哨兵/账户/market_mover 噪声分片归档；文本日志超过阈值裁到 5000 行。
- `system_alerts.py` 已新增磁盘容量、维护 timer、复盘 timer、当日超大分片、文本日志超大检测；入口页“自动告警”卡会显示磁盘已用百分比和剩余 GB。当前腾讯磁盘约 50GB，已用约 12.0%，剩余约 41.1GB。
- 自查后验收：腾讯 `crypto-data-maintenance.timer` active，下一次 16:00 CST；`crypto-system-alerts` ok/0；入口刷新正常；项目排除 `.venv` 和部署备份约 1.1GB，其中 `runtime/event_store.sqlite3` 约 351MB，`logs_v14` 约 249MB，`backtest_data` 约 166MB，归档约 71MB。

## 2026-05-25 优化后整体复盘与 Polymarket 复盘
- 腾讯主系统于 16:41-16:43 CST 实测健康：行情缓存、账户快照、哨兵、A/B/C scanner、入口刷新、系统告警及数据维护链路均 active；自动告警 `ok/0`；磁盘已用约 12.1%，可用约 41.04GB；修复后哨兵逐币 HTTP 400 计数为 0。
- 实时账户口径：三账户钱包合计约 9413.48 USDT，浮盈亏约 +17.90 USDT，当前 6 个持仓；其中 B/v16 浮盈约 +17.55 USDT，是当前正向贡献主体，A/v11 约 +0.35，C/v14 空仓。
- 2026-05-24 剔除恢复仓位口径复盘：A/v11 `-27.61`（累计 `-514.31`，PF `0.88`）；B/v16 `+47.29`（累计 `+465.30`，PF `1.53`）；C/v14 当日无有效新平仓且累计 `-593.34`，继续呈现大量信号但低转化问题。
- Polymarket 保持独立只读研究系统：腾讯节点连续监控 216 轮全成功、检查 17,220 个盘口、结构性套利机会 0、盘口错误 0；最接近机会仅约 0.1% 毛价差，当前不足以覆盖执行成本并进入实盘。阿里云 239 轮中 232 轮因网络不可达失败，不能作为当前采集主节点。
- 已修补 Polymarket 长期容量缺口：`F:\AutoTrading\polymarket_lab\monitor.py` 新增带时间戳探针报告固定保留（最近 288 轮）与汇总 JSONL 上限（25,920 行），已部署腾讯/阿里云并重启验证 `polymarket-monitor.service` active/running。详细复盘见 `F:\AutoTrading\reports\overall_system_review_2026-05-25.md`。

## 2026-05-25 开仓数量下降与胜率未提升诊断
- 用户指出“改动后开仓数量明显降低、胜率没有提升”。实测纠正：A/B 并非完全不开，`2026-05-23 -> 2026-05-24 -> 2026-05-25 17:34` 开仓约为 A `54 -> 108 -> 66`、B `39 -> 37 -> 109`、C `6 -> 0 -> 0`。体感下降主要来自活跃持仓少、C/v14 归零、以及 B/v16 在 2026-05-24 后半段被小仓阶段保护拦截。
- B/v16 5/24 阶段保护确实挡过大量 `score<55` 的单；但 5/25 已确认 `small_live_stage_guard=false`、`trade_size_usdt=100`。当前 B 的问题是胜率约 47.7%、平均盈利约 +3.21、平均亏损约 -3.16，赔率不足以覆盖低于 50% 的胜率，硬底/浮动止损损耗明显。
- A/v11 5/25 剔除恢复仓约 54 笔、胜率约 35.2%、PnL 约 +29.58，靠少数大赢家覆盖大量小亏；不是不开，而是低胜率高赔率结构仍不稳。
- C/v14 5/25 候选信号约 32,448 但开仓 0，核心问题是候选定义过宽，随后被 1h 阈值、15m 确认、尾部/赛道/冷却等层层否决，属于高噪声低转化。
- 结论：近期架构优化解决了实时入口、事件库、行情缓存、告警、日志容量和安全，不等于 alpha/胜率优化。下一步必须做 `OPEN_SKIPPED` 被拒信号的 15/30/60 分钟反事实收益评估，验证各过滤层是否真的挡掉坏单，而不是同时挡掉好单。详细诊断见 `F:\AutoTrading\reports\open_count_winrate_diagnosis_2026-05-25.md`。

## 2026-05-25 OPEN_SKIPPED 反事实评估落地
- 已完成第一版反事实评估器 `F:\AutoTrading\部署工具\counterfactual_open_skips.py`，并部署到腾讯主节点 `/opt/crypto-auto-trader/counterfactual_open_skips.py`。脚本读取 SQLite `events` 中有明确方向的 `OPEN_SKIPPED`，按下一根 1m K 线开盘模拟入场，计算 15/30/60/120 分钟收益、MFE/MAE、统一 1% TP/SL 先触发情况，并写回 SQLite 表 `counterfactual_open_skips`。
- 腾讯主节点已生成并拉回报告：`F:\AutoTrading\reports\counterfactual_open_skips_latest.md/html/json`。最近 48 小时评估窗口，60m 主口径完整样本约 669 条，若全部放行模拟 PnL 约 `-250.95 USDT`，模拟胜率约 `37.67%`，说明总体过滤有保护价值。
- 策略层结论：A/v11 的 `position_replacement: 周期池满且无可释放弱仓` 约 84 条，60m 模拟 PnL 约 `+124.42 USDT`，疑似明显错杀，应进入放宽/替换机制影子试验；B/v16 小仓阶段保护拦截约 284 条，60m 模拟 PnL 约 `-129.07 USDT`，总体证明阶段保护有保护价值，不宜简单删除；B/v16 `阈值未达:15m确认` 约 86 条，模拟 PnL 约 `-195.28 USDT`，应保留；C/v14 `15m无有效确认` 小样本为正但 `15m确认分不足` 整体为负，需要按 FOGO 等尾部样本继续拆分。
- 已配置腾讯 systemd 定时任务 `crypto-counterfactual-open-skips.timer`，每 2 小时运行一次同一评估服务；手动 `systemctl start crypto-counterfactual-open-skips.service` 已验证 Result=success。`system_alerts.py` 已纳入该 timer 和 service Result 检查，自动告警当前 `ok/0` 并显示该 timer active。

## 2026-05-25 A/v11 满仓替换释放修复 + 入口反事实并入
- 已按反事实结果优先修复 A/v11 满仓替换/释放逻辑：`F:\AutoTrading\策略文件\scanner.py` 的强信号替换阈值从极保守逻辑放宽为 `STRONG_SIGNAL_THRESHOLD=105`，引入 `counterfactual_v1` 替换策略；周期池满时只在同周期内等量释放弱仓，避免释放其他周期却仍无法开仓。
- 新释放规则：强信号可替换亏损或低质量旧仓；盈利 `>=6%` 的仓位硬保护；盈利 `>=2%` 的仓位只允许被明显更强信号替换；普通强信号最小持仓年龄 `20m`，精英信号 `>=120` 可缩短到 `10m`；释放日志会写入 `preferred_tf`、`require_preferred_tf`、分数差、软/硬保护阈值等字段，便于后续复盘。
- 腾讯主节点已部署新 `scanner.py`，本地与远端 SHA256 一致：`5ba8cf09659c669b7f0b6a468a6be81bf89516823a355470db76cf2e7ac06025`；远端 `py_compile` 通过，`crypto-scanner.service` 已重启并保持 `active/running`、`Result=success`、`NRestarts=0`。
- 总入口已并入 `OPEN_SKIPPED 反事实评估`：`F:\AutoTrading\部署工具\portal_dashboard.py` 读取 `counterfactual_open_skips_latest.json`，首页功能状态卡新增“反事实评估”，正文新增策略/过滤层反事实表，详情入口链接到 `F:\AutoTrading\reports\counterfactual_open_skips_latest.html`。
- 入口页当前会在待关注问题中直接暴露 `P1 A/v11 满仓替换错杀 +124.42 USDT`，让决策者第一屏看到“哪个过滤层可能错杀、有多少样本、模拟 PnL 是正还是负”，而不是必须点进详情才知道。
- 防卡住执行约定：后续远端操作优先使用短超时 SSH/分段验证/systemd 异步任务；长任务落到 service/timer 后查看状态、日志和产物，不在单条命令里无限等待；每 30 秒左右给用户一次进度说明，并把“已完成/阻塞/下一步”明确说出来。

## 2026-05-25 A/v11 100 USDT 保证金尺寸修复
- 用户发现 A/v11 当前仓位有 `quantity=1000`，但保证金不是强制的 `100 USDT`。根因已确认：旧逻辑把 `MAX_ORDER_SZ=1000` 当作“最大张数”安全阀，但 Binance U 本位的 `quantity` 是基础币数量；低价币被截断为 1000 个币后，`MEUSDT` 仅约 25 USDT 保证金、`NIGHTUSDT` 仅约 8 USDT 保证金。
- 已修复 `F:\AutoTrading\策略文件\scanner.py`：A/v11 不再用固定数量上限截断；开仓按 `目标保证金100 USDT * 杠杆 / 价格` 计算基础币数量，并以 `fixed_margin_v1` 做保证金校验。预计保证金必须在 `100 USDT ±5%` 内，否则直接 `OPEN_SKIPPED`，记录 `risk_category=position_sizing`、目标保证金、预计保证金、预计名义价值和数量。
- 同时调整开仓替换顺序：满仓释放弱仓现在发生在 ATR/止损方向/可交易性/尺寸校验之后，避免新信号本身尺寸不合规时先释放旧仓。
- 腾讯主节点已部署并重启 `crypto-scanner.service`，远端 SHA256 为 `ddaff9022e3101a8c74ef35a94455d764cdcd10246f34ed5a295dd1cb1bd93fc`，`py_compile` 通过，服务 `active/running`。部署后真实事件库验证：`BUSDT` 新开仓 `qty=1640`，`target_margin_usdt=100`，`expected_margin_usdt=99.999`，说明新逻辑已生效并允许低价币使用超过1000的基础币数量。
- 已增强实时账户与告警：`account_snapshot_service.py` 对 A/v11 持仓按入场价计算初始保证金，检查是否偏离 `100 USDT ±5%`；`system_alerts.py` 会把尺寸违规作为 `bad` 告警；`portal_dashboard.py` 首页实时账户表新增“尺寸违规”，第一屏会显示尺寸违规数量和示例。
- 当前旧仓仍有 2 个尺寸违规：`MEUSDT LONG qty=1000 initial_margin≈24.77`、`NIGHTUSDT SHORT qty=1000 initial_margin≈8.14`。未自动补仓或平仓，因为这属于真实交易动作；现有仓位继续由策略退出逻辑管理，入口页和告警会持续暴露，直到它们自然平仓或用户明确要求处理。

## 2026-05-25 总入口降噪偏好
- 用户明确要求总入口去掉解释性/教学式内容：不要再显示“昨日没抓到涨跌榜，怎么看”、不要“术语小抄”、不要“运行健康表”。入口页应保持决策者视角，只保留账户盈亏、尺寸/风控告警、策略决策、反事实评估、功能状态、可优化点、哨兵摘要和详情入口。
- `F:\AutoTrading\部署工具\portal_dashboard.py` 已删除上述三个区块及相关生成变量；详情入口中的“昨天为什么没抓到”卡片已改为中性“每日复盘”，说明文案改为“看市场变化、交易明细、策略卡点和大行情捕捉结果”。
- 新版已部署腾讯主节点并重启 `crypto-portal-refresh.service`；本地 `F:\AutoTrading\reports\index.html` / `portal_latest.html` 已拉回并修正为本地文件链接。验证关键词 `术语小抄`、`运行健康表`、`昨日没抓到涨跌榜`、`昨天为什么没抓到`、`看涨跌榜` 均不再出现在入口页。

## 2026-05-25 入库数据使用与策略进化链路审计
- 已生成审计报告 `F:\AutoTrading\reports\data_usage_strategy_evolution_audit_2026-05-25.md`，并同步到腾讯主节点 `/opt/crypto-auto-trader/reports/data_usage_strategy_evolution_audit_2026-05-25.md`。
- 结论：关键入库数据没有白写，但还没有做到“全部充分利用”。入口页、告警、反事实评估的 SQLite 方向基本准确，主指标按 `/signals`、`/decisions`、`/trades` 分源读取，避免了 `/events` 与 `/decisions` 双写重复计数。
- 腾讯 SQLite 当前约 `274k` events、`7398` account_snapshots、`2912` counterfactual rows。约 `69.45%` 事件来自主分析源（signals/decisions/trades/system）；其余主要是原始 `/events` 双写与 `sentinel/events`，当前更多是审计留档，尚未形成稳定策略优化输入。
- 未充分利用的数据资产：历史账户快照未做权益曲线/回撤/仓位利用率；哨兵事件总线未做长期误报/收益评分；baseline 只显示次数，未做性能趋势；反事实结果尚未自动生成候选；symbol/factor lessons 主要在研究审阅台展示，未进入晋级门槛。
- 策略升级能力判断：当前可以每天自动同步日志、生成研究案例/候选、跑影子实验并在审阅台提醒；但不能安全自动升级策略。现有 `experiment_runner.py` 默认只看 3 天，晋级门槛约 `sample_trades>=30`，缺少 14/30 天多窗口、out-of-sample、walk-forward、分市场状态、账户风险和反事实合并评分。
- 审计过程中发现 A/v11 新旧仓叠加风险：`fixed_margin_v1` 正确新单叠加旧低保证金 `MEUSDT` 后，交易所聚合初始保证金约 `124.87 USDT`。已紧急修复 `F:\AutoTrading\策略文件\scanner.py`：A/v11 开新仓前检查交易所/本地同币种持仓，若存在则 `OPEN_SKIPPED`，禁止继续同币种叠仓导致保证金偏离 100 USDT。远端 SHA256 `ab6ccec8e775649218bcb433b5db8777ec650c3bc18f0ad64833ffd18ffc0dcc`，`crypto-scanner.service` active/running。
- 后续目标：建立 `data_lineage_audit.py` 与 `strategy_evolution_gate.py`，把影子实验扩展到 3/7/14/30 天多窗口，合并 counterfactual、account_snapshots、sentinel quality 和研究候选，只允许自动升到“可审候选/小仓建议”，不允许无人工确认全量实盘升级。

## 2026-05-26 全策略同币种叠仓门控
- 用户要求“同币种已有仓时禁止再次开仓需要同步到所有策略”。规则已统一到 A/v11、B/v16、C/v14：开仓前实时读取交易所持仓，同时检查本地持仓；只要同一 symbol 已有非零仓位，不区分方向和周期，直接 `OPEN_SKIPPED`，禁止再次开仓。
- A/v11 保留 `fixed_margin_v1` 语义，跳过原因强调避免聚合仓位导致 100 USDT 保证金规则偏离；B/v16 与 C/v14 统一记录 `risk_category=position_duplicate`、`decision_stage=risk_gate`、`filter_layer=risk`，并写入 `existing_exchange_qty`、`existing_exchange_side`、`existing_entry_price`，便于后续在入口页解释“为什么没有开仓”。
- 同步修正 B/v16 扫描统计：只有 `_open_position()` 真正成功返回后，才增加 `open_positions` 和 `SCAN_STATS.opened`，避免被同币种门控、风险门控或执行失败拦截的候选被误报为已开仓。
- 本地三策略 `py_compile` 通过。腾讯主节点已部署并重启 `crypto-scanner.service`、`crypto-scanner-v14.service`、`crypto-scanner-v16.service`；远端 SHA256：A/v11 `ab6ccec8e775649218bcb433b5db8777ec650c3bc18f0ad64833ffd18ffc0dcc`，C/v14 `7a80b482ffa2c47b24d5377314849d0b2909df2a6133e23b9196e5bd6ddea147`，B/v16 `ff5733f3caa4517840122ac2ba5e835403d66df12d4ecd1259447038e7395723`。
- 验收状态：三个服务均 `active/running`、`Result=success`、`NRestarts=0`；短窗口 journal 只显示正常 stop/start，无启动报错。该规则只阻止未来叠仓，不自动平仓、补仓或调整当前已有持仓。

## 2026-05-26 策略进化提示优先级与验证原则
- 用户明确要求：当系统自动发现并充分验证出更优策略方案时，总入口必须以最明显的最高优先级提示决策者，优先于普通运行摘要、一般研究候选与日常复盘。
- 产品口径约束：`P0 已验证升级机会` 只能来自独立的严格晋级门禁结论；影子候选、短期正收益、人工批准小仓观察或单层反事实利好，均不能直接标记为“已验证更优方案”。
- 当前 `HYP-2026-05-22-B-v16-reverse_trade-stage-guard` 仍属于小仓观察，不属于已验证升级：已有记录显示短窗口原版 PnL `+64.1061`、影子 PnL `0`、错过盈利 `148.33`，虽减少硬顶但不足以证明整体更优。
- 目标进化门禁应包含：多窗口 3/7/14/30 天结果、样本充足性、留出集或 walk-forward、行情状态分层、手续费/滑点/延迟、最大回撤和硬顶风险、实际账户暴露、哨兵贡献质量、候选族多轮稳定性；自动化最高只可推荐“人工审批/小仓验证”，不得未经审批直接全量实盘升级。

## 2026-05-26 统一策略进化门禁落地
- 已新增只读统一门禁 `F:\AutoTrading\部署工具\strategy_evolution_gate.py`。它合并 `research_memory` 候选、`experiments` 影子实验、`counterfactual_open_skips`、人工审批和实时账户风险，输出 `runtime/strategy_evolution_latest.json`、`reports/strategy_evolution_latest.md/html/json`，不改策略代码、不下单。
- `experiment_runner.py` 已支持 `--windows 3,7,14,30`，生成 `experiments/results/windowed_latest.jsonl`。统一门禁使用多窗口结果，且新增硬规则：确认放行类实验如果没有真实/纸面撮合 PnL，不能晋级 P0/P1，避免 `original=0 shadow=0` 的假通过。
- `portal_dashboard.py` 已接入“策略进化门禁”：总入口“当前总结”最前面显示最高优先级进化结论；新增“策略进化门禁”表和“是否有更优方案”详情入口。只有门禁给出 P0/P1，才显示为“策略升级机会”；P2 只显示“策略进化观察”。
- 腾讯主节点已部署 `crypto-strategy-evolution-gate.service/timer`，每 2 小时运行：研究候选刷新（180s 超时，失败则保留旧候选）-> 多窗口影子实验 -> 实验报告/审阅台 -> 统一门禁 -> 总入口刷新。当前验收：service `Result=success`，timer active/waiting，`crypto-system-alerts` 和 `crypto-portal-refresh` active/running。
- 当前腾讯门禁结论：`P0=0 / P1=0 / P2=3 / REJECT=3`。最高项为 `EXP-20260523-v11-replacement-quality`，状态 `counterfactual_supported`，证据分 `97`、风险分 `25`，关键阻塞为 A/v11 仍有尺寸违规和长窗口不足。结论：有观察/验证方向，但没有已验证可升级方案。
- 阿里云影子节点也已同步更新：`shadow_sync_from_tencent.py` 修复远端 bundle fallback 拼接缺少分号导致的 `bash syntax error near unexpected token if`；`run_shadow_review.sh` 已加入多窗口实验和统一门禁，并给研究记忆构建加 `timeout 180s`，避免网络抓取卡住整条链路。手动验证已跨过日志同步阶段，timer active/waiting，service 最终重置为 success/inactive。

## 2026-05-26 GitHub迁移、长期状态文档与持久关注台账
- 用户已创建 private GitHub 仓库 `git@github.com:EdisonY/AutoTrading.git`。迁移口径：代码、部署定义、配置模板、文档、精简研究记忆可以入库；运行日志、SQLite、报告、服务器镜像、回测大文件、API/SSH 密钥绝不入库。
- 新增长期状态文档 `PROJECT_STATE.md`：用于多地开发快速恢复当前架构、已完成事项、未完成事项、服务入口和迁移规则。它与本文件分工：`MEMORY.md` 记录按时间发生的决策，`PROJECT_STATE.md` 记录当前状态。
- 新增持久关注台账 `research_memory/attention/open_items.json`：总入口和巡检会使用它保留值得决策者关注的问题/机会。日报滚动或复盘刷新不会自动遗忘，检测消失后先变成 `cleared_pending_review`，需要后续明确确认才关闭。
- Polymarket 作为独立只读研究系统纳入总入口复盘摘要；当前定位仍是样本收集和机会可行性验证，不接实盘交易。
- 2026-05-26 对 B/v16 的核对结论：不是超过两天未开仓。腾讯事件库显示 `2026-05-26 09:21:29 +08:00` 有 `XRPUSDT` short OPEN，且同日还有 `BTCUSDT`、`NEARUSDT`、`PORT3USDT` 等开仓；入口页需要区分“当前仍持仓的开仓时间”和“最近真实开仓事件”。
- 本地 Git 仓库已初始化为 `main` 并配置 remote `git@github.com:EdisonY/AutoTrading.git`；第一次推送曾因 `Permission denied (publickey)` 被拒，用户添加本机公钥后已成功推送 `main` 到 GitHub。当前机器公钥文件为 `C:\Users\Eels\.ssh\id_rsa.pub`，指纹 `SHA256:qxGXZU6nLnc0OtXLjRVjvH2NZv7p8c36rvRdTndJy+o`。

## 2026-05-26 README交接手册与运行数据口径
- 新增根目录 `README.md` 作为新电脑/新协作者启动手册：先读 `PROJECT_STATE.md`、再读 `MEMORY.md`、再读 `research_memory/attention/open_items.json`，然后按 README 的 fresh clone checklist 做本地验证。
- 日志、报告、SQLite、server mirror、回测数据、Polymarket probe 等运行数据不进 Git。原因不是“绝对高敏”，而是体积大、持续变化，且可能包含账户、订单、持仓、PnL 与策略行为细节；它们保留在服务器或本地 ignored 目录，按需同步分析。
- 2026-05-26 13:22 CST 核对服务器数据：腾讯 `/opt/crypto-auto-trader` 有 `runtime 929M`、`logs 118M`、`reports 14M`、`scanner_data*` 约 `129M`、`backtest_data 166M`；腾讯 `/opt/polymarket-lab/reports` 约 `46M`；阿里云 `/opt/crypto-shadow-lab/server_logs_tencent` 约 `459M`。
- 本地按需拉取腾讯紧凑日志镜像使用 `python 部署工具\sync_tencent_logs.py --days 3 --log-tail 800`，产物写入被 Git 忽略的 `server_logs_tencent/`。

## 2026-05-26 跨电脑完整接手工具链
- 已补齐“只依赖 Git 仓库 + 服务器访问资料”的接手链路：新增 `AGENTS.md` 给后续模型明确当前状态读取顺序、实时状态规则、部署回滚规则和防卡住约定；`README.md` 增加 fresh clone 依赖安装、实时上下文拉取、部署/回滚命令。
- 新增 `requirements.txt`，声明本地分析、部署和同步所需依赖：`numpy`、`pandas`、`paramiko`、Python 3.10 下的 `tomli`。`core/strategy_config.py` 已兼容 Python 3.10 的 `tomli` 回退。
- 从腾讯实盘节点恢复并纳入 Git 的关键依赖 `cloud/analyzer/auxiliary.py`，并补充 `cloud/__init__.py`、`cloud/analyzer/__init__.py`。此前 fresh clone 会缺少 scanner 引用的技术分析辅助模块，现在不会。
- 新增 `部署工具/pull_live_context.py`：一条命令拉取腾讯当前总入口、账户快照、告警、策略进化、持久关注台账和 Polymarket 摘要到 ignored 本地文件，并生成 `runtime/live_context_summary_latest.json`。2026-05-26 17:35 CST 实测成功：21 个文件拉取，关键服务全 `active`，当前持仓 7 个，浮盈亏约 `+82.6639 USDT`，关注项 P0=1/P2=3。
- 新增 `部署工具/release_manager.py`：按组件 dry-run/部署/列版本/回滚。部署时会在远端 `releases/<release-id>/backup` 备份旧文件、写 manifest、远端 `py_compile`、重启关联服务；默认不执行真实部署，必须加 `--apply`。
- 已验证：`release_manager.py` 和 `pull_live_context.py` 本地 `py_compile` 通过；腾讯 portal 和 strategy-b dry-run 成功，strategy-b dry-run 已包含 `cloud/analyzer/auxiliary.py`，避免未来部署漏依赖。
- 防再次“长时间无反馈”约定写入 `AGENTS.md`：长任务拆到 service/timer 或短超时命令，先报告阻塞再处理；当前状态问题必须先运行 `pull_live_context.py`，不能靠旧记忆猜测。

## 2026-05-26 全量变更同步 Git 与原因追踪规则
- 用户明确要求：后续所有操作、设计更新、优化和变更均应在当次工作结束前同步 Git，并清晰写明原因、完成内容、未完成内容、验证结果和线上影响，避免换电脑或换模型后失去决策上下文。
- 根目录新增 `CHANGELOG.md` 作为强制变更台账；只读查看且没有形成新结论/动作时不制造空提交，但部署、回滚、服务或配置调整、实盘风险决策即使没有改源代码也必须落账并推送。
- 新增 `部署工具/git_change_guard.py`：暂存中出现策略、交易客户端、`core`、配置、部署工具、Polymarket 或 `cloud` 等实质变更而未同步 `CHANGELOG.md` 时阻止提交；同时阻止 runtime、日志、报告、SQLite 和密钥类文件被纳入提交。
- `AGENTS.md` 与 `README.md` 已写入该规则，确保后续接手模型能理解并执行；待补项为未来配置 GitHub CI，在远端也自动校验此规则。

## 2026-05-26 C/v14 信号口径复盘与修正
- 用户指出 C/v14 信号太多、转开仓太少“不对”。代码检查确认：旧口径把 `15m` 确认周期候选和低至 `SCORE_MIN=25` 的原始分析候选都写成 `SIGNAL`，但真实开仓只看 `1h`，且阈值为多头 `55`、空头 `70`（`SHORT_ENTRY_PENALTY=15`），因此入口页“信号”不是可交易信号，严重放大了 C/v14 转化率异常。
- 已改为低风险口径修复：C/v14 未来只把通过真实入场分数门槛的 `1h` 候选写成 `SIGNAL`；`15m` 仍作为确认层，不再污染入口“入场候选”。系统心跳保留 `raw_signals_found` 和 `entry_signals_found`，便于后续看上游噪声与真实候选之间的漏斗。
- 总入口策略决策表列名从“信号”改为“入场候选”；对 C/v14 历史 SQLite 数据按 `1h + 实际阈值 + score<=80` 重算候选数，并显示原始候选数，避免继续误判。
- `config/v14.toml` 已从旧的错误/过时门槛同步到实际代码门槛：`confirm_min_score=35`、`score_min=25`、`score_threshold_1h=55`、`score_threshold_15m=65`。本次不放宽 C/v14 实盘开仓规则。
