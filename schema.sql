-- 医学影像科急救药品与器材管理系统：全新数据库结构。
-- 只适用于新数据库；不迁移、不删除、不兼容旧表。
-- Python 每次建立连接后，都必须在开启事务前启用外键检查。
PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

-- 用户表：权限认证和密码哈希的生成、验证由 Python 后端负责。
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL, -- 只保存后端生成的完整密码哈希，不保存明文。
    role TEXT NOT NULL CHECK (role IN ('admin', 'staff', 'viewer'))
);

-- 药品表：一行对应一支实体药品；同名药品可以具有不同有效期。
-- UUID 由应用程序生成；标签只存 UUID，扫描时只查询、不自动登记。
CREATE TABLE medicines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nfc_uuid TEXT NOT NULL UNIQUE, -- 扫描标识；id 用于数据库内部关联。
    name TEXT NOT NULL, -- 同种药品由后端统一名称，以便统计库存。
    location TEXT NOT NULL DEFAULT '未指定',
    expires_on TEXT NOT NULL -- 有效期必须是实际存在的 YYYY-MM-DD 日期。
        CHECK (
            length(expires_on) = 10
            AND expires_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            AND substr(expires_on, 1, 4) BETWEEN '0001' AND '9999'
            AND date(expires_on, '+0 days') IS NOT NULL
            AND date(expires_on, '+0 days') = expires_on
        ),
    status TEXT NOT NULL DEFAULT 'in_stock'
        CHECK (status IN ('in_stock', 'dispensed')), -- 领取状态，与启用状态独立。
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

-- 操作日志表：关联具体操作者和具体一支药品，保留操作历史。
-- 外键使用 RESTRICT，禁止删除仍有关联日志的用户或药品。
-- 常规药品管理只停用，不提供物理删除业务。
CREATE TABLE operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK (action IN ('register', 'dispense', 'disable', 'enable')),
    quantity INTEGER NOT NULL
        CHECK (typeof(quantity) = 'integer' AND quantity > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, -- SQLite 默认记录 UTC 时间。
    CHECK (action <> 'dispense' OR quantity = 1) -- 每次领取恰好一支。
);

-- username、nfc_uuid 的 UNIQUE 约束已自动建立索引，无须重复创建。
-- 支持按名称统计账面库存、可领取数量，以及按有效期筛选药品。
CREATE INDEX idx_medicines_name_status_active_expiry
    ON medicines (name, status, is_active, expires_on);
CREATE INDEX idx_medicines_expires_on ON medicines (expires_on);

-- 支持日志筛选和外键关联检查。
CREATE INDEX idx_operation_logs_user_id ON operation_logs (user_id);
CREATE INDEX idx_operation_logs_medicine_id ON operation_logs (medicine_id);
CREATE INDEX idx_operation_logs_action ON operation_logs (action);

COMMIT;
