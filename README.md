# Max Inverse Index（MII）

**看看 Max 又买了啥。**

公开地址：`https://cortana-hk.github.io/max-inverse-index/`

MII 是与账户收益反向联动的个人指数，支持多个互相隔离的真实账号实例。

## 目录规则

### 通用框架

- `docs/`：网页、图表、成交时间轴、持仓和实例选择器。
- `publisher/mii_publisher.py`：采集、计算和发布。
- `tests/`：计算、安全和实例隔离测试。

### 实例资源

每个实例只使用自己的资源：

| 资源 | 位置 |
|---|---|
| 实例与账号的唯一映射 | `private/instances.json` |
| 实例净值与历史状态 | `private/instances/<实例ID>/private_state.json` |
| 实例出入金 | `private/instances/<实例ID>/cash_flows.json` |
| 实例账号凭证 | `private/instances/<实例ID>/config/`（外部只读配置也可由映射指向） |
| 实例成交记录 | `private/instances/<实例ID>/fills.jsonl`（或映射到现有文件） |
| 实例公开数据 | `data/instances/<实例ID>/` |

不再使用 `private/accounts/`。一个实例绑定一个账号，其凭证、成交文件和私密状态都归属同一个实例目录；公开目录只保存脱敏数据。

当前只有 `test_data` 实例。它沿用原账号的只读 Tiger 配置 `../TIGER/config` 和成交记录 `../TIGER/data/fills.jsonl`，历史数据已整体保留并改名。公开数据只保存在 `data/instances/<实例ID>/`，根目录不再生成镜像副本。

## 添加第二个真实 Tiger 账号

### 1. 创建独立目录

在 `private/instances/` 下创建账号实例的凭证目录，例如：

```text
private/instances/account-2-real/config/
```

把该账号的 `tiger_openapi_config.properties` 和 token 配置放进 `config/`。`private/` 不会进入 Git。首次 dry-run 或发布时，程序会自动创建实例目录、空白 `fills.jsonl`、`cash_flows.json` 和 `private_state.json`；不需要手动创建这些文件。程序只会初始化对应实例自己的目录，不会写入外部路径。

### 2. 注册实例

编辑 `private/instances.json`，在 `instances` 数组中加入：

```json
{
  "id": "account-2-real",
  "name": "账号2",
  "enabled": true,
  "source": "tiger",
  "tiger_config": "private/instances/account-2-real/config",
  "fills_file": "private/instances/account-2-real/fills.jsonl"
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

## 历史回填与停更恢复

Tiger 实例每次运行会查询 `get_analytics_asset`，用真实历史净资产和出入金补齐日频指数历史，再拼接已有盘中快照。首次运行可回溯到券商提供的最早日期，本地服务停更后也会补齐接口仍能提供的缺口。订单通过 `get_orders` 分页查询，与本地成交记录合并去重，并缓存在 `private/instances/<实例ID>/broker_orders.json` 供后续增量同步。

券商接口没有提供的日期不会编造资产或指数。历史接口暂时失败时继续使用已有历史和当前快照，不阻断其他实例。

## 启用或禁用实例

编辑 `private/instances.json` 中对应实例的 `enabled`：

```json
{"id": "account-2-real", "enabled": false}
```

- `true`：启用采集和发布。
- `false`：停止后续采集，并在下一次成功发布时从网页实例清单隐藏；已有公开和私密历史不会删除。
- 重新设为 `true` 会从原状态继续，并自动补齐停更期间券商可提供的历史。
- 持续任务每轮重新读取配置，通常等待一个采集周期即可。需要立即生效时，先停止 MII 计划任务，修改后执行 `python publisher/mii_publisher.py --once`，再启动任务。
- `default_instance` 必须保持启用。要禁用默认实例，先把 `default_instance` 改成另一个已启用实例；系统至少需要一个启用的默认实例。

## 计算与安全

账户区间收益率 `r = 当期单位净值 / 上期单位净值 - 1`，MII 按 `MII(t) = MII(t-1) × (1-r)` 计算。日界为纽约时间 20:00，自动处理夏令时；没有日界前数据时使用当日首个可用点。

公开实例清单只含实例 ID 和显示名，不含配置路径、账户号或凭证。公开持仓和订单不包含持股数量、成交数量、资金金额或订单号。`private/` 不进入 Git。

MII 是个人展示品牌，不是可交易金融产品。数据可能延迟，仅供记录，不构成投资建议。
