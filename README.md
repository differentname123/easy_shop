# easy_shop

更加优惠方便的购物。

## 拼多多多人团商品采集

`app/pdd_fetch_group_info.py` 使用已有 Chrome 用户目录轮流采集各 Tab，商品和账号冷却状态全部保存到 MongoDB。每次响应只提交本批商品，不再加载或重写全量 CSV。

从项目根目录启动（Windows Git Bash，使用项目现有虚拟环境）：

```bash
.venv/Scripts/python.exe -m app.pdd_fetch_group_info
```

这是持续运行的采集器。启动前需准备可用的 Chrome 用户目录、安装 Chrome，并确保 MongoDB 可访问。Python 依赖为 `playwright`、`pymongo`、`filelock`；项目现有虚拟环境包含这些依赖。

### 配置

继续使用 `config/config.json`，无需新建存储配置。配置由项目路径读取，与启动时的工作目录无关：

| 配置键 | 含义 |
| --- | --- |
| `pdd_browser_data_list` | Chrome 用户目录完整路径列表 |
| `local_mongo_host` | MongoDB 主机 |
| `local_mongo_port` | MongoDB 端口 |
| `local_mongo_user` | 用户名；无需认证时为空 |
| `local_mongo_password` | 密码；无需认证时为空 |
| `local_mongo_db_name` | 业务数据库名 |

认证库沿用 `admin`。连接采用 5 秒的服务选择、连接及 socket 超时；启动时执行 ping 并创建索引。连接、索引或业务写入失败会终止本次运行，不降级回文件存储，也不会把写入失败的 Tab 当作完成。服务恢复后重新运行即可凭唯一键幂等刷新商品。

### 商品集合 `products`

唯一索引为 **`(platform, product_id)`**。`platform` 使用小写代码，例如 `pdd`、`taobao`、`jd`；`product_id` 为平台内商品 ID 的字符串。不同平台的同名 ID 不会覆盖，同平台重复采集更新原记录；本批未出现的历史商品不会删除。

| 字段 | 含义 |
| --- | --- |
| `platform` / `product_id` | 平台与平台内商品 ID |
| `category` | 当前采集 Tab 名称，不是官方商品类目 ID |
| `name` / `brand` | 商品名称与品牌 |
| `original_price` | 原价 |
| `activity_price` | 活动/补贴价 |
| `saved_price` | 接口提供的立省金额，不通过两价相减计算 |
| `currency` / `price_unit` | 当前 PDD 采集为 `CNY` / `yuan` |
| `sales_tip` | 原始销量提示文本 |
| `product_url` / `image_url` | 商品链接与主图链接 |
| `updated_at` | 最近一次写入的 UTC BSON 日期，不是格式化字符串 |

PDD 接口金额从分除以 100 转换为元；后续平台适配器应显式提供币种和金额单位。金额沿用当前采集器的数值存储方式，未引入 Decimal 或价格历史表。同一商品出现在多个 Tab 时保留最后一次观察到的分类；同批重复商品保留最后一条，新增/更新统计按本批唯一商品计算。

另有 `(platform, updated_at)` 索引用于按平台查询最新商品。更新统计使用 Mongo 的匹配数量，因此相同内容再次采集也属于更新；滚动结束条件仍为连续 10 次滑动无目标接口响应，和是否出现新商品无关。

### 账号集合 `crawler_account_status`

- 唯一索引：`(platform, account)`，`account` 是配置中的完整浏览器目录路径，而不是目录名。
- `last_used_at` 保存 UTC BSON 日期，在探测/采集开始前更新。
- 按配置顺序选取满足 30 分钟冷却期的账号；无状态/无有效时间戳的账号可立即使用，全员冷却时等待 60 秒。
- 数据库读取失败直接停止，不会误判所有账号可用。这是现有单进程调度的持久化，不提供多进程账号抢占锁。

### 后续多平台接入

存储层不包含具体平台的采集逻辑。淘宝、京东适配器转换为同样的字段后，可以复用 `ProductManager`：

```python
from common.mongo_db.mongo_base import gen_db_object
from common.mongo_db.mongo_manager import ProductManager

# 执行此示例会真实写入数据库；仅在准备好相应环境时运行。
db = gen_db_object()
try:
    db.ping()
    products = ProductManager(db)
    counts = products.upsert_products([
        {
            "platform": "jd",
            "product_id": "example-123",
            "name": "示例商品",
            "activity_price": 99.0,
            "currency": "CNY",
            "price_unit": "yuan",
        }
    ])
    latest = products.find_products("jd", limit=10)
    selected = products.find_products_by_ids("jd", ["example-123"])
    total = products.count_products("jd")
finally:
    db.close()
```

查询接口要求明确指定平台，避免跨平台同 ID 混查。此次未实现淘宝、京东的实际采集。

### 文件保留与迁移范围

**不迁移历史数据。** 原 CSV 和 `account_status.json` 保留原样，新采集器不再读取或写入它们；首次运行从 Mongo 中已有数据判断商品与账号状态，没有 Mongo 账号状态的账号视为首次使用。

错误截图/HTML（`error_data/`）、日志和 Chrome 用户目录继续保存在本地。现有 `app/pdd_fetch_group_info_debug.py` 仍是独立的历史 CSV 调试/分析脚本，不属于新的 Mongo 采集入口。

采集器没有持久化页码、滚动位置或已完成 Tab 游标。重启后从页面起点重新遍历，通过 Mongo upsert 去重。批量写入不是事务：数据库故障可能产生部分成功写入，失败批次不计入成功统计，重跑时幂等更新。

## 商品 AI 格式化

`app/goods_info_format.py` 是独立的单进程常驻任务，读取 `products` 中各平台商品的 `name`，使用现有模型网关的 `low` 模型组和 `prompt/商品数据结构化清洗.txt` 提取标准化数据。与采集进程分别启动：

```bash
.venv/Scripts/python.exe -m app.goods_info_format
```

启动前需准备现有 `config/config.json` 中的 MongoDB 和模型网关配置，并安装项目使用的 `openai`、`pymongo`、`filelock` 依赖。提示词通过脚本所在的项目路径定位，不依赖硬编码盘符。Ctrl+C 可退出并关闭数据库连接。

### 候选与轮次

- 查询 `format_status != "success"` 且 `format_retry_count < 3` 的商品；没有状态或次数字段的历史记录也可处理，缺失次数视为 0。
- 每轮只查询一次候选列表，每件商品在本轮只处理一次。正常完成、没有候选商品或本轮异常后，均等待 **3600 秒**再开始下一轮，即“本轮耗时 + 1 小时”，不是每小时整点执行。
- `gen_goods_format_info` 内最多尝试 3 次，失败后分别等待 2、4 秒；模型网关还可能自行重试或切换模型。这些内部尝试**都不增加数据库次数**。
- 只有该商品本轮所有内部尝试最终失败，才将 `format_retry_count` 原子加 1；第三次最终失败后不再自动处理。成功不增加也不清零已有失败次数。
- 空白或非字符串 `name` 记录为商品失败；数据库连接、查询、写入或提示词读取异常终止当前轮，记录日志并等待下一轮，不额外计入商品失败。

### 格式化字段

| 字段 | 含义 |
| --- | --- |
| `format_status` | `success` 或 `failed`；没有字段表示尚未处理 |
| `format_retry_count` | 累计最终失败轮数；首次处理成功也保存 0 |
| `format_info` | 校验后的真实 JSON 对象，保存为 BSON 子文档，不是响应文本或 JSON 字符串；失败为 null |
| `format_model` | 实际成功模型或最终失败时最后已知的尝试模型；未知为 null |
| `format_updated_at` | 本次处理结果保存的 UTC BSON 日期 |
| `format_error` | 最终失败的内部尝试错误说明；成功为空字符串 |
| `format_source_name` | 本次提取使用的原始商品标题 |

`format_info` 严格遵循提示词的 `core_entities`、`decision_keywords`、`quantity_info` 协议；允许空列表和数量 null，验证字段、类型、分数降序与重复项、数量层级乘积及末层单位。拒绝 Python 字面量、Markdown 围栏、注释、重复 JSON 字段、非标准数值，以及无法保存到 BSON 的超大整数或非法 Unicode 字符。本地校验不保证模型的商品语义判断正确。

保存使用条件更新，不创建缺失商品、不修改采集用的 `updated_at`。处理期间标题变化、商品被删除或不再符合条件时，会丢弃旧结果。数据库写入超时可能发生在服务端已提交之后，因此不盲目补写或再次增加次数，下一轮以数据库实际状态为准。

此任务按**单个格式化进程**使用，不提供多 worker 抢占锁。已成功商品即使后续标题变化，也不会自动重置格式化状态；若需重新处理，应显式重置相关格式化字段。`gen_goods_format_info` 的返回值现为包含 `status`、`format_info`、`model_used`、`error` 的结果字典，而非仅返回业务 JSON 或空字典。

## 离线测试

无需启动 Chrome、访问拼多多、调用 AI 或连接 MongoDB；使用标准库 `unittest.mock` 验证商品格式化、轮次重试和存储契约：

```bash
.venv/Scripts/python.exe -B -m unittest discover -s tests -v
```
