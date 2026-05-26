# Polymarket Lab

独立 Polymarket 研究系统。当前版本只读运行，不下单、不签名、不接入钱包。

## 第一版目标

- 拉取 Polymarket Gamma 活跃市场。
- 拉取 CLOB 公共盘口。
- 检查二元市场是否存在 Yes/No 结构性毛套利：
  - `buy_both`: Yes 最优卖价 + No 最优卖价 < 1。
  - `sell_both`: Yes 最优买价 + No 最优买价 > 1，需要库存或可卖出份额，当前只做记录。
- 输出 JSON、Markdown、HTML 报告，用于判断是否值得继续做低延迟/小仓研究。

## 运行

```powershell
python F:\AutoTrading\polymarket_lab\probe.py --config F:\AutoTrading\polymarket_lab\config.example.json
```

连续监控：

```powershell
python F:\AutoTrading\polymarket_lab\monitor.py --config F:\AutoTrading\polymarket_lab\config.example.json --all-markets --max-orderbooks 80 --interval-seconds 300
```

输出目录：

- `F:\AutoTrading\polymarket_lab\reports\polymarket_probe_latest.json`
- `F:\AutoTrading\polymarket_lab\reports\polymarket_probe_latest.md`
- `F:\AutoTrading\polymarket_lab\reports\polymarket_probe_latest.html`

## 边界

这一版只看公开 API 与公开盘口。报告里的 edge 是毛边际，不等于可成交净利润，未扣除网络延迟、成交排队、滑点、撤单失败、资金占用和结算风险。

## 服务器部署

- 阿里云优先节点：`/opt/polymarket-lab`，服务 `polymarket-monitor.service`。当前该节点到 Polymarket/Binance HTTPS 出口不可用，监控会明确记录 FAIL。
- 腾讯云 fallback：`/opt/polymarket-lab`，服务 `polymarket-monitor.service`。当前可访问 Polymarket API，并按 5 分钟间隔连续扫描 80 个高流动性二元盘口。
- 连续监控保留最近 `288` 轮详细 JSON/Markdown/HTML 报告，并保留最多 `25920` 行汇总记录；盘口详情不再无限占用磁盘。
