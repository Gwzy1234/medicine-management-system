-- 医学影像科急救药品与器材管理系统：命名 SQL 查询。
-- 每个 -- name: 后只有一条 SQL，Python 应按名称提取后用 execute 执行。
-- 不要把整个文件交给 executescript，也不要用字符串拼接传入参数。
-- 统一使用 :参数名 占位符；Python 绑定示例：{"nfc_uuid": uuid, "today": today}。
-- :today、:deadline 均由后端校验后传入 YYYY-MM-DD，使用同一业务时区。
-- 到期日当天仍有效；expires_on < :today 才是过期，不改变领取状态。
-- 每个连接均须在事务外执行 PRAGMA foreign_keys = ON。
-- 本文件不自动开启、提交事务，也不执行权限认证。
--
-- 登记流程（仅 admin）：后端生成 UUID，BEGIN IMMEDIATE，核验权限，
-- 执行 add_medicine，用同一游标的 lastrowid 获取 medicine_id，
-- 执行 add_operation_log（action=register、quantity=1），成功后 COMMIT。
-- 任一步失败都必须 ROLLBACK；实体 NFC 标签写入在本 SQL 范围之外。
--
-- 领取流程（admin / staff）：从已认证会话取得 user_id，BEGIN IMMEDIATE，
-- 用 get_user_role 在事务中核验权限，再执行 get_dispensable_medicine_by_nfc。
-- 查不到则失败；查到则保留 medicine_id，并执行 dispense_medicine_by_nfc。
-- 更新游标 rowcount 必须恰好为 1，才可执行 add_operation_log
-- （同一 medicine_id、action=dispense、quantity=1），最后 COMMIT。
-- 更新 0 行时不得写成功日志或返回成功；任何失败均须 ROLLBACK。
-- SQLite 同时只允许一个写事务；BEGIN IMMEDIATE 串行化并发领取。
-- 遇到数据库忙/锁超时应回滚后报错，或有限重试整个事务，不得返回成功。
-- 两种扫描入口共用 get_medicine_by_nfc 和上述领取流程；查询不到不登记。
--
-- 启用/停用（仅 admin）：后端也应在同一写事务中核验权限、执行更新，
-- 仅在 rowcount=1 时记录 enable/disable 日志（quantity=1），然后提交。
-- viewer 只允许查询。查询结果不能替代权限认证或事务内的条件更新。

-- name: create_user
-- 创建用户；参数：username、password_hash（完整密码哈希）、role。
INSERT INTO users (username, password, role)
VALUES (:username, :password_hash, :role);

-- name: get_user_by_username
-- 按用户名取得登录信息；password 仅供后端验证，不向前端返回。
SELECT id, username, password, role
FROM users
WHERE username = :username;

-- name: get_user_by_id
-- 按用户 ID 查询基本信息，不返回密码哈希；参数：user_id。
SELECT id, username, role
FROM users
WHERE id = :user_id;

-- name: get_user_role
-- 查询用户角色供后端核验权限；不存在的用户返回空结果；参数：user_id。
SELECT role
FROM users
WHERE id = :user_id;

-- name: get_users
-- 查询用户列表，不返回密码哈希，无参数。
SELECT id, username, role
FROM users
ORDER BY id;

-- name: add_medicine
-- 登记一支药品及 UUID；参数：nfc_uuid、name、location、expires_on。
-- 新药品默认在库且启用；位置未指定时后端传“未指定”。须与登记日志同事务。
INSERT INTO medicines (nfc_uuid, name, location, expires_on)
VALUES (:nfc_uuid, :name, :location, :expires_on);

-- name: get_medicine_by_id
-- 按内部 ID 查询单支药品，包含停用和已领取记录；参数：medicine_id。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE id = :medicine_id;

-- name: get_medicine_by_nfc
-- 真实 NFC 和 Web 模拟 NFC 共用：按 UUID 查询，不存在时返回空结果。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE nfc_uuid = :nfc_uuid;

-- name: get_medicines
-- 查询全部药品，包含在库、已领取、启用和停用记录，无参数。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
ORDER BY id;

-- name: get_medicines_by_name
-- 精确查询同名药品的各支记录；参数：name。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE name = :name
ORDER BY expires_on, id;

-- name: get_in_stock_medicines
-- 查询所有尚未领取的药品，包含过期和停用记录，无参数。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE status = 'in_stock'
ORDER BY name, expires_on, id;

-- name: get_dispensed_medicines
-- 查询所有已领取药品，无参数。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE status = 'dispensed'
ORDER BY id;

-- name: get_expired_medicines
-- 查询已过期药品，不限制领取和启用状态；参数：today。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE expires_on < :today
ORDER BY expires_on, id;

-- name: get_expiring_medicines
-- 查询从今天到截止日（含两端）到期的在库药品，包含停用记录。
-- 参数：today、deadline；后端计算截止日，并保证 deadline >= today。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE status = 'in_stock'
  AND expires_on BETWEEN :today AND :deadline
ORDER BY expires_on, id;

-- name: get_disabled_medicines
-- 查询全部停用药品，不限制领取状态，无参数。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE is_active = 0
ORDER BY id;

-- name: enable_medicine
-- 启用指定药品，不改变领取状态；参数：medicine_id。已启用时影响 0 行。
UPDATE medicines
SET is_active = 1
WHERE id = :medicine_id
  AND is_active = 0;

-- name: disable_medicine
-- 停用指定药品，保留历史，不改变领取状态；参数：medicine_id。
UPDATE medicines
SET is_active = 0
WHERE id = :medicine_id
  AND is_active = 1;

-- name: get_book_stock_by_name
-- 指定名称的账面库存：所有在库药品，包含停用、过期记录；参数：name。
SELECT COUNT(*) AS stock
FROM medicines
WHERE name = :name
  AND status = 'in_stock';

-- name: get_available_stock_by_name
-- 指定名称的可领取数量：在库、启用且未过期；参数：name、today。
SELECT COUNT(*) AS available_stock
FROM medicines
WHERE name = :name
  AND status = 'in_stock'
  AND is_active = 1
  AND expires_on >= :today;

-- name: get_stock_by_name
-- 按名称同时统计账面库存和可领取数量；参数：today。
-- 从全部药品分组，使仅剩已领取记录的名称仍能显示库存为 0。
SELECT name,
       SUM(CASE WHEN status = 'in_stock' THEN 1 ELSE 0 END) AS stock,
       SUM(CASE WHEN status = 'in_stock' AND is_active = 1
                     AND expires_on >= :today THEN 1 ELSE 0 END) AS available_stock
FROM medicines
GROUP BY name
ORDER BY name;

-- name: get_low_stock_medicines
-- 查询账面库存严格小于阈值的名称，包含库存为 0 的已有名称。
-- 参数：stock_threshold，由后端保证为非负整数；未曾登记的名称无法统计。
SELECT name,
       SUM(CASE WHEN status = 'in_stock' THEN 1 ELSE 0 END) AS stock
FROM medicines
GROUP BY name
HAVING SUM(CASE WHEN status = 'in_stock' THEN 1 ELSE 0 END) < :stock_threshold
ORDER BY stock, name;

-- name: get_dispensable_medicine_by_nfc
-- 查询 UUID 是否符合药品领取条件；参数：nfc_uuid、today。
-- 本查询只检查药品；用户角色仍由后端核验，领取时还须执行条件更新。
SELECT id, nfc_uuid, name, location, expires_on, status, is_active
FROM medicines
WHERE nfc_uuid = :nfc_uuid
  AND status = 'in_stock'
  AND is_active = 1
  AND expires_on >= :today;

-- name: dispense_medicine_by_nfc
-- 原子领取一支：只能由在库变为已领取；参数：nfc_uuid、today。
-- 必须在写事务内执行。重复、过期、停用或不存在均影响 0 行。
-- rowcount=1 后才可写领取日志；日志失败时必须回滚本次状态更新。
UPDATE medicines
SET status = 'dispensed'
WHERE nfc_uuid = :nfc_uuid
  AND status = 'in_stock'
  AND is_active = 1
  AND expires_on >= :today;

-- name: add_operation_log
-- 插入日志；参数：user_id、medicine_id、action、quantity。
-- 与对应药品变更使用同一连接、同一事务。领取 quantity 必须为 1。
-- 登记、启用、停用每次也操作一支，后端传 quantity=1。
INSERT INTO operation_logs (user_id, medicine_id, action, quantity)
VALUES (:user_id, :medicine_id, :action, :quantity);

-- name: get_operation_logs
-- 查询全部日志，关联操作者及单支药品 UUID，按时间倒序，无参数。
SELECT l.id, l.user_id, u.username, l.medicine_id, m.nfc_uuid,
       m.name AS medicine_name, l.action, l.quantity, l.created_at
FROM operation_logs AS l
JOIN users AS u ON u.id = l.user_id
JOIN medicines AS m ON m.id = l.medicine_id
ORDER BY l.created_at DESC, l.id DESC;

-- name: get_operation_logs_by_user
-- 查询指定用户的日志；参数：user_id。
SELECT l.id, l.user_id, u.username, l.medicine_id, m.nfc_uuid,
       m.name AS medicine_name, l.action, l.quantity, l.created_at
FROM operation_logs AS l
JOIN users AS u ON u.id = l.user_id
JOIN medicines AS m ON m.id = l.medicine_id
WHERE l.user_id = :user_id
ORDER BY l.created_at DESC, l.id DESC;

-- name: get_operation_logs_by_medicine
-- 查询指定单支药品的日志；参数：medicine_id。
SELECT l.id, l.user_id, u.username, l.medicine_id, m.nfc_uuid,
       m.name AS medicine_name, l.action, l.quantity, l.created_at
FROM operation_logs AS l
JOIN users AS u ON u.id = l.user_id
JOIN medicines AS m ON m.id = l.medicine_id
WHERE l.medicine_id = :medicine_id
ORDER BY l.created_at DESC, l.id DESC;

-- name: get_operation_logs_by_action
-- 按操作类型查询日志；参数：action（register / dispense / disable / enable）。
SELECT l.id, l.user_id, u.username, l.medicine_id, m.nfc_uuid,
       m.name AS medicine_name, l.action, l.quantity, l.created_at
FROM operation_logs AS l
JOIN users AS u ON u.id = l.user_id
JOIN medicines AS m ON m.id = l.medicine_id
WHERE l.action = :action
ORDER BY l.created_at DESC, l.id DESC;

-- name: update_medicine_details
-- 按药品 ID 修改在库药品资料；参数：medicine_id、name、location、expires_on。
-- 已领取或不存在时影响 0 行；权限和详细参数校验由后端负责，本阶段不写修改日志。
UPDATE medicines
SET name = :name,
    location = :location,
    expires_on = :expires_on
WHERE id = :medicine_id
  AND status = 'in_stock';
