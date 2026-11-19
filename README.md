# Max Inverse Index（MII）

**看看 Max 又买了啥。**

公开地址：`https://cortana-hk.github.io/max-inverse-index/`

MII 是与 Max 账户收益反向联动的个人指数。网页展示累计变化、今日变化、指数走势、成交时间轴，以及当前持仓权重、成本、现价和浮动盈亏。

## 项目边界

本目录是独立项目，包含网页、公开 JSON、发布器、测试和已忽略的 `private/` 私密状态。发布器直接调用 Tiger SDK，不导入或修改 TIGER 的 Python 代码，也不控制 TIGER 服务。

发布器默认只读访问：

- `../TIGER/config`：Tiger SDK 配置，可用 `--tiger-config` 或 `MII_TIGER_CONFIG_DIR` 改路径。
- `../TIGER/data/fills.jsonl`：本地成交记录，可用 `--fills-file` 或 `MII_FILLS_FILE` 改路径。

## 计算口径

账户区间收益率 `r = 当期单位净值 / 上期单位净值 - 1`，MII 按 `MII(t) = MII(t-1) × (1-r)` 逐区间计算。单位净值通过基金份额法剔除出入金影响。

日界统一为纽约时间 20:00（美股盘后结束），使用 `America/New_York` 时区自动处理夏令时。今日变化比较当前点位与日界时的可用点位。

后台保留最早可用快照以来的完整指数历史，订单事件从本地成交记录全量回填。网页默认把最早数据所在日归一为 100；选择其他起始日后，累计变化和曲线从该日起重新归一到 100，后台历史不变。

## 本地运行

```bash
python publisher/mii_publisher.py --dry-run
python publisher/mii_publisher.py --once
python publisher/mii_publisher.py
```

Windows 计划任务名为 `MII Public Snapshot Publisher`，动作指向本目录的 `publisher/mii_publisher.py`。出入金记录保存在已忽略的 `private/cash_flows.json`。

## 数据与安全

- `data/latest.json`、`data/history.json`、`data/events.json` 是唯一公开数据。
- 允许公开标的、仓位权重、平均成本、当前价格、订单时间和成交价格。
- 不公开持股数量、成交数量、资金金额、账户号或订单号。
- 券商配置和私密收益状态不进入 Git；本仓库不包含 TIGER 交易源码。
- 网页直接读取 raw JSON，无第三方脚本、无追踪器、无浏览器密钥。数据提交不会触发 Pages 重新部署。

MII 是个人展示品牌，不是可交易金融产品。数据可能延迟，仅供记录，不构成投资建议。
