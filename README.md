# Max Inverse Index（MII）

**看看 Max 又买了啥。**

公开地址：`https://cortana-hk.github.io/max-inverse-index/`

MII 是与 Max 账户收益反向联动的个人指数，支持多个彼此隔离的投资账号实例。

## 通用框架与实例资源

### 通用框架

- `docs/`：网页、实例选择器、图表、成交时间轴和持仓展示。
- `publisher/mii_publisher.py`：Tiger SDK 只读采集、反向指数计算、实例迁移与发布。
- `tests/`：收益、日界、安全和实例隔离测试。
- `config/instances.example.json`：实例配置模板。
- `.github/workflows/`：通用 Pages 部署。

### 每个账号独立的实例资源

| 资源 | 路径 |
|---|---|
| 实际实例清单 | `private/instances.json`（不进入 Git） |
| Tiger 配置 | 各实例的 `tiger_config` 指向的目录 |
| 成交记录 | 各实例的 `fills_file` 指向的文件 |
| 净值、份额与历史状态 | `private/instances/<实例ID>/private_state.json` |
| 出入金 | `private/instances/<实例ID>/cash_flows.json` |
| 公开快照 | `data/instances/<实例ID>/latest.json` |
| 公开指数历史 | `data/instances/<实例ID>/history.json` |
| 公开订单历史 | `data/instances/<实例ID>/events.json` |

`max` 是默认实例。旧状态已复制到实例目录，原文件继续保留；旧公开路径也保持兼容，不破坏已有链接。

## 新增账号实例

1. 参考 `config/instances.example.json` 编辑本地 `private/instances.json`，添加唯一的小写实例 ID、显示名、Tiger 配置目录和成交记录路径。
2. 新账号使用独立的私密状态目录，不复制其他账号的 `private_state.json`。
3. 先进行只读验证，不发布：
   ```bash
   python publisher/mii_publisher.py --dry-run --instance account-2
   ```
4. 首次发布该实例：
   ```bash
   python publisher/mii_publisher.py --once --instance account-2
   ```
5. 持续模式自动轮询所有启用实例：
   ```bash
   python publisher/mii_publisher.py
   ```

单个实例采集或发布失败不会阻止其他实例。未启用实例不会进入公开清单。网页通过右上角实例选择器切换，也可使用 `?instance=account-2` 分享指定实例。每个实例独立保存浏览器中的 100 起始日。

## 接入真实 Tiger 账号

真实账号在本地 `private/instances.json` 添加新实例，不要修改或复用 `max` 的状态，也不要把真实账号放进测试实例。建议实例 ID 使用 `account-2-real`，与现有 `account-2` 测试实例区分。

1. 将真实 Tiger 的 `tiger_openapi_config.properties` 和 token 配置放在账号自己的私密目录，例如 `private/accounts/account-2-real/config/`。该目录不会进入 Git。
2. 准备独立的成交记录路径，例如 `private/accounts/account-2-real/fills.jsonl`。如果现有交易服务已经在写成交记录，`fills_file` 直接指向那个文件；文件不存在时指数和持仓仍可发布，订单时间线为空。
3. 在 `private/instances.json` 的 `instances` 数组中加入：
   ```json
   {
     "id": "account-2-real",
     "name": "账号2",
     "enabled": true,
     "source": "tiger",
     "tiger_config": "private/accounts/account-2-real/config",
     "fills_file": "private/accounts/account-2-real/fills.jsonl"
   }
   ```
4. 暂停 MII 计划任务后执行 dry-run，确认无误再首次发布并恢复任务：
   ```bat
   schtasks /End /TN "MII Public Snapshot Publisher"
   python publisher/mii_publisher.py --dry-run --instance account-2-real
   python publisher/mii_publisher.py --once --instance account-2-real
   schtasks /Run /TN "MII Public Snapshot Publisher"
   ```

`source` 为 `tiger` 时直接通过 Tiger SDK 读取对应配置；凭证内容、账户号和配置路径都不会写入公开实例清单。

## 账号2测试实例

当前 `account-2` 是明确标记的测试实例，不是 Tiger 真实账号。它使用 `private/fixtures/account-2.json` 中依次推进的四组快照，以及独立的 `account-2-fills.jsonl`。每次发布推进一次游标，因此可以验证即时更新；测试数据与真实账号完全不共享状态，fixture 文件也不会进入 Git。

已验证连续两次发布时测试 MII 从 100 变为 95，提交只修改 `data/instances/account-2/` 和公开实例清单；`max` 公开数据及私密状态不变。网页可在 `Max` 与 `账号2（测试）` 之间来回切换。

## 计算口径

账户区间收益率 `r = 当期单位净值 / 上期单位净值 - 1`，MII 按 `MII(t) = MII(t-1) × (1-r)` 逐区间计算。单位净值通过基金份额法剔除出入金影响。

日界统一为纽约时间 20:00（美股盘后结束），使用 `America/New_York` 自动处理夏令时。今日变化比较当前点位与日界前的可用点位；没有日界前数据时使用当日首个可用点。

网页默认把当前实例最早数据所在日归一为 100。选择其他起始日后，累计变化和曲线重新归一，后台历史不变。

## 数据与安全

- 公开实例清单仅包含实例 ID、显示名和默认实例，不包含配置路径、账户号或凭证。
- 允许公开标的、仓位权重、平均成本、当前价格、订单时间和成交价格。
- 不公开持股数量、成交数量、资金金额、账户号或订单号。
- `private/` 不进入 Git；本仓库不包含 TIGER 交易源码。
- 网页无第三方脚本、无追踪器、无浏览器密钥。数据提交不会触发 Pages 重新部署。

MII 是个人展示品牌，不是可交易金融产品。数据可能延迟，仅供记录，不构成投资建议。
