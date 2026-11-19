# Max Inverse Index（MII）

**看看 Max 又买了啥。**

公开地址：`https://cortana-hk.github.io/max-inverse-index/`

MII 是与账户收益反向联动的个人指数，支持多个互相隔离的真实账号实例。

## 目录规则

### 通用框架

- `docs/`：网页、图表、成交时间轴、持仓和实例选择器。
- `publisher/mii_publisher.py`：采集、计算和发布。
- `tests/`：计算、安全和实例隔离测试。
- `config/instances.example.json`：配置模板。

### 实例资源

每个实例只使用自己的资源：

| 资源 | 位置 |
|---|---|
| 实例清单 | `private/instances.json` |
| 净值与历史状态 | `private/instances/<实例ID>/private_state.json` |
| 出入金 | `private/instances/<实例ID>/cash_flows.json` |
| 公开数据 | `data/instances/<实例ID>/` |
| Tiger 配置 | 实例的 `tiger_config` 指向的目录 |
| 成交记录 | 实例的 `fills_file` 指向的文件 |

当前只有 `test_data` 实例。它沿用原账号的只读 Tiger 配置 `../TIGER/config` 和成交记录 `../TIGER/data/fills.jsonl`，历史数据已整体保留并改名。公开数据只保存在 `data/instances/<实例ID>/`，根目录不再生成镜像副本。

## 添加第二个真实 Tiger 账号

### 1. 创建独立目录

在 `private/accounts/` 下创建账号目录，例如：

```text
private/accounts/account-2-real/config/
private/accounts/account-2-real/fills.jsonl
```

把该账号的 `tiger_openapi_config.properties` 和 token 配置放进 `config/`。`private/` 不会进入 Git。

### 2. 注册实例

编辑 `private/instances.json`，在 `instances` 数组中加入：

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

实例 ID 只能使用小写字母、数字、连字符和下划线。不要复制 `test_data` 的私密状态，也不要复用它的成交文件。

如果已有交易服务在写成交记录，`fills_file` 可以直接指向那个文件。没有成交文件时，指数和持仓仍可发布，订单时间线为空。

### 3. 验证并发布

在 `MII` 目录执行：

```bat
schtasks /End /TN "MII Public Snapshot Publisher"
python publisher/mii_publisher.py --dry-run --instance account-2-real
python publisher/mii_publisher.py --once --instance account-2-real
schtasks /Run /TN "MII Public Snapshot Publisher"
```

持续任务会轮询所有启用实例。一个实例失败不会阻止其他实例。网页右上角切换实例，也可访问 `?instance=account-2-real`。每个实例独立保存 100 起始日。

## 计算与安全

账户区间收益率 `r = 当期单位净值 / 上期单位净值 - 1`，MII 按 `MII(t) = MII(t-1) × (1-r)` 计算。日界为纽约时间 20:00，自动处理夏令时；没有日界前数据时使用当日首个可用点。

公开实例清单只含实例 ID 和显示名，不含配置路径、账户号或凭证。公开持仓和订单不包含持股数量、成交数量、资金金额或订单号。`private/` 不进入 Git。

MII 是个人展示品牌，不是可交易金融产品。数据可能延迟，仅供记录，不构成投资建议。
