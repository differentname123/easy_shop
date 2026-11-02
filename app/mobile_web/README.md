# mobile_web

移动端导购与返利应用。当前实际接入 **拼多多**；淘宝、京东只保留明确禁用的兼容占位，不包含真实业务实现。

## 模块边界

```text
main.py                    HTTP 装配、生命周期、静态页面、日志
api/                       请求校验、认证依赖、调用应用服务
services/                  业务编排、缓存、授权有效期、后台同步节流
adapters/                  平台 SDK、商品标识、原始字段转换、转链降级
repositories/              Mongo 持久化、幂等更新、查询和聚合
models/                    商品身份与现有数据契约
cache.py / config.py       分域有界缓存及公共配置
static/js/
  api.js                   HTTP 传输，保留 HTTP 与业务状态
  session.js               已验证会话及过期响应隔离
  format.js                商品身份、列表去重、金额展示
  browser.js               剪贴板、提示、加载器、移动端跳转
  feeds.js                 首页/爆单列表、分页、搜索快照
  account.js               登录、授权、钱包订单
  app.js                   首页控制器组合与导航/手势
  detail.js                独立详情页及按需转链
```

没有引入构建工具、插件发现、任务队列或新的第三方依赖。原 Vue 库和 CSS 保持不变。`create_app()` 只负责装配，仓储延迟到首次访问时获取 Mongo 连接、初始化索引；索引失败不会被永久标记为成功。

## 平台扩展方式

1. 在 `adapters/` 新增对应适配器，实现现有 `BasePlatformAdapter` 契约。
2. 在 `adapters/__init__.py` 的显式装配表登记适配器类型。
3. 由适配器提供平台名称、启用状态、推荐频道、标识解析、订单规范化及平台自身限制。

无需修改通用商品/授权/推广/订单流程，也不需要在 API 的平台枚举中加分支。平台参数只验证安全的代码格式，实际可用性由注册表判断。

`GET /api/home/platforms` 返回 `{status, data: [{code, name, active}]}`；前端平台筛选消费这份元信息。`GET /api/home/channels?platform=pdd` 从适配器取得频道，原来不带参数的请求保持有效。现有淘宝/京东占位调用返回明确错误，不再生成假链接或假授权结果。`tb` 是 `taobao` 的兼容别名。

## 商品身份、缓存与存储

- `ProductRef` 将平台、真实 `goods_id`、`goods_sign` 分开保存。数字与加密标识的解释属于适配器，不由通用服务猜测。
- 缺失 ID、旧清洗器的 `0` 不是真实商品 ID。Sign-only 商品使用内部 `product_id = sign:<goods_sign>` 占位，兼容现有 `(platform, product_id)` 唯一索引；对外真实 ID 仍为空，Sign 保留。
- 后续取得真实 ID 时可通过同平台 Sign 更新原记录。摘要不带真实 ID 时，不会抹掉已存真实 ID。
- 详情查库始终包含平台和标识类型。缓存使用带平台、用户作用域、标识类型的 tuple，不再读写裸 ID 键。
- 同时提交 ID 与 Sign 不代表两者已被验证为同一商品。详情按主标识查询，只缓存返回数据确认的别名；转链的数字 ID 兜底只使用同平台仓储确认的关联。
- 摘要/详情共用持久化字段映射；摘要只更新实际提供的字段，不覆盖完整详情、品牌或类目。原 `goods_id/product_id`、`brand/brand_name` 等输出别名保持兼容。
- 已知零价格、零优惠、零佣金保持零，不补造 10% 佣金。未知保障、评分、券库存保持空，不提供虚构商业承诺。
- 完整详情使用独立的 UTC `detail_updated_at`，有效期由 `PRODUCT_DETAIL_TTL` 统一管理。推荐摘要的 `updated_at` 不会延长详情有效期；旧记录没有详情时间时会尝试官方刷新。
- 上游故障可返回 `source=database_fallback`，但不会把降级详情写入完整 TTL 的正常缓存，以便恢复后重试。
- 游客推广缓存仅在身份已知、链接有效且非降级时持久化到商品表；URL-only 转链只使用内存 URL 作用域缓存，不创建假商品身份。

保留既有 `products/users/orders` 集合及主要字段，不删除索引、不自动迁移历史数据。历史已被错误 ID 覆盖丢失的商品内容不能凭空恢复。

## 授权、推广与订单

- 手机号登录、令牌格式、旧密码哈希升级、脱敏输出及现有认证头保持兼容。资料、订单、备案仍以令牌 UID 为权威。
- 页面会话必须由 token 验证；URL uid、缓存手机号不再被当作登录身份。退出、换号、筛选后迟到的请求不能恢复旧账户或覆盖新结果。
- 已确认平台授权按 `AUTH_STATUS_TTL` 缓存；过期后复核。上游错误与“确认未授权”严格区分，错误不会覆盖已有授权状态。
- 推广保留匿名与分享 UID 的原有 API 行为。平台负责生成链接与文案；缺链接的伪成功和 `is_fallback` 降级结果不进入正常推广缓存。
- 详情页进入时不自动转链；用户主动购买/复制时才转链，并合并同一身份下的并发请求。
- 订单通过 Mongo `$facet` 对完整筛选集统计收益，展示明细另行限制为最新 100 条；收益不再只统计这 100 条。
- 退款/结算的统一布尔字段优先；旧文档缺失字段时，才使用对应适配器的旧状态兼容规则。数据库错误不再伪装成零收入。
- `days=0` 表示全部历史；后台同步仍非阻塞。响应补充 `sync_status/sync_error/synced_count/last_sync_attempt_time`，保留 `last_sync_time` 但只在完整窗口落库成功后推进。手动刷新不绕过冷却或锁。

### 保留的同步局限

仍然是 **最近 23 小时、默认每次最多 5 页、进程内节流/互斥** 的 best-effort 同步。达到预算会明确报告 `partial`，不会冒充完整成功，但本次没有引入持久化游标、长期离线补单或多 worker 分布式互斥。因此本次重构不等于解决所有历史漏单问题。

本地爆单库的 `total_count` 仍保留原探测下界语义；翻页请使用 `has_more`，不要将其当成精确总数。订单汇总的 `total_count` 则是完整筛选集计数。

## 运行与验证

以下命令均在 `app/mobile_web` 目录执行。

后端回归（`pytest.ini` 已处理项目导入路径）：

```bash
python -B -m pytest tests -q
```

前端无依赖回归（Node.js 内置 `node:test`、真实执行控制器与 deferred-fetch）：

```bash
node --test tests/frontend_regression.cjs
```

正常服务入口，读取项目现有配置并连接真实 Mongo/拼多多：

```bash
python -B -m uvicorn app.mobile_web.main:app --app-dir ../.. --host 127.0.0.1 --port 8000
```

浏览器离线回归入口：使用真实 HTTP 路由、服务与页面，仅替换仓储/平台边界为内存测试替身，不连接联盟、不写真实 Mongo。

```bash
python -B -m uvicorn app.mobile_web.tests.preview_app:app --app-dir ../.. --host 127.0.0.1 --port 8000
```

`.claude/launch.json` 的 `mobile-web-offline-smoke` 使用上述**测试入口**。测试商品、账户、订单、链接均为假数据，仅用于验证，不可作为生产入口部署。两个服务命令占用同一端口，不要同时运行。

### 上线注意

**上线前必须通过 `WEB_AUTH_SECRET` 或项目 `web_auth_secret` 配置私有、足够长的认证密钥。** 现有兼容默认值不适合生产；本次没有强制轮换密钥，以免未经部署协调使现有令牌全部失效。日志已对查询参数中的认证凭据脱敏。

本轮回归及浏览器验证使用本地替身，没有验证真实联盟权限、实时商品可用性或真实佣金结算，不应将离线测试通过等同于生产联调完成。
