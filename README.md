# Redacted Records API

供机构公开发布脱敏记录的 HTTP 服务。维护人员凭服务密钥提交原文与敏感区间，发布时按 Unicode 码点区间合并遮蔽，生成**不可变快照**与**随机读取令牌**；公众仅凭有效令牌读取脱敏正文。

支持两种发布方式：

- **直接发布**（既有能力）：维护人员凭服务密钥直接发布指定版本；
- **受控发布**（申请-复核）：机构先把记录标记为受控，操作者用**各自的凭据与身份**提交指定历史版本的发布申请，由**另一名具复核资格者**批准或驳回（申请人不得复核）；批准时在同一事务生成不可变快照与新读取令牌。

已发布版本若不应继续公开，机构可凭服务密钥将对应**快照永久撤回**：撤回只作用于该快照，同记录其他版本继续可读；撤回后无论持有该快照的哪枚令牌，新开始的公开读取一律沿用模糊 `403 snapshot unavailable` 且不消耗读取次数；该版本再次直接发布、提交受控申请或批准撤回前已在审的申请均返回 `409`，待审申请仍可驳回。

机构发现某条记录需**临时阻断外部阅览**时，可凭服务密钥提交记录标识与**非空原因**将其**冻结**，并可查询冻结状态、解除冻结。冻结/解除重复操作幂等：**当前冻结期的原因与开始时间绝不被覆盖**，解除后再次冻结开启新冻结期。冻结覆盖该记录**现有及冻结期间新发布的全部快照**：冻结提交后开始的公开读取，即使用有效的单快照令牌或多快照共享凭证，也按既有 `403 snapshot unavailable` 拒绝，**不扣次数、不写成功读取审计**；共享凭证中**其他记录**的快照仍可读。解除冻结**只恢复原本仍有效的读取资格**——已撤回快照、过期/撤销/换发凭证、耗尽额度均不因解除而复活。冻结与并发读取按记录行锁保证**提交边界一致**，冻结原因**仅维护侧可见**；未知记录 `404`、空白原因 `422`、无效密钥 `401`。冻结不影响原有发布、复核与公开读取的鉴权与响应约定。

此外，机构可把**多个已发布快照一次性交给同一外部审阅者**：operator 用本人凭据提交多快照共享凭证申请（**至少两个互不重复的快照 ID**、可选到期策略与**正整数共享读取总额度**，提交时冻结清单），由**另一名 reviewer** 批准或驳回（申请人不得自审）；批准时**重新核对全部快照仍存在且未撤回**，任一项失效则整次批准失败、不签发凭证（申请仍可驳回）。批准成功后**一次性签发随机共享凭证**，绑定冻结清单并**共用读取额度**：凭证在**现有公开快照读取接口**上只能读取清单内快照的脱敏正文，沿用原响应与模糊 403；清单内某项快照后来撤回，只拒绝该项且**不扣额度**，其他项仍可读；到期、撤销或额度耗尽时全部拒绝。维护人员凭服务密钥可查询凭证清单、额度与状态并撤销。

发现读取凭证（单快照令牌或多快照共享凭证）**疑似泄露**时，维护人员可凭服务密钥向统一换发接口提交**原凭证与请求标识**，换发**新随机凭证**：响应返回新凭证、适用快照、**原绝对到期时刻**与**剩余额度**。仅**未撤销、未到期且额度未耗尽**的凭证可换发；新凭证继承原范围（适用快照/冻结清单）、到期时刻与**已用次数**（额度不重置），旧凭证在换发提交后立即不能再读取。相同原凭证与请求标识重试只返回首次换发结果（幂等，不再次换发）；不同请求标识并发换发同一凭证仅一次成功，其余 `409`。共享清单中个别快照已撤回时仍可换发，该项继续不可读；换发与同时进行的成功读取按事务先后结算，不重置也不超支额度。公开读取响应、模糊 403 与成功扣次规则保持不变。

机构需要审计已发布脱敏快照的**实际读取**：自启用起，单快照令牌或共享凭证的**每次成功读取**都与本次额度扣减在**同一事务**写入一条**不可变审计事件**，记录**事件标识、快照、时间、凭证类型（token/bundle）与不含凭证明文的稳定换发链标识**——同一凭证及任意次换发后的成功读取都归于**同一条链**；失败、清单外或已撤回快照的请求**不产生事件**。事件表只追加，数据库层拒绝任何改写/删除。维护人员凭服务密钥可按**快照 + 时间范围分页查询**事件，返回上述字段与下一页游标；分页在并发写入期间对查询起点前已提交的事件**不重复、不遗漏**，查询期间新提交的事件可留待下次查询。非法时间范围、游标或未知快照被明确拒绝；查询**不返回原文、脱敏正文或凭证明文**。

签发快照读取令牌时可**可选**地设置正整数**读取次数上限**（`max_reads`）：直接发布在请求时确定，受控发布在提交申请时冻结、批准签发后生效。公开读取沿用既有脱敏正文响应，每次凭有效令牌**成功读取**消耗一次；无效、撤销、到期、快照已撤回、令牌与快照不匹配或已达上限的请求不消耗次数。

机构维护人员可凭服务密钥**原子替换并查询当前启用的敏感短语清单**（短语非空、清单内不重复；**空清单表示停用核验**）。直接发布与受控申请提交都按**拟发布版本的原文**做核验：对原文按 **Unicode 码点、区分大小写的字面量**（非正则）查找命中，**每个命中区间都必须被提交的遮蔽区间完整覆盖**，否则整次操作 `422` 拒绝，**不产生快照或令牌**，错误响应**只回未覆盖命中的码点位置，绝不回显短语或原文**。批准受控申请时，还要用**当时启用的清单**对**申请冻结的原文与区间**重新核验；不合格则申请保持待审（仍可驳回）。清单变更与发布并发时，发布事务对清单单行加行锁，**按同一已提交清单版本完成核验与提交**。清单变更**不改变已发布快照与既有凭证**，原有发布鉴权、成功响应与公开读取响应完全沿用既有约定。

机构还可为快照设定**统一的外部阅览时段**：直接发布或提交受控发布申请时，可随版本填写带时区的**开放时刻**（`opens_at`）与**截止时刻**（`closes_at`）——两者须同时填写且开放早于截止，提交时截止已到则拒绝；均不填则沿用现有无时段限制。受控申请在提交时**冻结**该时段；批准时若截止已到，拒绝批准且申请仍待审、可驳回。时段随**首次发布**写入不可变快照：同一记录版本的后续签发省略时段则沿用首次设置，显式填写不同设置一律 `409` 冲突拒绝，**快照不被改写**。公开读取仅在**开放时刻（含）起至截止时刻（不含）前**有效，以服务端资格判定时刻为准；窗口外对单快照令牌与共享凭证均沿用既有模糊 `403 snapshot unavailable`，**不扣次数、不写成功读取审计**，其他快照不受影响。撤回、冻结及凭证失效仍按原规则阻断；维护侧查询返回快照时段，未带时段的既有请求、公开成功响应与鉴权约定保持兼容。

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16
- 服务地址：`http://localhost:8000`（交互式文档：`http://localhost:8000/docs`）

## 一键启动

```bash
docker compose up --build
```

Compose 会启动 PostgreSQL 与 API 两个容器，自动完成依赖安装与数据库初始化。API 启动时在复用 `pgdata` 数据卷的前提下执行**有版本记录的数据库升级流程**（见下节），并带重试等待数据库就绪。数据持久化在命名卷 `pgdata` 中。

停止并清空数据：`docker compose down -v`（仅在确认要丢弃数据时使用；正常升级切勿加 `-v`）。

## 数据库版本化升级（复用已有数据卷）

服务把启动时的结构动作——**初始建表**、**历史缺列补齐（含已用额度与换发链标识回填）**、**审计事件防改写约束**、**记录冻结表**、**敏感短语配置**与**快照阅览时段列**——纳入有版本记录的单向升级流程，每次升级都有行可查、可核对。

### 版本记录与升级版本

当前结构版本记录在数据库表 `schema_migrations(version, name, applied_at)` 中：

| 版本 | 名称 | 内容 |
| --- | --- | --- |
| 1 | `initial_schema` | 初始建表：当前全部业务表（空库路径） |
| 2 | `historical_columns_and_rotation_chain` | 历史缺列补齐（受控标记、快照撤回列、令牌次数/换发列、共享凭证换发列、受控申请策略列、删除早期冗余列），并为既有凭证回填稳定换发链标识 `chain_id` |
| 3 | `audit_events_append_only` | 审计事件防改写约束：触发器拒绝任何 `UPDATE`/`DELETE`/`TRUNCATE` |
| 4 | `record_freezes` | 记录临时冻结表 `record_freezes` 与“每条记录至多一个当前冻结期”的部分唯一索引；只新增结构，既有记录默认未冻结、全部快照继续按既有资格可读 |
| 5 | `sensitive_phrases` | 敏感短语清单单行配置表 `sensitive_phrase_settings` 与初始“停用核验”行（空清单、版本 1）；只新增结构，既有发布/读取语义不变 |
| 6 | `snapshot_read_windows` | 快照统一外部阅览时段列：`snapshots` 与 `publish_requests` 各补 `opens_at`/`closes_at` 及“成对且开放早于截止”的 CHECK 约束；既有快照与在审申请一律为 NULL（无时段限制），语义不变 |

代码支持的最高版本为 6（`/readyz` 的 `supported_schema_version`）。**后续新增变更只能在升级链末尾追加新版本，不得改写历史版本。**

### 升级流程与保证

启动时（`uvicorn` lifespan）每个实例都执行同一套升级协调逻辑：

1. **等待数据库就绪**（按 `DB_CONNECT_RETRIES` / `DB_CONNECT_RETRY_DELAY_SECONDS` 重试）；
2. 获取一把固定键的 **PostgreSQL 会话级咨询锁**（`pg_advisory_lock`）：多个实例同时启动时，**只有一个实例（领导者）进入升级**，其余实例在锁上排队等待；
3. 判定起点：
   - **空库**（无任何业务表）：从 v1 开始依次建表、补齐、安装防改写触发器、建冻结表、建敏感短语配置表、补快照阅览时段列；
   - **升级框架出现前的旧库**（有业务表但无 `schema_migrations`）：把既有结构**补登为 v1 基线**（不重放建表、不重写数据），再增量执行 v2、v3、v4、v5、v6；
   - **已升级库**：只核对版本，不执行任何变更语句；
4. **每个版本独立事务**：升级语句、数据回填与该版本记录原子提交；任一语句失败则整个版本回滚且**不登记**，修复后重启可从失败版本安全重试；
5. 升级完成后核对库版本必须等于代码支持版本，然后释放咨询锁；排队的其余实例拿到锁后看到已是目标版本，仅做核对即放行。

数据与语义保证：

- **已完成步骤绝不重复执行、不重复改变数据**：版本记录与变更同事务提交，重启只做核对；链标识回填只作用于仍为 `NULL` 的行，已有 `chain_id`、`reads_used` 等一律不重写；
- 升级后**记录、不可变快照、凭证继续有效**；令牌/共享凭证的**已用额度沿用**（不重置）、到期/撤销/换发状态保留；**换发链标识稳定**（同凭证多次换发仍归同一链）；**审计事件原样保留**，且成功读取仍在扣次同事务写事件；
- **未知的较新版本**（库版本高于本代码支持版本）：拒绝降级、拒绝服务——所有业务路径返回 `503`，仅 `/livez` 可探活；必须先把代码升级到支持该库版本的版本再启动。

### 多实例部署要点

- 直接把 API 副本数扩到多个即可：咨询锁保证串行升级，等待实例在领导者完成后核对版本再开始接收请求；
- 编排系统应把 **`/readyz` 作为就绪探针**（仅就绪后放入流量），把 **`/livez` 作为存活探针**；升级期间或失败时 `/readyz`、`/healthz` 与所有业务接口均返回 `503 {"detail":"service unavailable: database schema is not ready"}`，不暴露鉴权或内部错误细节；
- 领导者进程在升级中途崩溃时，会话级咨询锁随数据库连接自动释放，未登记的失败版本已随事务回滚，新的实例可重新竞争锁并从该版本重试。

### 升级操作（标准流程）

```bash
# 1) 备份（强烈建议；复用数据卷升级前先留一份可回退的副本）
docker compose exec db pg_dump -U redact redact > backup-$(date +%F).sql

# 2) 用新代码启动（保留 pgdata 卷，不要加 -v）；可先滚动启动一个实例完成升级
docker compose up --build -d

# 3) 等待该实例就绪（返回 200 且 schema_version 为目标版本）
curl -fsS http://localhost:8000/readyz
# => {"status":"ready","schema_version":6,"supported_schema_version":6}

# 4) 核对升级记录
docker compose exec db psql -U redact -d redact \
  -c "SELECT version, name, applied_at FROM schema_migrations ORDER BY version;"

# 5) 再扩容/启动其余实例：它们只核对版本，不重复升级
```

升级是幂等的，对已处于目标版本的库再次启动不会执行任何变更语句。

### 失败恢复

- 升级失败时：失败版本整体回滚、未登记，服务进入失败态（业务路径 `503`、`/livez` 仍 200），日志给出失败版本与数据库错误；
- 查看 `docker compose logs api` 定位原因（常见：数据库不可达、权限不足、磁盘满、连接被中断）；
- **修复后重新启动同一实例即可重试**：已登记版本不重放，从首个未完成版本继续；成功后 `/readyz` 恢复 200；
- 如需人工回退，用升级前备份恢复：

```bash
docker compose down            # 停 API（不要 -v）
cat backup-YYYY-MM-DD.sql | docker compose exec -T db psql -U redact -d redact
docker compose up --build -d   # 用与备份匹配的旧代码启动
```

- 遇到 **`database schema version N is newer than this binary supports`**：说明该卷已被更新的代码升级过，当前是旧代码在启动。**不要手工删除 `schema_migrations` 行**（那会让已登记步骤被重放）；正确做法是换回不低于该库版本的代码镜像再启动。

### 手动核对版本（只读）

```sql
-- 当前已应用版本
SELECT version, name, applied_at FROM schema_migrations ORDER BY version;
-- 审计事件防改写触发器是否就位
SELECT tgname FROM pg_trigger
WHERE tgrelid = 'read_audit_events'::regclass AND tgname LIKE 'trg_read_audit_events_%';
```

## 配置（环境变量）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `SERVICE_KEY` | 维护人员服务密钥，**必填**，未配置服务拒绝启动 | 无（Compose 中为演示值 `dev-service-key-change-me`，生产务必修改） |
| `REVIEW_CREDENTIALS` | 受控发布操作者凭据，JSON 数组，见下 | `[]`（受控接口无人可用，不影响其他接口） |
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+psycopg2://redact:redact@localhost:5432/redact` |
| `DB_CONNECT_RETRIES` | 启动时等待数据库的重试次数 | `30` |
| `DB_CONNECT_RETRY_DELAY_SECONDS` | 重试间隔（秒） | `1.0` |

维护接口（创建、修订、直接发布、撤销、状态、标记受控）通过请求头 `X-Service-Key` 鉴权；缺失或不符一律 `401` 整次拒绝。

受控发布接口（提交、批准、驳回、查询申请）使用 **HTTP Basic 认证**（`Authorization: Basic base64(用户名:口令)`），按 `REVIEW_CREDENTIALS` 校验**操作者个人凭据**，并区分角色：

- `operator`：可提交发布申请；
- `reviewer`：可批准/驳回申请（申请人不得复核自己的申请）；
- 一人可同时具备两种角色（如机构的备用复核人）。

`REVIEW_CREDENTIALS` 为 JSON 数组，每项形如：

```json
[
  {"username": "alice", "password": "强口令A", "roles": ["operator"]},
  {"username": "bob",   "password": "强口令B", "roles": ["reviewer"]},
  {"username": "carol", "password": "强口令C", "roles": ["operator", "reviewer"]}
]
```

服务启动时校验该配置：用户名非空且不重复、口令非空、角色只能是 `operator`/`reviewer` 且至少一个，非法配置会让服务**快速失败**（报错信息不含口令）。至少需配置一名 `operator` 与一名**不同于**常用申请人的 `reviewer`，否则申请无法被独立复核。生产环境请通过机密管理系统注入，不要把真实口令提交到仓库。

## API 一览

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/records` | 服务密钥 | 创建记录（版本 1），提交原文与敏感区间 |
| `PUT` | `/records/{record_id}` | 服务密钥 | 修订：携带 `base_version`，过期返回 `409` 与当前版本 |
| `PUT` | `/records/{record_id}/controlled` | 服务密钥 | 将记录设为受控发布（幂等） |
| `POST` | `/records/{record_id}/publish` | 服务密钥 | 直接发布指定版本（可带 `max_reads` 上限与 `opens_at`/`closes_at` 阅览时段）；**受控记录一律 `409` 拒绝** |
| `POST` | `/records/{record_id}/publish-requests` | operator | 提交受控发布申请（冻结指定历史版本内容、`max_reads` 与阅览时段） |
| `POST` | `/publish-requests/{request_id}/approve` | reviewer（非申请人） | 批准：同事务生成快照 + 新读取令牌（冻结的上限与时段生效；截止已到则 422 且申请仍待审） |
| `POST` | `/publish-requests/{request_id}/reject` | reviewer（非申请人） | 驳回（可带 `reason`），驳回为终态，不生成令牌或计数 |
| `GET` | `/publish-requests/{request_id}` | 任一已认证操作者 | 查询单个申请状态（含冻结的 `token_max_reads` 与阅览时段，不含原文） |
| `GET` | `/records/{record_id}/publish-requests` | 任一已认证操作者 | 列出某记录的全部申请（不含原文） |
| `GET` | `/tokens/{token}` | 服务密钥 | 查询令牌状态：上限 `max_reads`、已用 `reads_used`、剩余 `remaining_reads`（不含原文） |
| `POST` | `/tokens/{token}/revoke` | 服务密钥 | 撤销读取令牌（含受控发布签发的令牌） |
| `POST` | `/snapshots/{snapshot_id}/withdraw` | 服务密钥 | **永久撤回快照**（首次需带 `reason`）；重复撤回返回首次撤回时间与状态 |
| `GET` | `/records/{record_id}` | 服务密钥 | 查看当前版本、受控标记与快照列表（含阅览时段、撤回时间、原因与状态） |
| `POST` | `/records/{record_id}/freeze` | 服务密钥 | **临时冻结记录**（首次需非空白 `reason`）：覆盖现有及期间新发布的全部快照；重复冻结幂等、不覆盖原因/开始时间 |
| `GET` | `/records/{record_id}/freeze` | 服务密钥 | 查询当前冻结状态（当前冻结期开始时间、原因；未冻结时各字段为 null） |
| `POST` | `/records/{record_id}/unfreeze` | 服务密钥 | **解除冻结**（幂等）：只恢复原本仍有效的读取资格，不复活失效凭证/额度/已撤回快照 |
| `GET` | `/sensitive-phrases` | 服务密钥 | 查询当前启用的敏感短语清单（版本、短语、`enabled`；空清单即停用核验） |
| `PUT` | `/sensitive-phrases` | 服务密钥 | **原子替换**敏感短语清单（短语非空、不重复；`[]` 停用核验），版本 +1 |
| `POST` | `/bundle-requests` | operator | 提交多快照共享凭证申请：≥2 个互不重复快照 ID + 正整数 `max_reads`（可带到期策略），提交时冻结清单 |
| `POST` | `/bundle-requests/{request_id}/approve` | reviewer（非申请人） | 批准：重新核对全部快照仍存在且未撤回，全部有效才一次性签发随机共享凭证 |
| `POST` | `/bundle-requests/{request_id}/reject` | reviewer（非申请人） | 驳回共享凭证申请（可带 `reason`），终态，不签发凭证 |
| `GET` | `/bundle-requests/{request_id}` | 任一已认证操作者 | 查询单个共享凭证申请（冻结清单与额度，不含原文） |
| `GET` | `/bundle-requests` | 任一已认证操作者 | 列出全部共享凭证申请（不含原文） |
| `GET` | `/read-bundles` | 服务密钥 | 列出全部已签发共享凭证：清单、额度、状态（不含原文） |
| `GET` | `/read-bundles/{bundle_id}` | 服务密钥 | 查询单个共享凭证的清单、`max_reads`/`reads_used`/`remaining_reads` 与状态 |
| `POST` | `/read-bundles/{bundle_id}/revoke` | 服务密钥 | 撤销共享凭证（撤销后全部读取 403） |
| `POST` | `/credentials/rotate` | 服务密钥 | **泄露换发**：提交原凭证 + 请求标识，单快照令牌/多快照共享凭证统一换发为新随机凭证 |
| `GET` | `/snapshots/{snapshot_id}/read-events` | 服务密钥 | **成功读取审计**：按快照 + 时间范围分页查询不可变读取事件（游标翻页；不含原文/凭证） |
| `GET` | `/public/snapshots/{snapshot_id}?token=...` | 无（凭令牌或共享凭证） | 公开读取脱敏正文（单快照令牌与多快照共享凭证同一入口；成功读取同事务写审计事件） |
| `GET` | `/livez` | 无 | **存活探针**：进程存活即 200（即使数据库升级失败），返回 `upgrade_status` |
| `GET` | `/readyz` | 无 | **就绪探针**：仅当数据库升级完成且版本核对通过时 200，返回当前/支持的结构版本 |
| `GET` | `/healthz` | 无 | 健康检查（升级未就绪时返回 503） |

### 区间语义

- 左闭右开 `[start, end)`，位置按 **Unicode 码点**计（与 Python 字符串下标一致，BMP 之外的字符算 1 个码点）。
- 越界、为空或倒置的区间 → `422`，**整次拒绝**，不落库任何内容。
- 发布时合并**相交或相邻**的区间，每段替换为 `[已遮蔽]`，其余原文保留。

### 读取次数上限（`max_reads`）

签发读取令牌时可**可选**地携带正整数 `max_reads`（不传或为 `null` 表示**不限次**，既有及新令牌默认如此）：

- **确定/冻结时点**：直接发布在发布请求中确定；受控发布在 `POST .../publish-requests` 提交申请时**冻结**（申请详情以 `token_max_reads` 返回），复核耗时与后续修订不改变它，**批准签发后**才落到新令牌上；申请被驳回不生成令牌或计数。
- **计数口径**：每次凭**有效**令牌成功读取脱敏正文消耗一次；`reads_used` 只增不减。同一快照的**不同令牌各自独立计数**（同一版本重复发布会复用快照、签出新令牌）。
- **达上限后**：读取沿用既有模糊拒绝 `403 snapshot unavailable`，公开响应结构不变，不额外暴露“已用尽”这一原因。
- **不消耗次数的请求**：令牌无效、已撤销、已到期，或令牌与快照不匹配（含快照不存在）。
- **撤销/到期优先**：即使尚有剩余次数，撤销或到期后读取一律 `403`，不能因尚有次数而放行。
- **并发安全**：次数判定与递增由公开读取接口内的**同一条条件 `UPDATE`** 原子完成（`reads_used < max_reads` 命中行锁串行化），并发读取的成功次数绝不超过上限；撤销是独立的原子 `UPDATE`，提交后优先生效。
- **维护侧查询**：`GET /tokens/{token}`（服务密钥）返回 `max_reads` / `reads_used` / `remaining_reads`（不限次时后两者中的上限与剩余为 `null`）及 `active` 等元数据；该接口**不返回快照原文或脱敏正文**，未知令牌 `404`，缺密钥 `401`。

### 快照永久撤回

机构发现某个已发布版本不应继续公开时，凭服务密钥调用
`POST /snapshots/{snapshot_id}/withdraw`，请求体携带撤回原因 `{"reason": "..."}`：

- **响应**：`{"snapshot_id", "withdrawn_at", "status": "withdrawn"}`，其中 `withdrawn_at` 为**首次**撤回时间。
- **幂等**：重复撤回（即使原因不同或不带原因）返回**首次**撤回时间与状态，首次原因保留不变；并发撤回由条件 `UPDATE` 串行化，恰好一个首次结果。
- **作用域**：撤回只作用于该快照。同记录其他版本的快照与令牌继续可读；快照中的**历史脱敏正文、已批准的发布申请记录均继续留存**（维护侧可查）。
- **公开侧效果**：撤回提交后才开始的公开读取，无论持有该快照的**哪枚令牌**（或无效令牌），一律沿用既有模糊拒绝 `403 snapshot unavailable`；这些请求**不消耗**任何令牌的读取次数，公开响应也**不透露撤回原因**。
- **并发界限**：公开读取先对快照行加锁再判定，读取与撤回由数据库行锁串行化——撤回提交前已在读的请求按既有规则处理，撤回提交后开始的读取必定 403 且不计数。
- **发布冲突**：该版本
  - 再次**直接发布** → `409`（不会复用撤回快照签出新令牌）；
  - **提交受控申请** → `409`；
  - 撤回前已存在的**待审申请**，批准时 → `409`（不签发令牌），但**仍可驳回**；已批准申请记录继续留存。
- **状态码**：未知快照 `404`；缺失或错误服务密钥沿用 `401`；首次撤回缺少非空白 `reason` → `422`。
- **维护侧可见**：`GET /records/{record_id}` 的每个快照条目含 `status`（`published` / `withdrawn`）、`withdrawn_at`、`withdraw_reason`。

### 调用示例

```bash
# 1. 创建记录（版本 1）
curl -sS -X POST http://localhost:8000/records \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
        "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
        "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
      }'
# => {"record_id":"<RID>","version":1}

# 2. 修订（base_version 过期时返回 409 与当前版本）
curl -sS -X PUT http://localhost:8000/records/<RID> \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"base_version": 1, "text": "……新原文……", "intervals": [{"start": 0, "end": 3}]}'
# => {"record_id":"<RID>","version":2}
# 过期时 => 409 {"detail":{"message":"base_version is stale","current_version":2}}

# 3. 发布版本 2（可另加 "expires_in_seconds": 3600、"expires_at": "2026-10-01T00:00:00Z"、"max_reads": 5
#    与 "opens_at"/"closes_at" 阅览时段，见“快照统一外部阅览时段”一节）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"version": 2, "max_reads": 5}'
# => {"snapshot_id":"<SID>","version":2,"status":"published","token":"<TOKEN>","expires_at":null,"max_reads":5,
#     "opens_at":null,"closes_at":null}

# 3b. 维护侧查询已用/剩余次数（不限次令牌 max_reads 与 remaining_reads 为 null）
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"token":"<TOKEN>","snapshot_id":"<SID>","record_id":"<RID>","version":2,
#     "max_reads":5,"reads_used":0,"remaining_reads":5,"active":true, ...}

# 4. 公开读取（无需密钥，只返回脱敏正文）；前 5 次 200，第 6 次起 403 snapshot unavailable
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":2,"status":"published","redacted_text":"患者[已遮蔽]，……"}

# 5. 撤销令牌（撤销完成后，即使尚有剩余次数，新开始的读取一律 403）
curl -sS -X POST http://localhost:8000/tokens/<TOKEN>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"

# 6. 永久撤回快照（首次需带原因；重复撤回返回首次 withdrawn_at 与 status）
curl -sS -X POST http://localhost:8000/snapshots/<SID>/withdraw \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"reason":"发现该版本不应继续公开"}'
# => {"snapshot_id":"<SID>","withdrawn_at":"2026-09-30T12:00:00Z","status":"withdrawn"}
# 撤回后：公开读取（任意令牌）一律 403 snapshot unavailable 且不消耗次数；
#        该版本再次直接发布/提交受控申请/批准在审申请均 409，待审申请仍可驳回。
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => 403 {"detail":"snapshot unavailable"}
# 维护侧记录状态可见撤回时间与原因（公开侧不透露原因）
curl -sS http://localhost:8000/records/<RID> -H "X-Service-Key: dev-service-key-change-me"
# => {"snapshots":[{"version":2,"status":"withdrawn",
#                   "withdrawn_at":"...","withdraw_reason":"发现该版本不应继续公开", ...}, ...]}
```

完整端到端演示（含 401/409/422、快照不可变、`max_reads` 次数上限与独立计数、维护侧计数查询、撤销与到期）：

```bash
./examples/demo.sh        # 依赖 curl 与 jq；可用 BASE_URL / SERVICE_KEY 覆盖默认配置
```

完整快照撤回演示（含 401/404/422、重复撤回幂等、撤回后任意令牌 403 不计数、同记录其他版本可读、三类发布冲突 409、在审申请仍可驳回）：

```bash
./examples/demo_withdraw.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 记录临时冻结（临时阻断某条记录的外部阅览）

机构发现某条记录需临时阻断外部阅览时，凭服务密钥调用冻结接口；冻结是**临时**措施（与永久撤回不同），解除后原本仍有效的读取资格恢复。

### 接口与规则

- **冻结**：`POST /records/{record_id}/freeze`，请求体 `{"reason": "非空白原因"}`，响应：
  `{"record_id", "frozen": true, "frozen_at", "reason", "unfrozen_at": null, "already_frozen": false}`。
- **查询状态**：`GET /records/{record_id}/freeze`（服务密钥）。冻结中返回当前冻结期的开始时间与原因；未冻结时 `frozen=false`、`frozen_at`/`reason`/`unfrozen_at` 均为 `null`。历史冻结期在库内逐次留存，本接口只返回当前冻结期。
- **解除冻结**：`POST /records/{record_id}/unfreeze`（服务密钥），响应：
  `{"record_id", "frozen": false, "unfrozen_at", "already_unfrozen": false}`。
- **幂等与不可覆盖**：
  - 已在冻结期时重复冻结（无论是否带原因、原因是否不同）原样回放当前冻结期并返回 `already_frozen=true`，**当前冻结期的原因与开始时间绝不被覆盖**；
  - 未冻结时重复解除返回 `frozen=false`、`already_unfrozen=true`，不改变状态；
  - 解除后再次冻结开启**新的冻结期**（新的开始时间与原因），历史冻结期逐次留存。
- **覆盖范围**：冻结覆盖该记录**现有及冻结期间新发布的全部快照**（按记录生效，不按单个快照）。冻结**不阻止**维护侧发布或受控申请-复核——发布、复核照常完成，但其快照在冻结期间同样不可公开读取。
- **公开侧效果**：冻结提交后才开始的公开读取，即使用有效的**单快照令牌**或**多快照共享凭证**，一律沿用与撤回完全一致的模糊 `403 {"detail":"snapshot unavailable"}`；这些请求**不扣次数**（令牌/共享额度均不消耗）、**不写成功读取审计**，公开响应也**不透露冻结或原因**。
- **作用域**：冻结只作用于该记录。多快照共享凭证中属于**其他记录**的快照继续可读，读取其他记录不消耗被冻结记录的任何额度；凭证本身不失效、不撤销。
- **解除语义**：解除冻结只恢复“原本仍有效”的读取资格，随后的公开读取仍按各凭证既有规则判定——冻结期间**已撤回**的快照、已**过期/撤销/换发**的凭证、已**耗尽**的额度**均不复活**，解除不会重置任何计数。
- **并发界限**：公开读取先锁快照行（与撤回串行化），再锁**记录行**并在记录锁内核对当前冻结期；冻结先锁记录行再写入冻结行。冻结与读取由数据库行锁串行化，保证**提交边界一致**：冻结提交后开始的读取必定 403 且不计数，冻结提交前已在读的请求按既有规则处理。
- **状态码**：未知记录 → `404`（`record not found`，三个接口一致）；缺失/错误服务密钥 → `401`；首次冻结缺少非空白 `reason`（`{}`、`""`、纯空白）→ `422`。重复冻结不校验本次原因（幂等回放）。

### 调用示例

```bash
# 1. 冻结（首次需非空白原因）
curl -sS -X POST http://localhost:8000/records/<RID>/freeze \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"reason":"接到临时阻断要求，暂停该记录外部阅览"}'
# => {"record_id":"<RID>","frozen":true,"frozen_at":"2026-10-02T08:00:00Z",
#     "reason":"接到临时阻断要求，暂停该记录外部阅览","unfrozen_at":null,
#     "already_frozen":false}

# 2. 重复冻结（不同原因/空原因）幂等，already_frozen=true，原因与开始时间不变
curl -sS -X POST http://localhost:8000/records/<RID>/freeze \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"reason":"试图覆盖的另一个原因"}'
# => {"frozen":true,"already_frozen":true,"frozen_at":"<与首次完全一致>",
#     "reason":"接到临时阻断要求，暂停该记录外部阅览","unfrozen_at":null}

# 3. 查询当前冻结状态（原因仅维护侧可见）
curl -sS http://localhost:8000/records/<RID>/freeze -H "X-Service-Key: dev-service-key-change-me"
# => {"record_id":"<RID>","frozen":true,"frozen_at":"...","reason":"...","unfrozen_at":null}

# 4. 冻结提交后的公开读取：有效单快照令牌/共享凭证均按既有模糊拒绝，不扣次数、不写审计
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => 403 {"detail":"snapshot unavailable"}
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"max_reads":3,"reads_used":1,"remaining_reads":2,...}   # 阻断的 403 未消耗次数

# 5. 解除冻结（重复解除幂等）；原本仍有效的读取资格恢复
curl -sS -X POST http://localhost:8000/records/<RID>/unfreeze \
  -H "X-Service-Key: dev-service-key-change-me"
# => {"record_id":"<RID>","frozen":false,"unfrozen_at":"2026-10-02T09:00:00Z",
#     "already_unfrozen":false}
# 已撤回快照、已撤销/过期/换发凭证、已耗尽额度不复活；其他凭证恢复可读
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => 200 {"record_id":"<RID>","status":"published","redacted_text":"……"}

# 6. 明确拒绝：未知记录 404；首次冻结空白原因 422；缺/错密钥 401
# => 404 {"detail":"record not found"}
# => 422 {"detail":"reason is required when freezing a record"}
# => 401 {"detail":"missing or invalid service key"}
```

完整记录临时冻结演示（含 401/404/422、重复冻结不覆盖、有效令牌/共享凭证均 403 不扣次不写审计、共享凭证中其他记录仍可读、冻结期间新发布快照同样阻断、解除不复活已撤销凭证）：

```bash
./examples/demo_freeze.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 敏感短语清单（发布前字面量命中核验）

机构维护人员可维护一份**当前启用的敏感短语清单**，让服务在发布前对拟公开内容做最后一道核验：拟发布版本原文中的每个敏感短语命中，都必须已被提交的遮蔽区间**完整覆盖**，否则拒绝发布。

### 清单维护（服务密钥）

- **原子替换**：`PUT /sensitive-phrases`，请求体 `{"phrases": ["短语A", "短语B"]}`：
  - 每个短语须**非空**（最长 200 字符），清单内**不得重复**（区分大小写，`"a"` 与 `"A"` 是两个不同短语），最多 500 个；否则 `422`；
  - **空清单 `[]` 合法，表示停用核验**：停用后所有发布/申请不再做短语核验；
  - 整份清单在同一事务原子替换并把 `version` **+1**（即使新内容与旧内容相同也产生新版本）；并发替换由清单单行的行锁串行化。
- **查询**：`GET /sensitive-phrases`（服务密钥）返回：
  ```json
  {"version": 3, "phrases": ["短语A"], "enabled": true,
   "created_at": "…", "updated_at": "…"}
  ```
  `enabled` 是便捷标志（`phrases` 非空即 `true`；空清单停用核验时为 `false`）。初始行为空清单、`version=1`、`updated_at=null`。
- 鉴权沿用既有服务密钥约定：缺失/错误密钥一律 `401`。

### 核验规则

- **匹配方式**：对**拟发布版本的原文**按 **Unicode 码点**、**区分大小写**做**字面量**子串匹配（非正则，不做大小写折叠；BMP 之外的字符算 1 个码点）。允许重叠命中（短语 `"aaa"` 对原文 `"aaaa"` 命中 `[0,3)` 与 `[1,4)` 两处）。
- **覆盖判定**：提交的遮蔽区间（左闭右开，与发布脱敏同一套区间）先按相交/相邻合并；**每个命中区间都必须整体落在某段遮蔽区间内**才算覆盖，只盖住一部分（部分覆盖）也算不通过。
- **核验时点**：
  - **直接发布** `POST /records/{id}/publish`：按请求版本的原文与该版本区间核验；
  - **受控申请提交** `POST /records/{id}/publish-requests`：按拟发布历史版本的原文与区间核验，不通过则**不产生待审申请**；
  - **受控申请批准** `POST /publish-requests/{id}/approve`：除既有检查外，用**批准当时启用的清单**对**申请冻结的原文与区间**重新核验（等待复核期间清单可能已变更）；不通过则**不产生快照或令牌，申请保持待审且仍可驳回**。
  - 共享凭证申请/批准不再重复核验：它们引用的是**已发布快照的脱敏正文**，发布当时已核验。
- **并发一致性**：发布（提交/批准）事务先对清单单行 `SELECT … FOR UPDATE` 加锁并在锁内完成核验，行锁持有到本次提交。与清单替换并发时由同一把行锁串行化，保证**发布按同一已提交清单版本完成核验和提交**——替换先提交则发布在获锁后按新清单核验，发布先提交则本次不受替换影响。加锁顺序恒为“清单行 → 快照行”，与库内其他事务同序，不会死锁。
- **失败响应（不泄露内容）**：核验失败统一 `422`，且**只向有权调用方**（服务密钥的直接发布、持个人凭据的提交/批准）返回**未覆盖命中的码点区间**，**绝不回显任何短语或原文**：
  ```json
  {"detail": {"message": "sensitive phrase hits are not fully covered by redaction intervals",
              "hits": [{"start": 10, "end": 28}]}}
  ```
  不合格则**拒绝本次操作，不产生快照或令牌**；待审申请在批准阶段失败时保持待审、仍可驳回。
- **不回溯**：清单变更**不改变已发布快照（脱敏正文）与既有读取凭证**；已发布内容在清单变更后仍按既有公开读取约定访问，新清单只作用于之后的发布/提交/批准核验。
- 记录创建/修订本身不核验（核验只发生在发布环节，针对拟发布版本）。

### 状态码约定

| 场景 | 状态码 |
| --- | --- |
| 缺失/错误服务密钥（GET/PUT）；受控接口凭据问题沿用各自约定 | `401` |
| 清单含空短语/重复短语/超长；短语命中未被遮蔽区间完整覆盖（直接发布/提交/批准） | `422` |
| 既有发布冲突（受控记录、已撤回版本、申请已终态等）沿用各自约定 | `409` 等 |
| 查询/替换成功 | `200`；直接发布/申请提交成功沿用既有 `201` |

### 调用示例

```bash
BASE=http://localhost:8000
KEY=dev-service-key-change-me
AUTH=(-H "X-Service-Key: $KEY" -H "Content-Type: application/json")

# 1. 查询初始清单（空清单 = 停用核验）
curl -sS "$BASE/sensitive-phrases" "${AUTH[@]}"
# => {"version":1,"phrases":[],"enabled":false,"created_at":"…","updated_at":null}

# 2. 原子替换：启用两个敏感短语
curl -sS -X PUT "$BASE/sensitive-phrases" "${AUTH[@]}" \
  -d '{"phrases": ["110101199003077777", "张三"]}'
# => {"version":2,"phrases":["110101199003077777","张三"],"enabled":true,...}

# 非法清单：空短语 / 重复短语 => 422
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/sensitive-phrases" "${AUTH[@]}" -d '{"phrases":[""]}'
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/sensitive-phrases" "${AUTH[@]}" -d '{"phrases":["a","a"]}'

# 3. 创建记录（遮蔽姓名 [2,4) 与电话 [34,45)，未遮蔽身份证号）
RID=$(curl -sS -X POST "$BASE/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 34, "end": 45}]
}' | jq -r .record_id)

# 4. 直接发布：身份证号命中 [10,28) 未被完整覆盖 => 422，只回命中位置
curl -sS -X POST "$BASE/records/$RID/publish" "${AUTH[@]}" -d '{"version": 1}'
# => 422 {"detail":{"message":"sensitive phrase hits are not fully covered by redaction intervals",
#                   "hits":[{"start":10,"end":28}]}}   # 不回显短语或原文，且无快照/令牌产生

# 5. 遮蔽区间完整覆盖命中后发布成功（姓名/身份证号都在遮蔽区间内）
curl -sS -X PUT "$BASE/records/$RID" "${AUTH[@]}" -d '{
  "base_version": 1,
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}' >/dev/null
curl -sS -X POST "$BASE/records/$RID/publish" "${AUTH[@]}" -d '{"version": 2}'
# => 201 既有发布成功响应（snapshot_id/token/...），鉴权与响应结构不变

# 6. 停用核验：空清单后，未遮蔽任何短语的原文也可发布
curl -sS -X PUT "$BASE/sensitive-phrases" "${AUTH[@]}" -d '{"phrases": []}'

# 7. 受控申请：提交时按当前清单核验；批准时用“当时启用”的清单重新核验冻结原文
#    （等待期间若新清单命中未覆盖区域，批准 422，申请保持待审、仍可驳回）

# 8. 清单变更不影响已发布快照与既有令牌：公开读取响应完全不变
curl -sS "$BASE/public/snapshots/<SID>?token=<TOKEN>"
```

完整端到端演示（含 401/422、区分大小写、部分覆盖不通过、停用核验、受控提交/批准两次核验、失败仍可驳回、已发布内容不受清单变更影响、清单与发布并发的行锁串行化）：

```bash
./examples/demo_phrases.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 快照统一外部阅览时段（`opens_at` / `closes_at`）

机构可为快照设定**统一的外部阅览时段**：只有在这段时间内，公众才能凭有效凭证读取该快照的脱敏正文。时段是**快照级**属性（对该快照的全部令牌与共享凭证生效），随**首次发布**写入不可变快照，此后不可改写。

### 设置与冻结

- **填写位置**：直接发布 `POST /records/{id}/publish` 与受控申请提交 `POST /records/{id}/publish-requests` 的请求体可携带 `opens_at` 与 `closes_at`（ISO 8601，**带时区**的绝对时刻；naive 时间按 UTC 处理）。
- **校验规则**（违反一律 `422`，不产生快照、令牌或申请）：
  - 两者**必须同时填写**（只填一个 → `422`）；
  - **开放必须早于截止**（`opens_at < closes_at`）；
  - **提交时截止已到**（`closes_at` 不晚于服务端当前时刻）→ `422`；
  - 均不填写 → **无时段限制**（既有行为，默认如此）。
- **受控申请冻结时段**：申请在提交时冻结 `opens_at`/`closes_at`（申请详情 `GET /publish-requests/{id}` 返回这两个字段），复核耗时与后续修订不改变它；**批准时若截止已到** → `422`，**不产生快照或令牌，申请保持待审、仍可驳回**（与批准时的敏感短语重核同一语义）。

### 同一记录版本只保留首次发布的时段

时段随首次发布写入快照后**不可改写**（快照不可变的一部分）。该版本**已有快照**时的后续签发（直接再发布，或批准另一份受控申请）：

- **省略时段** → 沿用首次设置（签发响应与维护侧查询返回快照实际生效的时段）；
- **显式填写相同时段** → 正常签发新令牌（按**绝对时刻**比较，同一时段的不同时区写法不算不同）；
- **显式填写不同时段**（含首次未设时段、本次显式填写）→ `409` 冲突拒绝，**快照不被改写**；
- 受控申请在**提交时**即对照既有快照做该冲突检查（`409`）；**批准时再次核对**（等待期间同一版本可能已被另一份申请批准定版），冲突则 `409` 且申请保持待审、仍可驳回。

### 公开读取判定

- 设有时段的快照仅在 **`[opens_at, closes_at)`** 内可读（开放时刻含、截止时刻不含），以**服务端资格判定时刻**为准。
- **窗口外**（尚未开放或已过截止）：无论持该快照的**单快照令牌**还是**共享凭证**，一律沿用既有模糊 `403 {"detail":"snapshot unavailable"}`——**不扣任何次数/额度、不写成功读取审计**，公开响应不透露时段或原因。
- 时段按快照生效：同一共享凭证清单内的**其他快照**（无时段或仍在窗口内）不受影响，照常读取与计数。
- 与既有阻断规则叠加：**撤回、记录冻结、凭证撤销/到期/换发、额度耗尽仍按原规则阻断**；窗口只是额外的读取资格约束，不复活任何已失效资格。

### 维护侧可见与兼容

- `GET /records/{id}` 的每个快照条目、直接发布与批准签发的响应、受控申请详情均返回 `opens_at`/`closes_at`（无时段限制时为 `null`）。
- **兼容**：现有未带时段的请求行为完全不变；公开成功响应结构不变（**不含**时段字段）；各接口鉴权约定不变。

### 状态码约定（阅览时段）

| 场景 | 状态码 |
| --- | --- |
| 只填 `opens_at`/`closes_at` 之一；开放不早于截止；提交时截止已到 | `422` |
| 受控申请批准时，冻结时段的截止已到（申请保持待审、可驳回） | `422` |
| 该版本已有快照且显式填写不同时段（直接再发布 / 提交受控申请 / 批准时核对） | `409` |
| 窗口外的公开读取（单快照令牌与共享凭证；不扣次数、不写审计） | `403 snapshot unavailable` |
| 未带时段的既有请求 | 行为完全不变 |

### 调用示例

```bash
BASE=http://localhost:8000
KEY=dev-service-key-change-me
AUTH=(-H "X-Service-Key: $KEY" -H "Content-Type: application/json")

# 1. 带时段直接发布：开放 2026-10-01 09:00（+08:00），截止 2026-11-01 09:00（+08:00）
curl -sS -X POST "$BASE/records/<RID>/publish" "${AUTH[@]}" -d '{
  "version": 2, "max_reads": 100,
  "opens_at": "2026-10-01T09:00:00+08:00",
  "closes_at": "2026-11-01T09:00:00+08:00"
}'
# => {"snapshot_id":"<SID>","token":"<TOKEN>","max_reads":100,
#     "opens_at":"2026-10-01T01:00:00Z","closes_at":"2026-11-01T01:00:00Z", ...}

# 2. 窗口内公开读取（响应结构不变，不含时段字段）
curl -sS "$BASE/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":2,"status":"published","redacted_text":"……"}
# 窗口外（未到开放或已过截止）=> 403 {"detail":"snapshot unavailable"}，不扣次数、不写审计

# 3. 同一版本再签发：省略时段 -> 沿用首次设置
curl -sS -X POST "$BASE/records/<RID>/publish" "${AUTH[@]}" -d '{"version": 2}'
# => 201，opens_at/closes_at 与首次完全一致
#    显式填写不同时段 => 409 {"detail":"snapshot of this version already has a different read window"}

# 4. 维护侧查询快照时段
curl -sS "$BASE/records/<RID>" "${AUTH[@]}"
# => {"snapshots":[{"version":2,"opens_at":"2026-10-01T01:00:00Z",
#                   "closes_at":"2026-11-01T01:00:00Z","status":"published", ...}, ...]}

# 5. 受控发布：提交时冻结时段；批准时截止已到 -> 422，申请仍待审、可驳回
curl -sS -X POST "$BASE/records/<RID>/publish-requests" -u alice:alice-pw \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "opens_at": "2026-10-01T00:00:00Z", "closes_at": "2026-10-02T00:00:00Z"}'
curl -sS "$BASE/publish-requests/<PRID>" -u alice:alice-pw
# => {...,"opens_at":"2026-10-01T00:00:00Z","closes_at":"2026-10-02T00:00:00Z",...}（冻结的时段）
curl -sS -X POST "$BASE/publish-requests/<PRID>/approve" -u bob:bob-pw
# 若批准时截止已到 => 422 {"detail":"closes_at must be in the future"}，申请仍 pending，可驳回
```

完整端到端演示（含 422 校验、窗口内/外读取、不扣次数不写审计、省略沿用与不同时段 409、共享凭证窗口约束、受控冻结时段与批准后仍待审可驳回）：

```bash
./examples/demo_read_window.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 受控发布（申请-复核）

受控记录**不能**直接发布，必须走申请-复核流程。

### 语义与规则

1. **标记受控**：机构凭服务密钥 `PUT /records/{id}/controlled`（幂等）。受控记录的直接发布请求返回 `409`；非受控记录的受控申请同样返回 `409`（引导使用正确入口）。
2. **提交申请**：具备 `operator` 角色者用**本人凭据** `POST /records/{id}/publish-requests`，指定一个**历史版本**（`version`，可带 `expires_in_seconds`/`expires_at`、可选正整数 `max_reads` 与 `opens_at`/`closes_at` 阅览时段，规则与直接发布一致）。申请在提交时**冻结该版本的原文与敏感区间、读取次数上限与阅览时段**，后续修订只产生新版本，不改变待审内容、上限与时段。
3. **复核**：具备 `reviewer` 角色的**另一人**批准或驳回：
   - **申请人不得复核**自己的申请（即使其同时拥有 reviewer 角色）→ `403`；
   - 批准与驳回互斥：驳回后不能批准、批准后不能再驳回；对已终结申请再次操作 → `409`；
   - 并发复核由数据库行锁串行化，**同一申请只会产生一个终态**，落选方收到 `409`。
4. **批准事务**：沿用既有脱敏（合并相交/相邻区间→`[已遮蔽]`）与令牌（`secrets.token_urlsafe(32)`、可选到期）规则，在**同一事务**内生成不可变快照、写入新读取令牌（携带申请时冻结的 `max_reads`）并终结申请；任何一步失败整体回滚，**不会留下可读令牌或计数**。
5. **驳回**：只终结申请（可记录 `reason`），不生成快照、令牌或读取计数。
6. **拒绝不泄露原文**：无凭据/凭据错误 → `401`，无角色 → `403`，未知记录/版本/申请 → `404`，已终结/入口错误 → `409`；申请查询只返回元数据（含 `token_max_reads`），冻结原文绝不离开服务端。
7. 批准签发的令牌与直接发布令牌完全一致：遵守 `max_reads` 次数上限与到期规则，可用 `POST /tokens/{token}/revoke` 撤销，可用 `GET /tokens/{token}` 查询已用/剩余次数。

### 调用示例

```bash
# 0. 机构把记录设为受控（服务密钥；幂等）
curl -sS -X PUT http://localhost:8000/records/<RID>/controlled \
  -H "X-Service-Key: dev-service-key-change-me"
# => {"record_id":"<RID>","controlled":true}

# 0b. 受控记录的直接发布请求被明确拒绝（不生成快照）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"version": 1}'
# => 409 {"detail":"record is controlled; submit a publish request for review instead"}

# 1. 申请人 alice（operator）用本人凭据提交历史版本 1 的发布申请
#    max_reads 在申请时冻结，批准签发后生效（驳回则不生成令牌/计数）
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -u alice:alice-pw -H "Content-Type: application/json" \
  -d '{"version": 1, "max_reads": 3}'
# => {"request_id":"<PRID>","record_id":"<RID>","version":1,"status":"pending",
#     "requested_by":"alice","created_at":"..."}

# 1b. 申请详情可见冻结的上限（只含元数据，不含原文）
curl -sS http://localhost:8000/publish-requests/<PRID> -u alice:alice-pw
# => {...,"token_expires_in_seconds":null,"token_expires_at":null,"token_max_reads":3,...}

# 2. 申请人不能复核自己的申请
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve -u alice:alice-pw
# => 403 {"detail":"the requester may not review their own publish request"}

# 3. 另一人 bob（reviewer）批准：同事务生成不可变快照 + 新读取令牌
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve -u bob:bob-pw
# => {"request_id":"<PRID>","record_id":"<RID>","version":1,"status":"approved",
#     "reviewed_by":"bob","snapshot_id":"<SID>","token":"<TOKEN>","expires_at":null,"max_reads":3}

# 4. 公众凭令牌读取冻结版本的脱敏正文：恰好 3 次 200，第 4 次起 403 snapshot unavailable
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":1,"status":"published","redacted_text":"患者[已遮蔽]，……"}
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"max_reads":3,"reads_used":1,"remaining_reads":2,"active":true,...}（不含原文）

# —— 驳回路径 ——
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -u alice:alice-pw -H "Content-Type: application/json" -d '{"version": 2}'
# => {"request_id":"<PRID2>",...}
curl -sS -X POST http://localhost:8000/publish-requests/<PRID2>/reject \
  -u bob:bob-pw -H "Content-Type: application/json" -d '{"reason":"材料不全"}'
# => {"request_id":"<PRID2>","status":"rejected","reviewed_by":"bob","reason":"材料不全",...}
curl -sS -X POST http://localhost:8000/publish-requests/<PRID2>/approve -u bob:bob-pw
# => 409 {"detail":{"message":"publish request already decided","status":"rejected"}}

# 查询（任一已认证操作者；只返回元数据，不返回原文）
curl -sS http://localhost:8000/publish-requests/<PRID> -u alice:alice-pw
curl -sS http://localhost:8000/records/<RID>/publish-requests -u alice:alice-pw
```

完整受控流程演示（含 401/403/404/409、冻结内容与 `max_reads`、申请人回避、并发复核终态唯一、次数用尽与撤销）：

```bash
./examples/demo_controlled.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW / CAROL_PW 覆盖
```

### 状态码约定（受控接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误 Basic 凭据 | `401`（带 `WWW-Authenticate: Basic`） |
| 已认证但缺少所需角色（operator/reviewer） | `403` |
| 申请人复核自己的申请 | `403` |
| 未知记录 / 版本 / 申请 | `404` |
| 受控记录直接发布、非受控记录提交申请、对已终结申请再操作、提交/批准命中已撤回版本、令牌到期时间非法 | `409` / `422` |

## 多快照共享凭证（一次把多个快照交给同一外部审阅者）

机构需要把**多个已发布快照**交给同一名外部审阅者时，可申请一枚**跨快照共享凭证**：凭证仍在**现有公开读取接口** `GET /public/snapshots/{snapshot_id}?token=...` 使用，但只能读取**申请清单内**快照的脱敏正文，且所有成功读取**共用同一读取额度**。快照不要求来自受控发布——任何已存在且未撤回的快照都可入单。

### 流程与规则

1. **提交（operator）**：`POST /bundle-requests`，用申请人本人 Basic 凭据，请求体：
   - `snapshot_ids`：**至少两个、互不重复**的快照 ID（顺序即冻结清单顺序）；
   - `max_reads`：**正整数**共享成功读取**总额度**（必填，清单内所有快照共用）；
   - 可选 `expires_in_seconds` / `expires_at`（二选一，规则与令牌到期一致；不传则永不到期）。

   提交时即**冻结清单**：未知快照 → `404`，含已撤回快照 → `409`，少于两个/重复/额度非正整数 → `422`。
2. **复核（另一名 reviewer）**：
   - 申请人**不得自审**（即使同时具备 reviewer 角色）→ `403`；
   - 批准时**重新核对冻结清单的全部快照仍存在且未撤回**（对快照行加锁，与撤回串行化），**任一项失效则整次批准失败** → `409`，**不签发凭证**，申请保持待审、**仍可驳回**；
   - 批准与驳回互斥、终态唯一，并发复核由申请行行锁串行化（落选方 `409`）。
3. **一次性签发**：批准成功在**同一事务**内生成随机凭证（`secrets.token_urlsafe(32)`）、写入绑定的冻结清单并终结申请；任一步失败整体回滚。响应返回 `bundle_id`、`credential`、冻结 `snapshot_ids`、`expires_at` 与 `max_reads`。
4. **公开读取**：`GET /public/snapshots/{snapshot_id}?token=<共享凭证>`——与单快照令牌**同一入口、同一响应结构、同一模糊 `403 snapshot unavailable`**：
   - 凭证只能读取**清单内**快照；读取清单外快照（含快照不存在）→ `403`，不扣额度；
   - 每次**成功**读取扣一次**共享**额度（`reads_used` 跨清单累计，只增不减）；并发读取由同一条条件 `UPDATE` 原子判定并递增，成功总次数绝不超过 `max_reads`；
   - 清单内**某项快照后来撤回**：只拒绝该项（快照行锁阶段即 403），**不扣额度**，其他项仍可凭同一凭证读取；
   - **到期、撤销或额度耗尽**时，清单内全部快照一律 `403`；这些失败请求都不消耗额度。
5. **维护侧（服务密钥）**：`GET /read-bundles` 与 `GET /read-bundles/{bundle_id}` 返回凭证清单（每项含 `snapshot_id`、`record_id`、`version`、`position` 与当前 `status`/`withdrawn_at`）、`max_reads` / `reads_used` / `remaining_reads` / `active` 与到期、撤销时间，**不含任何原文**；`POST /read-bundles/{bundle_id}/revoke` 撤销凭证（幂等），撤销提交后新开始的读取全部 `403`。
6. **与既有能力兼容**：原有单快照令牌的读取、计数、查询（`/tokens/{token}`）、撤销与受控/直接发布接口的行为与响应**保持不变**；共享凭证是独立命名空间，不会出现在 `/tokens` 中，单快照令牌也不受共享额度影响。

### 调用示例

```bash
# 1. operator（alice）提交：冻结两个已发布快照 + 共享总额度 5（可另加 expires_in_seconds/expires_at）
curl -sS -X POST http://localhost:8000/bundle-requests \
  -u alice:alice-pw -H "Content-Type: application/json" \
  -d '{"snapshot_ids": ["<SID1>", "<SID2>"], "max_reads": 5}'
# => {"request_id":"<BRID>","status":"pending","snapshot_ids":["<SID1>","<SID2>"],
#     "requested_by":"alice","max_reads":5,"created_at":"..."}

# 申请详情：冻结清单与额度（不含原文）
curl -sS http://localhost:8000/bundle-requests/<BRID> -u alice:alice-pw

# 2. 申请人不得自审
curl -sS -X POST http://localhost:8000/bundle-requests/<BRID>/approve -u alice:alice-pw
# => 403 {"detail":"the requester may not review their own bundle request"}

# 3. 另一人 bob（reviewer）批准：重新核对清单全部有效后一次性签发凭证
curl -sS -X POST http://localhost:8000/bundle-requests/<BRID>/approve -u bob:bob-pw
# => {"request_id":"<BRID>","status":"approved","reviewed_by":"bob",
#     "bundle_id":"<BID>","credential":"<CRED>","snapshot_ids":["<SID1>","<SID2>"],
#     "expires_at":null,"max_reads":5}

# 4. 凭凭证在既有公开接口读取清单内脱敏正文（两个快照共用 5 次额度）
curl -sS "http://localhost:8000/public/snapshots/<SID1>?token=<CRED>"
# => {"record_id":"<RID1>","version":1,"status":"published","redacted_text":"……"}
curl -sS "http://localhost:8000/public/snapshots/<SID2>?token=<CRED>"
# 清单外快照（或不存在）=> 403 {"detail":"snapshot unavailable"}，不扣额度

# 5. 维护侧查询清单、额度与状态（不含原文）
curl -sS http://localhost:8000/read-bundles/<BID> -H "X-Service-Key: dev-service-key-change-me"
# => {"bundle_id":"<BID>","credential":"<CRED>","max_reads":5,"reads_used":2,
#     "remaining_reads":3,"active":true,"expires_at":null,"revoked_at":null,
#     "items":[{"snapshot_id":"<SID1>","record_id":"...","version":1,"position":0,
#               "status":"published","withdrawn_at":null}, ...]}

# 6. 清单中某项快照撤回：只拒绝该项、不扣额度，其他项仍可读
curl -sS -X POST http://localhost:8000/snapshots/<SID2>/withdraw \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"reason":"该版本不再对外提供"}'
curl -sS "http://localhost:8000/public/snapshots/<SID2>?token=<CRED>"   # => 403 snapshot unavailable（不扣额度）
curl -sS "http://localhost:8000/public/snapshots/<SID1>?token=<CRED>"   # => 200（继续可读）

# 7. 撤销共享凭证（撤销后全部 403）；驳回/终态与受控发布语义一致
curl -sS -X POST http://localhost:8000/read-bundles/<BID>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"
```

完整端到端演示（含 401/403/404/409/422、申请人回避、批准时清单失效整次失败仍可驳回、清单外读取不扣额度、撤回单项不扣额度、共用额度耗尽与并发不越限、到期与撤销、维护侧查询）：

```bash
./examples/demo_bundles.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW / CAROL_PW 覆盖
```

### 状态码约定（共享凭证接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误 Basic 凭据 | `401`（带 `WWW-Authenticate: Basic`） |
| 已认证但缺少 operator/reviewer 角色 | `403` |
| 申请人复核自己的申请 | `403` |
| 提交含未知快照；未知申请；维护侧查询未知凭证 | `404` |
| 提交/批准命中已撤回快照（批准时任一项失效）、对已终结申请再操作 | `409` |
| 清单少于两个、含重复 ID、`max_reads` 非正整数、到期时间非法、两种到期方式同传 | `422` |
| 凭证读取清单外快照、凭证无效/撤销/到期/额度耗尽、快照已撤回 | 公开侧统一 `403 snapshot unavailable` |

## 读取凭证泄露换发（单快照令牌 / 多快照共享凭证统一入口）

机构发现某枚读取凭证**疑似泄露**（例如令牌或共享凭证出现在不受控渠道）时，可凭服务密钥把它**换发**为一枚新随机凭证，旧凭证随即失效。**单快照读取令牌与多快照共享凭证使用同一个接口**，服务端自动识别凭证类型并返回对应内容。

### 流程与规则

1. **换发（服务密钥）**：`POST /credentials/rotate`，请求头带 `X-Service-Key`，请求体：
   - `credential`：**原凭证**（发布/批准时签发的随机令牌或共享凭证字符串，非空白）；
   - `request_id`：**请求标识**（调用方生成，建议 UUID，非空白，最长 200 字符）。
2. **换发资格**：仅**未撤销、未到期且额度未耗尽**的凭证可换发；否则返回 `409`（`detail.reason` 为 `revoked` / `expired` / `quota_exhausted` / `already_rotated`）。快照本身是否已撤回**不影响**换发资格。
3. **继承语义**：新凭证继承原凭证的
   - **范围**：单快照令牌仍绑定同一快照；共享凭证复制同一份**冻结清单**（顺序不变）；
   - **原绝对到期时刻**（`expires_at`，不延长、不重置；相对到期策略在首次签发时已换算为绝对时刻）；
   - **已用次数**（`reads_used`，只增不减，不因换发重置），因此响应里的**剩余额度**为 `max_reads - reads_used`；单快照令牌不限次时 `max_reads`/`remaining_reads` 为 `null`。
4. **旧凭证立即失效**：换发提交后，旧凭证在公开读取接口一律沿用模糊 `403 snapshot unavailable`，且这些失败请求**不消耗次数**；旧凭证的 `reads_used` 停在换发时刻。新凭证在同一入口 `GET /public/snapshots/{snapshot_id}?token=...` 使用，公开响应结构、模糊 403 与成功扣次规则完全不变。
5. **幂等**：相同（原凭证, 请求标识）重试（例如网络超时后重发）**原样返回首次换发结果**（同一枚新凭证、同一 `rotated_at`，并带 `"replayed": true`），**不会再次换发**。首次成功响应不含 `replayed`（即 `false`）。
6. **并发唯一**：同一原凭证以**不同**请求标识并发或重复换发时，数据库行锁串行化，**仅一次成功**，落选请求返回 `409`（`reason: "already_rotated"`）。
7. **与读取并发的结算**：换发按“先快照行、再凭证行”加锁，与公开读取的加锁顺序一致，换发与同时进行的成功读取**按事务先后结算**——换发提交前已获锁的成功读取计入旧凭证并被新凭证**继承**，换发提交后开始的读取用旧凭证一律 403；成功总次数绝不超过原上限（**不超支、不重置**），两者不会死锁。
8. **撤回的清单快照**：共享凭证冻结清单中**个别快照已撤回时仍可换发**，新凭证复制该清单后，**该项继续不可读**（读它仍 403 且不扣共享额度），其他项继续可读；响应 `items` 中逐项给出当前 `status`（`published` / `withdrawn`）。
9. **链式换发**：换发出的新凭证自身也是普通凭证，可再次凭（新凭证, 新请求标识）继续换发；前几代凭证均不可再读。

### 状态码约定（换发接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误服务密钥 | `401` |
| `credential` 或 `request_id` 缺失、空白或超长 | `422` |
| 原凭证既不是单快照令牌也不是共享凭证 | `404`（`credential not found`） |
| 已撤销 / 已到期 / 额度耗尽；不同请求标识对同一原凭证再次换发 | `409` |
| 相同（原凭证, 请求标识）重试 | `200`（幂等回放，`replayed: true`） |

> 换发不改变维护侧既有能力：旧凭证仍可查询（`GET /tokens/{token}`、`GET /read-bundles/{id}`，返回非空 `rotated_at`、`active=false`），也仍可调用撤销接口（幂等）。

### 调用示例

```bash
# 1. 怀疑令牌泄露：凭服务密钥提交原令牌与请求标识换发
curl -sS -X POST http://localhost:8000/credentials/rotate \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"credential": "<原令牌TOKEN>", "request_id": "req-20261001-0001"}'
# 单快照令牌 =>
# {"kind":"token","request_id":"req-20261001-0001",
#  "old_credential":"<原令牌TOKEN>","new_credential":"<新令牌NEW>",
#  "snapshot_ids":["<SID>"],"expires_at":"2026-10-01T12:00:00Z",
#  "max_reads":5,"reads_used":2,"remaining_reads":3,
#  "rotated_at":"2026-10-01T05:00:00Z","replayed":false,"items":null}

# 2. 旧令牌立即失效；新令牌在同一公开接口继续使用，沿用剩余额度
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<原令牌TOKEN>"
# => 403 {"detail":"snapshot unavailable"}（不计数）
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<新令牌NEW>"
# => 200 脱敏正文（继承已用 2 次，仅剩 3 次）

# 3. 相同（原凭证, 请求标识）重试：原样回放首次结果，不再次换发
curl -sS -X POST http://localhost:8000/credentials/rotate \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"credential": "<原令牌TOKEN>", "request_id": "req-20261001-0001"}'
# => 同一枚 new_credential，"replayed":true，rotated_at 与首次一致

# 4. 换发共享凭证：同样的请求体，响应 kind=bundle 并带冻结清单逐项状态
curl -sS -X POST http://localhost:8000/credentials/rotate \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"credential": "<原共享凭证CRED>", "request_id": "req-bundle-1"}'
# => {"kind":"bundle", "new_credential":"<新共享凭证>", "snapshot_ids":["<SID1>","<SID2>"],
#     "max_reads":5,"reads_used":2,"remaining_reads":3,"expires_at":null,
#     "items":[{"snapshot_id":"<SID1>","status":"published",...},
#              {"snapshot_id":"<SID2>","status":"withdrawn","withdrawn_at":"...",...}]}
# 清单内已撤回的 <SID2> 继续不可读、读取不扣额度；<SID1> 继续可读

# 5. 不可换发的情况：已撤销 / 已到期 / 额度耗尽 / 已被换发（不同请求标识）=> 409
# => 409 {"detail":{"message":"credential is not eligible for rotation","reason":"expired"}}
```

完整端到端演示（含 401/404/422/409、幂等回放、并发仅一次成功、继承到期/已用/剩余、撤回清单单项继续不可读、与读取并发结算不超支）：

```bash
./examples/demo_rotate.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 成功读取审计（不可变事件 + 换发链归并 + 分页查询）

机构需要掌握已发布脱敏快照的**实际读取**情况。自本功能启用起，每次凭单快照令牌或多快照共享凭证**成功读取**快照，都会与本次**额度扣减在同一事务**内追加一条**不可变审计事件**；维护人员可凭服务密钥按**快照与时间范围分页查询**。

### 事件记录与写入规则

每条事件仅含以下字段，**绝不记录凭证字符串本身，也不记录任何原文或脱敏正文**：

| 字段 | 含义 |
| --- | --- |
| `event_id` | 事件标识（写入时随机生成的 UUID） |
| `snapshot_id` | 被读取的快照 |
| `occurred_at` | 读取时间（与额度扣减同一事务，UTC） |
| `credential_kind` | 凭证类型：`token`（单快照令牌）/ `bundle`（多快照共享凭证） |
| `credential_chain_id` | **稳定换发链标识**（UUID）：同一凭证经任意次换发，新旧凭证共享同一标识 |

- **同事务原子性**：事件写入与 `reads_used` 扣次在同一事务，任一失败整体回滚——不存在“扣了次数没有事件”或“有事件没扣次数”。
- **只记录成功**：凭证无效/已撤销/已到期/已换发/额度耗尽、快照不存在或**已撤回**、共享凭证读取**清单外**快照等失败请求沿用原模糊 `403 snapshot unavailable`，**不扣额度也不产生事件**；公开响应结构与模糊 403 完全不变。
- **换发链归并**：凭证首次签发时生成随机 `chain_id`；换发出的新凭证**继承**该标识（令牌链与共享凭证链各自独立）。因此同一枚凭证在多次泄露换发前后的成功读取，在审计中都可按 `credential_chain_id` 归并到一条链；链标识是独立随机 UUID，**无法从中还原任何一代凭证**。旧凭证换发提交后即不可读，其失败读取不写事件。
- **不可变（只追加）**：应用不提供修改/删除入口；数据库在 `read_audit_events` 上安装触发器，拒绝任何 `UPDATE` / `DELETE` / `TRUNCATE`。

### 维护侧分页查询

`GET /snapshots/{snapshot_id}/read-events`（请求头 `X-Service-Key`），查询参数：

| 参数 | 说明 |
| --- | --- |
| `start` / `end` | **必填**，时间窗口，左闭右开 `[start, end)`，ISO 8601；naive 时间按 UTC 处理；`start >= end` 为非法窗口 |
| `limit` | 可选，每页事件数，`1..500`，默认 `100` |
| `cursor` | 可选，上一页响应中的 `next_cursor`（不透明，只能在同一快照、同一时间窗口与同一 `limit` 的后续页使用） |

响应：

```json
{
  "snapshot_id": "<SID>",
  "start": "2026-09-01T00:00:00Z",
  "end": "2026-10-01T00:00:00Z",
  "events": [
    {
      "event_id": "0ed25169-…",
      "snapshot_id": "<SID>",
      "occurred_at": "2026-09-30T12:00:00Z",
      "credential_kind": "token",
      "credential_chain_id": "57505735-…"
    }
  ],
  "next_cursor": "eyJ…" 
}
```

本页填满且仍有后续事件时 `next_cursor` 非空（原样回填到下次请求即可），否则为 `null`。

- **并发分页语义**：事件按库内单调序号（公开读取对快照行加锁、提交顺序即序号顺序）排列。首页以**查询开始时该快照已提交事件的最大序号为上界**，此后各页沿用该上界：窗口内、查询起点前已提交的事件跨页**既不重复也不遗漏**；查询**进行中**才提交的新事件可能落在上界之后，**留待下次查询**（重新从首页发起即可见到），不会造成页间重排。
- **状态码**：缺/错服务密钥 `401`；未知快照 `404`（不返回空页）；缺参数/时间不可解析/`start >= end`/`limit` 越界/游标非法或与路径快照、窗口、页大小不匹配，一律 `422`。
- **不泄露**：响应只含上表五个字段；不返回记录原文、快照脱敏正文、令牌或共享凭证字符串。已撤回快照在撤回前产生的历史事件仍可查询。

### 状态码约定（审计接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误服务密钥 | `401` |
| 未知快照 | `404` |
| 缺少 `start`/`end`、时间不可解析、`start >= end`、`limit` 越界、游标非法或与查询不匹配 | `422` |
| 成功 | `200`（事件可能为空页，`next_cursor` 为 `null`） |

### 调用示例

```bash
# 1. 成功读取两次（失败请求不写事件）
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>" >/dev/null
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>" >/dev/null
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=wrong"   # => 403，不写事件

# 2. 换发后用新凭证读取：事件仍归于同一条 credential_chain_id
NEW=$(curl -sS -X POST http://localhost:8000/credentials/rotate \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"credential":"<TOKEN>","request_id":"audit-1"}' | jq -r .new_credential)
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=${NEW}" >/dev/null

# 3. 按快照 + 时间范围分页查询（每页 2 条），只返回事件元数据与换发链标识
curl -sS -G http://localhost:8000/snapshots/<SID>/read-events \
  -H "X-Service-Key: dev-service-key-change-me" \
  --data-urlencode "start=2000-01-01T00:00:00Z" \
  --data-urlencode "end=2100-01-01T00:00:00Z" \
  --data-urlencode "limit=2"
# => {"snapshot_id":"<SID>", ..., "events":[{"event_id":"…","credential_kind":"token",
#     "credential_chain_id":"…", ...}], "next_cursor":"eyJ…"}

# 4. 沿游标翻下一页（窗口与 limit 必须与首页一致）
curl -sS -G http://localhost:8000/snapshots/<SID>/read-events \
  -H "X-Service-Key: dev-service-key-change-me" \
  --data-urlencode "start=2000-01-01T00:00:00Z" \
  --data-urlencode "end=2100-01-01T00:00:00Z" \
  --data-urlencode "limit=2" --data-urlencode "cursor=eyJ…"

# 5. 明确拒绝：未知快照 404；非法时间范围/游标 422
curl -sS -G http://localhost:8000/snapshots/<未知SID>/read-events \
  -H "X-Service-Key: dev-service-key-change-me" \
  --data-urlencode "start=2020-01-01T00:00:00Z" --data-urlencode "end=2021-01-01T00:00:00Z"
# => 404 {"detail":"snapshot not found"}
```

完整端到端演示（含成功/失败/清单外/撤回不写事件、多次换发同链、令牌与共享凭证两类事件、游标分页、401/404/422）：

```bash
./examples/demo_audit.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 升级后调用示例

以下示例演示：复用旧数据卷升级到当前版本后，**存量记录/快照/凭证继续可用、已用额度沿用、审计链连续**。设 `BASE=http://localhost:8000`、`KEY=dev-service-key-change-me`。

```bash
BASE=http://localhost:8000
KEY=dev-service-key-change-me

# 1) 探针：就绪后才放流量；livez 在升级失败时仍可探活
curl -fsS "$BASE/readyz"
# => {"status":"ready","schema_version":6,"supported_schema_version":6}
curl -s  "$BASE/livez"
# => {"status":"alive","upgrade_status":"ready"}
# 升级未就绪/失败时，除 /livez 外一律：
# => 503 {"detail":"service unavailable: database schema is not ready"}

# 2) 升级前发布的旧令牌仍可读取同一不可变快照（脱敏正文不变）
curl -s "$BASE/public/snapshots/<SID>?token=<旧TOKEN>"
# => {"record_id":"<RID>","version":1,"status":"published","redacted_text":"……脱敏正文……"}

# 3) 已用次数沿用、未被升级重置；读取成功后在同一事务 +1 并写一条审计事件
curl -s -H "X-Service-Key: $KEY" "$BASE/tokens/<旧TOKEN>"
# => {"token":"<旧TOKEN>","max_reads":5,"reads_used":2,"remaining_reads":3,"active":true,...}
curl -s "$BASE/public/snapshots/<SID>?token=<旧TOKEN>"     # => 200，reads_used 变 3
curl -s -H "X-Service-Key: $KEY" "$BASE/tokens/<旧TOKEN>"  # => "reads_used":3,"remaining_reads":2

# 4) 成功读取事件仍写入同一条换发链（升级前后 chain_id 一致，绝无凭证明文）
curl -s -H "X-Service-Key: $KEY" \
  "$BASE/snapshots/<SID>/read-events?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z"
# => {"snapshot_id":"<SID>","events":[{"event_id":"…","credential_kind":"token",
#       "credential_chain_id":"<升级前后相同的链标识>","occurred_at":"…"}, ...],"next_cursor":null}

# 5) 旧凭证可继续走泄露换发；新凭证继承范围、到期时刻与已用次数，仍属同一条链
curl -s -X POST "$BASE/credentials/rotate" -H "X-Service-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"credential":"<旧TOKEN>","request_id":"post-upgrade-0001"}'
# => {"kind":"token","new_credential":"<新TOKEN>","max_reads":5,"reads_used":3,
#     "remaining_reads":2,"replayed":false,...}

# 6) 鉴权与错误响应完全兼容：维护侧无密钥仍 401，失效凭证仍模糊 403
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/records/<RID>"                       # => 401
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/public/snapshots/<SID>?token=bad"   # => 403

# 7) v4 新增的记录冻结即可使用：存量记录升级后默认未冻结（frozen=false），
#    冻结提交后该记录现有/期间新发布快照的公开读取一律 403 且不扣次，解除恢复
curl -s -H "X-Service-Key: $KEY" "$BASE/records/<RID>/freeze"
# => {"record_id":"<RID>","frozen":false,"frozen_at":null,"reason":null,"unfrozen_at":null}
```

## 设计要点

- **不可变快照**：发布时把脱敏结果写入 `snapshots`（按 `(record_id, version)` 唯一），公开读取只访问快照；后续修订写入新版本，旧快照不变。
- **并发安全**：
  - 修订用条件更新 `UPDATE ... WHERE current_version = :base_version`，原子比较并递增，并发修订只有一个成功，其余收到 `409` 与当前版本；
  - 发布用 `INSERT ... ON CONFLICT DO NOTHING` 保证同一版本只有一份快照，快照与令牌同事务提交，不存在部分发布；
  - 受控复核用 `SELECT ... FOR UPDATE` 锁定申请行串行化并发批准/驳回，配合终态检查保证同一申请只有一个终态；
  - 公开读取的**有效性判定与次数消耗合并为同一条条件 `UPDATE ... WHERE 未撤销 AND 未到期 AND (不限次 OR reads_used < max_reads)`**，命中行即 `reads_used = reads_used + 1`：行锁串行化并发读取，成功次数绝不超过上限；撤销/到期不命中因而不消耗次数，且优先于“尚有剩余次数”；
  - 撤销是单条原子 `UPDATE`，提交后新开始的读取即被拒绝。
- **快照永久撤回**：
  - `snapshots` 增加 `withdrawn_at` / `withdraw_reason`：`withdrawn_at` 为空即仍公开，非空为撤回终态；撤回原因仅维护侧返回，公开侧永不透露；
  - 撤回是一条条件 `UPDATE ... WHERE withdrawn_at IS NULL`，并发撤回只有一个首次结果，落选方读回首次时间与状态，重复撤回完全幂等；
  - 公开读取先 `SELECT ... FOR UPDATE` 锁定快照行再判定，撤回与读取按快照行锁串行化：撤回提交后开始的读取在计数 `UPDATE` 之前就收到模糊 403，**不返回正文也不消耗次数**；
  - 直接发布/受控批准在写令牌前以 `FOR UPDATE` 取快照并检查撤回状态，提交受控申请在写申请前检查；已撤回版本的重新公开一律 `409`，但撤回前已在审的申请仍可驳回；
  - 撤回不删除任何数据：历史脱敏正文、同记录其他版本快照、已批准申请记录全部留存，仅公开读取被关闭。
- **不泄露原文**：公开接口只返回快照中的脱敏正文；令牌无效/撤销/到期/次数用尽/快照已撤回/记录已冻结/阅览时段窗口外/与快照不匹配统一返回模糊的 `403 snapshot unavailable`；维护侧令牌查询与受控申请查询只返回元数据与计数，错误信息不含原文内容，冻结/撤回原因仅维护侧可见。
- **令牌**：`secrets.token_urlsafe(32)` 随机生成；支持 `expires_in_seconds` 或 `expires_at`（二选一）设置到期时间，并支持可选正整数 `max_reads` 读取次数上限；到期、撤销或次数用尽后读取一律拒绝。直接发布与受控批准共用同一套令牌规则、计数、查询与撤销接口。
- **受控发布**：
  - 申请行保存**冻结副本** `frozen_text`/`frozen_intervals`，复核批准基于副本做脱敏，历史版本行即使变化也不影响待审内容；
  - 批准/驳回先 `SELECT ... FOR UPDATE` 锁定申请行，把并发复核串行化：先提交者写入终态，后提交者看到非 `pending` 收到 `409`，配合 `status` 检查约束，保证同一申请只有一个终态；
  - 批准时**快照、新令牌、申请终态在同一事务提交**，任一异常整体回滚，不会留下无主或可读令牌；驳回不产生快照/令牌；
  - 个人凭据（HTTP Basic）与机构服务密钥（`X-Service-Key`）分离，角色为 `operator`/`reviewer`，申请人与复核人身份按用户名强制不同；
  - 历史缺列补齐由**有版本记录的启动升级流程**完成（见“数据库版本化升级”一节）：旧库补登 v1 基线后增量执行 v2，幂等补列 `records.controlled`（旧记录默认非受控）、`read_tokens.max_reads`/`reads_used`（既有令牌 `max_reads` 为空即继续不限次、已用为 0）、`publish_requests.token_max_reads` 与 `snapshots.withdrawn_at`/`withdraw_reason`（既有快照默认未撤回、继续可读），并补列 `read_tokens.chain_id`/`read_bundles.chain_id`（仅对 NULL 行回填后收紧非空）；缺触发器/审计表由 v2、v3 一键补齐，冻结表由 v4 补齐，已登记版本不重放、不重写数据。
- **记录临时冻结**：
  - 新表 `record_freezes`（`record_id`、非空 `reason`、`frozen_at`、`unfrozen_at`）随 v4 迁移幂等创建，`unfrozen_at IS NULL` 上的**部分唯一索引**保证每条记录至多一个当前冻结期；冻结只插入新行、解除只回填 `unfrozen_at`，冻结期逐次留存且绝不更新或删除，当前冻结期的原因与开始时间因此**不可能被重复冻结覆盖**；
  - 冻结先 `SELECT ... FOR UPDATE` 锁记录行再核对/插入冻结行，重复冻结在记录锁内读到当前冻结期即幂等回放（`already_frozen=true`），并发冻结由记录行锁与部分唯一索引串行化，恰好一个首次结果；解除是一条原子条件 `UPDATE ... WHERE unfrozen_at IS NULL`，重复解除返回 `already_unfrozen=true`；
  - 公开读取在快照行锁（撤回判定）之后**再锁记录行**并在锁内以 `FOR UPDATE` 核对当前冻结期，锁序恒为“快照行 → 记录行 → 冻结行”，与冻结/换发事务同序，**串行化而不死锁**；命中冻结期即在任何计数 `UPDATE` 与审计写入之前返回模糊 `403`，**不扣次数、不写成功读取审计**，公开响应不透露冻结或原因；
  - 冻结按记录生效：覆盖该记录现有及冻结期间新发布的全部快照，却不影响共享凭证中**其他记录**的读取，也不撤销或改动任何凭证；解除不重置计数、不改凭证状态——已撤回快照、过期/撤销/换发凭证、耗尽额度均不复活。
- **敏感短语清单核验**：
  - 新表 `sensitive_phrase_settings` 为**全表单行**（固定 `id=1` + `CHECK (id=1)`），存放当前启用清单（JSONB 短语数组）与单调 `version`；v5 迁移幂等建表并插入初始空清单行（`phrases=[]`、`version=1`，即停用核验），既有数据与发布/读取语义不变；
  - 清单由维护接口凭服务密钥**原子替换**（整份数组在同一事务更新并 `version=version+1`），非空/不重复/非空元素由请求模型校验；查询返回版本、短语与 `enabled` 标志；
  - 匹配为按 **Unicode 码点、区分大小写的纯字面量**子串查找（`str.find`，非正则），每次命中只前进 1 个码点故**重叠命中全部报告**；提交区间按与脱敏一致的口径合并相交/相邻段，命中区间只有**整体落在某段内**才算被完整覆盖，部分覆盖也算未覆盖；
  - 直接发布、受控申请提交与批准在写任何快照/令牌/申请之前调用核验：直接发布/提交按拟发布版本原文，**批准额外用当时启用清单重新核验申请冻结的原文与区间**；不通过统一 `422`，响应 `detail.hits` **只含未覆盖命中的码点区间，绝不回显短语或原文**，且不产生快照/令牌，待审申请保持待审仍可驳回；
  - 发布（提交/批准）事务先 `SELECT … FOR UPDATE` 锁定清单单行并在锁内核验、持锁到提交：与清单替换由同一行锁串行化，保证**核验与提交对应同一已提交清单版本**；加锁顺序恒为“清单行 → 快照行”，与库内其他事务同序，不会死锁；
  - 清单变更只影响之后的发布核验，**不回溯**：已发布快照的脱敏正文、既有令牌/共享凭证及其读取、计数、审计完全不变；空清单即停用核验。
- **快照统一外部阅览时段**：
  - `snapshots` 与 `publish_requests` 随 v6 迁移各幂等补列 `opens_at`/`closes_at`（`TIMESTAMP WITH TIME ZONE`，可空）及“两列同空或同非空且开放早于截止”的 CHECK 约束；既有快照与在审申请一律 `NULL/NULL`（无时段限制），语义不变；
  - 直接发布与受控申请提交在服务端归一化（naive 按 UTC）并校验：成对、开放早于截止、提交时截止未到（`422`）；受控申请把时段**冻结**在申请行上，批准时复核“截止未到”（`422`，申请保持待审）后随首次发布落到新快照；
  - 快照插入改为 `INSERT ... ON CONFLICT DO NOTHING ... RETURNING id`：只有真正插入的一方是“首次发布”并以其时段定版；并发落选方与后续签发拿到既有快照行（`FOR UPDATE`），**省略时段即沿用、显式不同时段即 `409`**（按绝对时刻比较，不同时区写法不算不同），快照行永不被改写；
  - 公开读取在快照行锁（撤回判定）与记录行锁（冻结判定）之后、任何计数 `UPDATE` 与审计写入**之前**核对 `[opens_at, closes_at)`（以服务端当前时刻为准）：窗口外即返回与撤回/冻结完全一致的模糊 `403`，**不扣次数、不写成功读取审计**；时段按快照生效，共享凭证清单内其他快照不受影响；
  - 维护侧 `GET /records/{id}` 快照条目、直接发布/批准签发响应与受控申请详情返回时段字段；公开成功响应不含时段字段，未带时段的既有请求与鉴权约定完全兼容。
- **多快照共享凭证**：
  - 新表 `read_bundle_requests`（冻结清单 `frozen_snapshot_ids`、共享 `max_reads`、到期策略、申请/复核人与终态）、`read_bundles`（随机 `credential`、共享 `max_reads`/`reads_used`、到期与撤销）与 `read_bundle_items`（凭证-快照绑定与提交顺序）随 `create_all` 幂等创建，不改动既有表；
  - 清单在**提交时冻结**为 JSONB 快照 ID 数组（去重保序，至少 2 项，附 `jsonb_array_length >= 2` 与额度正整数检查约束）；批准时对全部快照行 `SELECT ... FOR UPDATE` **重新核对**仍存在且未撤回，任一失效即回滚本次批准、申请保持待审（仍可驳回）；
  - 凭证、清单项与申请终态在**同一事务**提交，随机凭证一次性签发；驳回不产生凭证；
  - 公开读取沿用快照行锁先判定撤回，再把“凭证有效 + 快照在清单内（`EXISTS` 子查询命中 `read_bundle_items`）+ 未撤销 + 未到期 + `reads_used < max_reads`”与共享计数递增合并为**同一条条件 `UPDATE`**：行锁串行化跨快照并发读取，成功总次数绝不超过共享上限；清单外/失效/到期/撤销/耗尽均不命中因而不扣额度，统一模糊 `403`；
  - 清单内某快照撤回只在该快照的行锁阶段拒绝该项、不触碰共享计数，其他清单项不受影响；撤销凭证是独立的原子条件 `UPDATE`，提交后优先生效；
  - 共享凭证与单快照令牌是**两套独立计数与命名空间**：公开接口先按单快照令牌原子判定，未命中再按共享凭证判定，故既有令牌的响应、计数与 `/tokens` 查询行为完全不变。
- **读取凭证泄露换发**：
  - 新表 `credential_rotations`（`kind` 区分 token/bundle、原/新凭证、请求标识、首次响应快照 JSONB）随 `create_all` 幂等创建；`read_tokens` 与 `read_bundles` 各幂等补列 `rotated_at`（既有凭证默认空、继续可读）；
  - 换发在**同一事务**内：先按快照行（令牌为其快照；共享凭证按冻结清单全部快照、按 ID 排序）`FOR UPDATE` 加锁，再对凭证行 `FOR UPDATE` 加锁并以 `populate_existing()` 读取锁等待后的最新已提交版本——锁序与公开读取的“快照行锁 → 条件 UPDATE”一致，换发与读取**串行化而不死锁**，按事务先后结算已用次数；
  - 新凭证复制原范围（同快照 / 同冻结清单同顺序）、原 `expires_at`、`max_reads` 与 `reads_used`，旧凭证置 `rotated_at` 后提交即不能再读；公开读取的两条条件 `UPDATE` 均增加 `rotated_at IS NULL`，旧凭证的失败读取不计数；
  - `(old_credential, request_id)` 唯一约束保证相同请求重试幂等：命中既有换发记录即回放保存的首次响应（`replayed=true`），不生成新凭证；不同请求标识的并发换发在凭证行锁后看到非空 `rotated_at`，仅一次成功、其余 `409`；
  - 资格（未撤销/未到期/额度未耗尽）在凭证行锁内按锁后最新值判定，换发与读取并发时不会重复扣次或超支；共享清单内已撤回快照不阻止换发（不重新核对清单），该项按既有规则继续 403 且不扣额度。
- **成功读取审计**：
  - 新表 `read_audit_events`（`id` 事件 UUID、库内单调递增 `seq` IDENTITY、`snapshot_id`、`occurred_at`、`credential_kind` 与 `credential_chain_id`）随 `create_all` 幂等创建；`read_tokens` / `read_bundles` 各幂等补列 `chain_id`（既有凭证逐行回填随机链标识后收紧非空，各自开启一条链）；不设快照外键，审计事件独立留存、不随任何状态变化级联；
  - 公开读取把两条条件 `UPDATE` 改为 `... RETURNING chain_id`：只有真正完成扣次的成功路径取回链标识，并在**同一事务**追加一条事件后提交；所有失败路径在写事件之前即结束（快照行锁阶段 403、令牌/共享凭证条件更新均未命中），因此**失败、清单外、已撤回请求不扣次也不写事件**；
  - 换发新凭证在继承范围、到期时刻与已用次数的同时**继承 `chain_id`**，同一凭证任意次换发前后的成功读取归于同一链；链标识为独立随机 UUID，事件与查询响应中**绝不出现凭证字符串或正文**；
  - 表为只追加：启动迁移幂等安装触发器函数与行级/语句级触发器，拒绝任何 `UPDATE` / `DELETE` / `TRUNCATE`，应用层也无修改或删除入口；
  - 维护侧 `GET /snapshots/{id}/read-events`（服务密钥）按 `(snapshot_id, 时间窗口)` 过滤、按 `seq` 升序分页（索引 `(snapshot_id, seq)`），不透明游标承载窗口、页大小、`after_seq` 与首页快照——首页以查询开始时该快照已提交事件的最大 `seq` 为上界，配合公开读取“快照行锁串行提交”的性质，保证起点前已提交事件跨页**不重复、不遗漏**，查询期间新提交事件可留待下次查询；未知快照 `404`、非法窗口/游标 `422`、缺密钥 `401`。
- **版本化数据库升级**：
  - 版本表 `schema_migrations(version, name, applied_at)` 与升级链 v1 建表 / v2 历史缺列补齐与链标识回填 / v3 审计防改写触发器 / v4 记录临时冻结表与部分唯一索引 / v5 敏感短语清单单行配置表与初始停用行 / v6 快照阅览时段列与成对校验约束；空库从 v1 依次执行，升级框架出现前的旧库补登 v1 基线后只增量执行 v2 及以后各版，已升级库只核对；
  - 启动先取固定键的 PostgreSQL 会话级**咨询锁**：多实例并发只有一个执行升级，其余等待并在拿到锁后核对版本；进程崩溃则锁随连接释放，失败版本随事务回滚、未登记，重启可重试；
  - **每个版本独立事务**：DDL/回填与版本记录原子提交，已完成步骤绝不重放；回填只作用于 NULL 行，存量记录/快照/凭证、已用额度、换发链标识与审计事件均保留；
  - 升级后核对版本必须等于代码支持版本；库版本高于代码（未知较新版本）或升级失败时进入失败态，中间件对除 `/livez` 外的全部路径返回 `503`（不暴露鉴权/内部细节），`/readyz` 供编排就绪探针，修复后重启可恢复服务。

## 本地开发

```bash
pip install -r requirements.txt
export SERVICE_KEY=dev-service-key-change-me
export DATABASE_URL=postgresql+psycopg2://redact:redact@localhost:5432/redact
# 可选：配置受控发布操作者（不配置则受控接口一律 401，其他功能不受影响）
export REVIEW_CREDENTIALS='[{"username":"alice","password":"alice-pw","roles":["operator"]},{"username":"bob","password":"bob-pw","roles":["reviewer"]}]'
uvicorn app.main:app --reload --port 8000

# 脱敏与敏感短语字面量核验核心逻辑单元测试（仅依赖标准库）
python3 -m unittest tests.test_redaction -v
# 凭据配置校验单元测试
python3 -m unittest tests.test_config -v
# 受控发布端到端集成测试：默认用 pgserver 内置 PostgreSQL 自动建临时库，
# 也可用 TEST_DATABASE_URL 指向已有数据库
python3 -m unittest tests.test_integration_api -v
# 版本化数据库升级测试：空库/旧库升级、数据与链标识保留、未知较新版本拒绝、
# 失败回滚可重试、多实例并发只有一个执行升级（真实 PostgreSQL）
python3 -m unittest tests.test_migrations -v
```

## 目录结构

```
app/
  main.py       # 路由与事务逻辑（受控发布、令牌计数、快照撤回、记录临时冻结、多快照共享凭证申请-复核与共用额度、凭证泄露换发、成功读取审计事件与分页查询、敏感短语清单维护与发布核验、快照统一外部阅览时段）；启动门禁与 /livez /readyz /healthz
  migrations.py # 有版本记录的数据库升级：schema_migrations 版本表 + 咨询锁；v1 建表 / v2 历史缺列补齐与链标识回填 / v3 审计防改写触发器 / v4 记录冻结表与部分唯一索引 / v5 敏感短语配置表 / v6 快照阅览时段列与校验约束
  models.py     # records / record_versions / snapshots(含撤回字段与阅览时段) / record_freezes(冻结期) / read_tokens(含换发链) / publish_requests(含冻结的阅览时段) / read_bundles(+requests/items，含换发链) / credential_rotations / read_audit_events(只追加) / sensitive_phrase_settings(启用清单单行)
  redaction.py  # 区间校验、合并、替换与敏感短语字面量命中/覆盖核验（纯函数）
  schemas.py    # 请求/响应模型（含撤回、记录冻结、max_reads、阅览时段、多快照共享凭证、泄露换发、读取审计与敏感短语清单模型）
  security.py   # 服务密钥与操作者个人凭据（Basic）鉴权、角色依赖
  config.py     # 环境变量配置（含 REVIEW_CREDENTIALS 校验）
  db.py         # 引擎与会话
examples/
  demo.sh             # 直接发布端到端调用示例
  demo_controlled.sh  # 受控发布（申请-复核）端到端调用示例
  demo_withdraw.sh    # 快照永久撤回端到端调用示例
  demo_freeze.sh      # 记录临时冻结（冻结/查询/解除 + 并发边界语义）端到端调用示例
  demo_bundles.sh     # 多快照共享凭证（申请-复核 + 共用额度）端到端调用示例
  demo_rotate.sh      # 读取凭证泄露换发（令牌/共享凭证统一入口）端到端调用示例
  demo_audit.sh       # 成功读取审计（不可变事件 + 换发链 + 游标分页）端到端调用示例
  demo_phrases.sh     # 敏感短语清单维护与发布/申请核验（含并发版本一致性）端到端调用示例
  demo_read_window.sh # 快照统一外部阅览时段（设置/冻结/继承/冲突 + 窗口内外读取）端到端调用示例
tests/
  test_redaction.py       # 脱敏逻辑与敏感短语字面量命中/覆盖核验单元测试
  test_config.py          # 操作者凭据配置校验单元测试
  test_integration_api.py # 受控/共享凭证/换发/读取审计/记录冻结/敏感短语清单/快照阅览时段/既有接口端到端集成测试（真实 PostgreSQL）
  test_migrations.py      # 版本化升级流程集成测试（空库/旧库/并发/失败恢复/未知新版本，真实 PostgreSQL）
Dockerfile / docker-compose.yml
```
