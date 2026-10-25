# 基于 Python 与 NFC 的医学影像科急救药品管理系统

本科课程项目，当前完成第八阶段批量登记：保留单支管理和统一 NFC 领取，并支持管理员一次登记 1～100 支独立药品。
只管理急救药品；尚未开发实体 NFC 硬件读写或 Ubuntu/Caddy 部署。

每条药品记录代表一支实体，拥有唯一 UUID 和独立有效期。库存实时查询，不保存库存数字。
扫描只读，两种扫描都通过 `/api/nfc/dispense` 领取，每次一支、同一 UUID 仅成功一次。

## 文件职责

| 文件/目录 | 职责 |
| --- | --- |
| `schema.sql` | users、medicines、operation_logs 三张表及约束 |
| `queries.sql` | 29 条命名 SQL |
| `medicine_db.py` | 密码校验、角色权限、事务、药品及库存业务 |
| `data_io.py` | 药品与权限范围内日志 CSV 导出、公式防护、拒绝覆盖 |
| `test_medicine_db.py`、`test_data_io.py` | 原有 99 项标准库测试 |
| `backend/config.py` | 环境变量及启动配置校验 |
| `backend/auth.py` | JWT 签发、验证和当前用户/角色依赖 |
| `backend/schemas.py` | HTTP 输入校验与公开输出字段 |
| `backend/api.py` | HTTP 路由，调用原业务层与 CSV 导出 |
| `backend/main.py` | 应用工厂、数据库启动校验和统一错误处理 |
| `backend/errors.py` | 可以公开给客户端的错误类型 |
| `backend/cli.py` | 显式初始化、隐藏密码输入创建首位管理员 |
| `tests/test_api.py` | FastAPI TestClient 接口测试 |
| `tests/test_live_http.py` | 初始化工具测试、真实 Uvicorn + 本机 HTTP 测试 |
| `tests/test_regressions.py` | R1～R4 的 18 项回归测试 |
| `tests/test_system_fixes.py` | B1～B5 的事务、资料确认、快照及 HTTP 回归测试 |
| `tests/test_batch.py` | 批量登记数量、权限、整批回滚、并发及 NFC 回归测试 |
| `frontend/index.html`、`frontend/css/style.css` | 中文页面入口和桌面、手机响应式样式 |
| `frontend/js/api.js` | 唯一 Fetch 入口、Bearer、超时、错误和响应结构校验 |
| `frontend/js/auth.js` | 仅内存保存登录身份、验证当前角色、登录过期处理 |
| `frontend/js/ui.js` | 安全 DOM、表格、表单、弹窗和日期显示工具 |
| `frontend/js/views.js` | 总览、药品、库存、NFC、日志及用户页面 |
| `frontend/js/app.js` | 登录、导航、页面生命周期与重复提交保护 |
| `tests/test_frontend.py` | 首页、静态资源边界、API 兼容和业务集成测试 |
| `tests/frontend/` | 浏览器内运行的前端模块测试，无构建工具依赖 |
| `tests/frontend_preview.py` | 仅测试使用的临时库浏览器联调服务，不是另一套正式后端 |

调用关系：`浏览器 → 同源 FastAPI → MedicineDB / data_io → SQLite`。
接口层没有业务 SQL，也没有第二套 UUID、密码哈希、库存计数或领取事务。
业务层保留 B1～B5 修复及同事务批量登记；日志列表和新增CSV导出共用权限校验及逐行读取。SQL保持不变，完整性测试保留并更新经授权修改文件的 SHA-256。

## 安装、初始化和运行

本机实测 Python 3.14.7。代码使用 Python 3.11+；其他版本和 Ubuntu 尚未实机验证。
标准库业务层仍可单独运行测试，不要求安装 FastAPI。Web 层依赖在独立虚拟环境中安装。

```bash
cd '/Users/gyz/Documents/Python编程/期末个人/CODEX'
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

export MEDICINE_DB_PATH='medical.db'
export MEDICINE_JWT_SECRET="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export TOKEN_EXPIRE_MINUTES='30'
export SQLITE_TIMEOUT_SECONDS='5'

# 显式初始化，不创建默认账号；已有库只校验，不迁移、不重建。
.venv/bin/python -B -m backend.cli init-db

# 仅用户表为空时可执行；密码隐藏输入两次，无内置密码。
.venv/bin/python -B -m backend.cli create-admin --username admin

.venv/bin/python -B -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 8011
```

启动后打开 **http://127.0.0.1:8011/**，使用自己创建的账号登录网页。
Swagger 保留在 **http://127.0.0.1:8011/docs**，`/redoc` 和 `/openapi.json` 也保持可用。
已有进程需要重启才能加载新增的首页路由。前端无需 npm、Node.js 构建或额外开发服务器。
登录使用 JSON，不是 OAuth2 表单；没有公开注册、默认密码或公开首位管理员初始化接口。

应用启动必须有有效密钥和已经准备好的数据库。数据库路径错误或库不存在时拒绝启动，
不会替用户自动建一个空正式库。已有结构不匹配、空文件或坏库均拒绝，不自动迁移。
只导入模块不会创建数据库；不要通过运行测试来准备演示账号。

## 环境变量

| 变量 | 默认值/要求 |
| --- | --- |
| `MEDICINE_DB_PATH` | `medical.db`；相对路径以项目目录为基准 |
| `MEDICINE_JWT_SECRET` | 必填，至少 32 字节；使用随机值，后续启动保持一致 |
| `TOKEN_EXPIRE_MINUTES` | 30，允许 1～1440 分钟 |
| `SQLITE_TIMEOUT_SECONDS` | 5，允许 0～300 秒 |

`.env.example` 只是配置清单，程序不自动加载 `.env`。不要提交密钥或真实数据库。
网页与 API 由同一个 FastAPI 服务提供，无跨域配置，无外部 CDN 前端资源。
只公开 `frontend/` 目录；数据库、Python 文件和测试夹具不属于公开资源。首页及 `/static/index.html` 等前端 HTML 入口统一设置 CSP，禁止跨来源 iframe 嵌入；文档和资源路径保持可用。

## 网页怎么使用

- **系统总览**：按后端数据展示药品种类、账面库存、可领取数量和已领取数量。
- **药品管理**：名称是精确查询；可以组合状态筛选，查看详情。管理员可逐支登记、编辑、启用和停用。
- **库存管理**：调用库存接口按名称分组，也可输入完整名称查询，再进入单支记录。
- **NFC 识别与领取**：模拟扫描或输入已有 UUID，两种方式共用确认领取流程。
- **操作日志**：管理员查看全部，工作人员仅查看本人；只读用户没有入口。支持用户 ID、药品 ID、操作类型的后端筛选。
- **用户管理**：仅管理员可查看列表、详情及创建账号，不提供修改密码、删除用户或更改角色。
- **药品CSV导出**：仅管理员可用，下载所有已登记药品，包括已领取和停用记录。
- **日志CSV导出**：日志页顶部点击“导出日志 CSV”；管理员导出全部，工作人员仅本人，只读用户禁止。导出不受当前页面筛选、排序或折叠影响。

管理员拥有管理与领取入口；工作人员可查询、识别、领取和查看本人日志；只读用户仅查询和识别。
页面入口会根据 `/api/auth/me` 返回的当前角色调整，服务端仍会独立校验所有权限。

模拟扫描只读取 UUID，随后显示药品资料。点击“确认领取”后，还需在弹窗中点击“确认领取 1 支”才实际提交。
取消不会提交领取请求。提交期间按钮锁定，成功后重新查询库存；409 会提示状态或资料已变化并要求重新扫描。
网络错误或超时不会自动重试写操作，也不会当作成功；请先重新扫描或刷新核实结果。

访问令牌仅保存在内存中，不使用 localStorage/sessionStorage，不保存密码、不把 JWT 放进 URL。
刷新页面需要重新登录；收到 401 或本地有效期届满时清空会话。切换账号会清空旧页面、弹窗和提示。
用户名去除首尾空白后须为 1～500 字符；历史超限账号不会被自动截断或改名，需要另行授权处理。

桌面使用侧栏和表格，手机使用可折叠导航及药品卡片。建议使用支持 ES 模块、Fetch 和原生 dialog 的现代浏览器。
本机浏览器视口检查不等同于 Windows 或 iPhone Safari 真机验证；手动 UUID 输入也不代表已实现硬件读卡。

## API 与权限

除登录外都要求 `Authorization: Bearer <access_token>`。
JSON 成功结果统一放在 `data` 中；CSV 直接作为附件下载。

| 方法 | 路径 | 权限 | 作用 |
| --- | --- | --- | --- |
| POST | `/api/auth/login` | 未登录 | 验证原有密码哈希、签发 JWT |
| GET | `/api/auth/me` | 三种角色 | 当前用户，无密码哈希 |
| GET | `/api/users` | admin | 用户列表 |
| POST | `/api/users` | admin | 创建用户，可指定合法角色 |
| GET | `/api/users/{user_id}` | admin | 查询指定用户 |
| GET | `/api/medicines` | 三种角色 | 名称、状态、有效期等现有查询 |
| GET | `/api/medicines/{medicine_id}` | 三种角色 | 查询单支药品 |
| POST | `/api/medicines` | admin | 登记一支药品，UUID 由业务层生成 |
| POST | `/api/medicines/batch` | admin | 同一事务登记 1～100 支药品及逐支日志 |
| PATCH | `/api/medicines/{medicine_id}` | admin | 编辑名称、位置、有效期 |
| POST | `/api/medicines/{medicine_id}/enable` | admin | 启用 |
| POST | `/api/medicines/{medicine_id}/disable` | admin | 停用 |
| GET | `/api/inventory` | 三种角色 | 全部名称的账面及可领取库存 |
| GET | `/api/dashboard` | 三种角色 | 同一个 SQLite 读事务中的总览快照 |
| GET | `/api/inventory/by-name` | 三种角色 | 指定名称的两种库存 |
| POST | `/api/nfc/scan` | 三种角色 | 按已有 UUID 只读识别 |
| POST | `/api/nfc/simulate` | 三种角色 | 按名称选择已有可领取 UUID，只读 |
| POST | `/api/nfc/dispense` | admin、staff | 唯一领取入口 |
| GET | `/api/logs` | admin 全部；staff 本人 | 操作日志及组合筛选 |
| GET | `/api/export/medicines` | admin | CSV 附件，响应后清理临时文件 |
| GET | `/api/export/logs` | admin 全部；staff 本人；viewer 403 | 全部权限内原始日志CSV附件，不接受范围或文件路径参数 |

药品列表参数：`name` 为精确名称，`category` 可取
`all/in_stock/dispensed/expired/expiring/disabled`，`days` 为临期期限，默认 30，允许 0～3650。
名称与状态组合时取原业务查询结果的交集，没有新增 SQL。
`/inventory/by-name` 使用必填 `name`；`/logs` 可组合 `user_id`、`medicine_id`、`action`。
日志中的 `user_id` 是筛选条件，实际操作人始终来自令牌对应用户。

登记 JSON：

```json
{"name":"肾上腺素","location":"虚构CT室急救柜","expires_on":"2030-12-31"}
```

编辑使用相同三个字段，**三项必须一起传入**。这是对现有原子业务方法的直接适配，
不在接口层读取旧值再拼凑部分更新，以避免并发覆盖。
禁止提交 `id`、`nfc_uuid`、`status`、`is_active` 或操作者身份字段。
已领取药品编辑返回 409；停用但在库的药品允许编辑。

扫描 JSON：`{"nfc_uuid":"登记时返回的UUID"}`。
扫描与模拟扫描的原有药品字段保留，另附 `business_date`（上海业务日期）和 `can_dispense`（识别时资格，与角色权限独立）。网页领取入口以服务器资格为准，不用浏览器日期决定。

**领取请求的必要兼容性调整：** `/api/nfc/dispense` 现在必须同时发送用户确认的三项原始资料：

```json
{"nfc_uuid":"登记时返回的UUID","name":"肾上腺素","location":"虚构CT室急救柜","expires_on":"2030-12-31"}
```

旧版仅 UUID 的 HTTP 领取请求返回 422，必须先扫描并更新客户端；没有允许浏览器绕过确认的旧路由。
最终 `BEGIN IMMEDIATE` 事务内比较名称、位置及有效期，变化时返回 409 / `details_changed`，不修改状态、不写领取日志，要求重新扫描确认。
Python `withdraw_by_nfc()` 保留旧的受信任内部调用方式；新增 `confirmed_details` 关键字参数启用比较。HTTP 路由始终传入完整确认资料，不提供降级选项。
原有 18 个接口的路径与方法全部保留；领取输入有上述必要调整，扫描输出新增两字段；总览及批量登记加入后为20个。本次新增日志CSV导出后合计21个，原20个接口的正常请求和响应契约保持不变。

`GET /api/dashboard` 返回 `business_date`、`inventory`、`medicines`、`registered_count`、`dispensed_count`，全部数据来自同一读事务。前端不再跨请求拼接总览。
模拟扫描 JSON：`{"name":"肾上腺素"}`。模拟查询不会预留药品；随后可能已被他人领取，
因此前端应显示药品并确认，再调用统一领取接口。未知 UUID 只返回 404，不登记。

账面库存包含过期和停用的在库实体；可领取库存还要求启用且未过期。
到期日当天仍有效，次日起过期，日期由业务层按上海时区生成。
登记、领取、启停的日志和状态变更使用原事务；资料编辑暂不写日志。
日志时间为 UTC。导出使用 UTF-8 BOM，固定七字段，并保留原有公式防护。
CSV 是一次性完整附件，不支持 Range 分段下载；此类请求返回统一 JSON 400。

## 批量登记药品

管理员在“药品管理”点击“批量登记药品”，填写名称、位置、有效期和数量（默认 1，范围 1～100）。
点击“核对登记信息”查看摘要，再点击“确认登记 N 支”才写入数据库；可以返回修改或取消。
原来的“登记药品”单支入口仍然保留。

`POST /api/medicines/batch` 请求示例：

```json
{"name":"肾上腺素","location":"虚构急救药品柜A","expires_on":"2027-12-31","quantity":10}
```

数量必须是整数，拒绝字符串、小数和布尔值；省略数量时为 1，省略位置时为“未指定”。
客户端不能提交 ID、UUID、领取状态或操作者。成功返回 201，`data` 包含 `created_count` 和 `medicines` 列表；
列表中每支药品均包含自己的 ID、NFC UUID 及原有资料字段。原有 19 个接口的请求和响应保持不变。

业务方法为 `add_medicines_batch(name, expires_on, quantity, user_id, location="未指定")`。
全部插入、逐支 `register` 日志和回读使用同一个 `BEGIN IMMEDIATE` 事务，复用原有命名 SQL。
只有整批提交成功才返回结果；UUID 冲突、中途插入或日志失败、提交失败均整批回滚，新增药品和日志均为 0。
日志每条 `quantity=1`；不新增库存字段或业务表，不改变每 UUID 仅领取一次的规则。

成功后显示实际数量，并按该药品名称重新查询独立记录。库存和总览没有前端缓存，进入相应页面时重新读取后端统计。
失败时保留输入，不自动重试。网络或响应异常时可能是服务端已提交而客户端未收到结果，页面要求先“刷新核实登记结果”，
禁止在原表单盲目再次提交；确认结果前不要重新登记同一批。

## 认证与错误

JWT 使用 PyJWT、固定 HS256，校验签名、到期时间、签发时间、签发者、受众和用户 ID。
令牌不保存授权角色，每次请求都从数据库读取当前角色；写操作还由业务层在事务内复查。
接口不接收客户端操作者 ID，不重新实现密码校验，不返回或记录明文密码和完整访问令牌。
公开响应模型只包含允许的字段；验证错误不回显原始输入。

```json
{"error":{"code":"already_dispensed","message":"药品已经领取"}}
```

| 状态码 | 语义 |
| --- | --- |
| 400 | 通过 HTTP 格式验证后的业务参数错误 |
| 401 | 登录失败、缺少/无效/过期令牌，含 `WWW-Authenticate: Bearer` |
| 403 | 当前角色无权限 |
| 404 | 药品/用户不存在，或模拟扫描没有可领取药品 |
| 409 | 重复领取、停用/过期领取、已领取资料编辑、唯一编号冲突 |
| 422 | 请求字段、格式、额外参数不合法 |
| 500 | 内部、日志写入或文件导出故障；不回传 SQL、路径或堆栈 |
| 503 | SQLite 写锁等待超时，含 `Retry-After: 1` |

失败领取绝不返回 200。重复启停返回 `changed=false`，不重复写日志。
没有令牌刷新或主动登出撤销接口；访问令牌按有效期失效，角色始终以数据库为准。

## 测试

```bash
# 原有 99 项回归，不依赖 Web 第三方包：
python3 -B -m unittest -v test_medicine_db test_data_io

# Web、初始化工具及真实 Uvicorn HTTP：
.venv/bin/python -B -m unittest -v tests.test_api tests.test_live_http tests.test_regressions

# 一次运行全部测试：
.venv/bin/python -B -m unittest -v test_medicine_db test_data_io tests.test_api tests.test_live_http tests.test_regressions tests.test_frontend tests.test_system_fixes tests.test_batch tests.test_log_export
.venv/bin/python -m pip check
```

全部数据库、账号、密钥、导出文件属于独立临时测试，结束后清理，不使用环境变量中的正式库。
真实 HTTP 测试在临时端口启动 Uvicorn，经过登录、登记、扫描、领取、日志、CSV 下载后停止进程。
本机测试使用虚构数据；没有声称已完成 iPhone 真机 NFC 验证。

第六阶段本机验收：175 项 Python 测试通过（原有 167 项及新增 8 项），浏览器模块测试
27 项通过。使用真实 FastAPI 和临时 SQLite 完成了三角色登录、登记、编辑、启停、扫描、
取消与确认领取、并发冲突、日志和 CSV 下载联调，并检查了过期会话、网络失败及异常响应。
320、390、768、1440 像素视口未出现整页水平溢出；尚未完成 Windows 或 iPhone Safari 真机测试。

第七阶段修复验收：193 项 Python 测试通过（原有 175 项保留，新增 18 项），浏览器模块测试
40 项通过（原有 27 项保留，新增 13 项）。新增测试先在旧代码上复现问题，再验证修复。
总览并发测试在临时 WAL 数据库上让另一个真实事务提交领取、登记或资料编辑，检查读快照一致。
另以隔离 HTTP 服务验证原有 18 个接口的三级权限、并发领取、浏览器旧确认拒绝后重新扫描领取、
隐藏字段查询、跨来源 iframe 和客户端日期偏差；没有使用正式数据库或 8011 服务。
普通药品列表仍显示有效期并支持后端过期筛选；缺少服务器业务日期时不使用客户端时钟添加“已过期”辅助标记。

第八阶段验收：211 项 Python 测试通过（保留原有 193 项，新增 18 项）；浏览器 JavaScript 测试
48 项通过（保留原有 40 项，新增 8 项）。覆盖 1、10、100 支登记、非法数量、权限、中途 UUID/日志失败、
真实 COMMIT 锁失败整批回滚、两个管理员并发、逐支 NFC 领取和原有接口回归。
真实浏览器双击最终确认只产生一批 10 支及 10 条登记日志；锁冲突时零新增并保留表单。
批量确认弹窗在 320、375、390、768、1024、1440px 视口下未出现水平溢出；不等同于 iPhone Safari 真机验证。
运行中的旧服务需要正常重启才能加载新增后端接口；本轮测试没有重启或操作正式 8011 服务。

实测直接依赖固定在 requirements 文件中；完整环境见 `requirements-lock.txt`。
当前 Starlette 对 httpx 测试适配会发出迁移到 httpx2 的弃用提示，未屏蔽提示；
当前 TestClient 和真实 HTTP 验证结果需分别查看，不将依赖提示当作业务通过证据。
CSV 正常完成、生成失败或发送异常都会清理；进程被强制杀死时仍可能遗留系统临时目录。

前端模块测试在真实浏览器中运行，不需要安装构建工具。可用独立临时目录启动测试服务：

```bash
TEST_ROOT="$(mktemp -d /tmp/medicine-browser.XXXXXX)"
.venv/bin/python -B -m tests.frontend_preview --root "$TEST_ROOT" --port 8012
```

访问 `http://127.0.0.1:8012/` 可使用虚构夹具进行浏览器联调；测试账号与密码仅定义在
`tests/frontend_preview.py` 中，正式网页没有内置账号。访问 `/__tests/index.html` 执行前端模块测试。
测试服务额外挂载的 `/__tests` 在正式应用中不存在。结束后停止该测试进程，再清理本次创建的临时目录。
该测试工具不可用于正式数据库目录；不要把测试账号、测试端口或故障注入当作正式部署配置。

实现参考：[FastAPI JWT](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)、
[PyJWT 验证 API](https://pyjwt.readthedocs.io/en/stable/api.html)、
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)。密码方案仍使用原业务层 PBKDF2。

## 操作日志CSV导出

`GET /api/export/logs` 使用现有Bearer认证，不接受查询参数：用户ID、角色、文件路径及前端筛选条件均不能控制导出范围。
服务端在同一SQLite读事务内重新核验当前用户角色，复用原有29条SQL中的日志查询，按UTC时间倒序、ID倒序逐行写入文件。
Python不保存全部日志列表；完整CSV生成后才返回附件，发送正常结束或发生异常都会清理本次临时目录。
浏览器使用带认证的Fetch取得Blob，不把JWT放在下载链接中；禁止重复点击并释放Blob URL。

附件名为 `操作日志_YYYY-MM-DD_HH-mm-ss.csv`，时间使用 `Asia/Shanghai` 业务时区。
编码为 `utf-8-sig`（UTF-8 BOM），标准CSV引用及换行，适合Excel/WPS按UTF-8读取；未将编码检查冒充实际软件打开验证。
表头固定为：日志ID、操作时间（北京时间）、操作时间（原始UTC）、操作用户ID、操作用户名、操作类型、药品ID、药品名称（当前）、NFC UUID（完整）、操作数量。

药品名称来自当前关联药品记录，不是操作发生时的历史名称；操作类型保持 `register/dispense/disable/enable` 原值。
操作数量保持原值，启停日志的数量不表示库存增减。异常旧UTC值保留在原始列，北京时间留空，绝不伪造时间。
文本公式风险仅在CSV副本中用单引号前缀防护，数据库原值不变。空日志仅输出表头，导出不写操作日志，不改变药品或库存。

本次增加后端路由，需要用户重启原FastAPI服务才能使用新接口，再刷新网页。
开发验证使用独立临时服务，不自动关闭或重启现有8011服务。

日志导出验证：Python全量235项通过（原211项及新增24项）；JavaScript全量92项通过（原81项及新增11项）。
使用独立临时数据库，覆盖20001条日志的逐行导出、正常及异常文件清理、真实浏览器管理员/工作人员下载、403/401和范围参数拒绝。
已验证UTF-8 BOM、CSV结构及中文往返读取，未在Excel/WPS桌面软件中实际打开验证。
