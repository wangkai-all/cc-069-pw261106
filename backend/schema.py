"""SQLite schema for the category evolution workbench.

Temporal model
--------------
Every membership (a product belonging to a category, or a category having a
parent category) is stored as one or more half-open intervals
``[effective_from, effective_to)``.  ``effective_to IS NULL`` means the row is
currently open.  Overlapping intervals for the same subject are forbidden (see
``temporal_core``), which guarantees a single parent / single category at any
point in time.  Intervals never mutate in place: a change closes the open row
and inserts a new one, so the full history is retained ("旧关系留档").
"""

from __future__ import annotations

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 分类节点（本身不带版本；节点的名称/层级关系变化由下面的版本表记录）
CREATE TABLE IF NOT EXISTS categories (
    id              TEXT PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'normal' CHECK (kind IN ('root','normal','merged','retired')),
    effective_from  TEXT NOT NULL DEFAULT '1970-01-01',  -- 业务生效日（节点何时存在）
    retired_at      TEXT,                                -- 停用/合并生效日（NULL=在当前视角有效）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    note            TEXT
);

-- 分类树的边（父 -> 子），带半开区间版本
CREATE TABLE IF NOT EXISTS category_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id       TEXT NOT NULL REFERENCES categories(id),
    child_id        TEXT NOT NULL REFERENCES categories(id),
    effective_from  TEXT NOT NULL,
    effective_to    TEXT,
    change_id       INTEGER REFERENCES changes(id),
    CHECK (parent_id <> child_id),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- 商品 -> 分类 的归属关系，带半开区间版本
CREATE TABLE IF NOT EXISTS product_memberships (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      TEXT NOT NULL REFERENCES products(id),
    category_id     TEXT NOT NULL REFERENCES categories(id),
    effective_from  TEXT NOT NULL,
    effective_to    TEXT,
    change_id       INTEGER REFERENCES changes(id),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE IF NOT EXISTS products (
    id          TEXT PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 一次分类调整（移动 / 拆分 / 合并 / 补录）。change_type 决定语义，
-- payload 以 JSON 留存操作明细，便于报表重述时给出原因。
CREATE TABLE IF NOT EXISTS changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    change_type     TEXT NOT NULL CHECK (change_type IN
                        ('move','split','merge','create','retire','backfill')),
    title           TEXT NOT NULL,
    reason          TEXT,
    effective_date  TEXT NOT NULL,          -- 业务生效日（可在过去，即补录）
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    recorded_by     TEXT NOT NULL DEFAULT 'analyst',
    payload_json    TEXT NOT NULL DEFAULT '{}',
    supersedes_id   INTEGER REFERENCES changes(id)
);

-- 拆分/合并的血缘映射（商品或分类级），区间表示该映射有效的业务日期段
CREATE TABLE IF NOT EXISTS lineage_mappings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id           INTEGER NOT NULL REFERENCES changes(id),
    source_category_id  TEXT REFERENCES categories(id),
    source_product_id   TEXT REFERENCES products(id),
    target_category_id  TEXT NOT NULL REFERENCES categories(id),
    weight              REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0 AND weight <= 1),
    effective_from      TEXT NOT NULL,
    effective_to        TEXT,
    CHECK (source_category_id IS NOT NULL OR source_product_id IS NOT NULL)
);

-- 交易事实（销量）。事实本身不可变；口径差异在查询时按归属版本计算。
CREATE TABLE IF NOT EXISTS sales (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  TEXT NOT NULL REFERENCES products(id),
    sale_date   TEXT NOT NULL,
    qty         INTEGER NOT NULL CHECK (qty >= 0),
    amount      REAL NOT NULL CHECK (amount >= 0),
    UNIQUE (product_id, sale_date)
);

-- 已发布报表：同一 period_key 可有多版本，published 版本不可改。
CREATE TABLE IF NOT EXISTS report_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_key      TEXT NOT NULL,          -- 如 2026-01（月）
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,          -- 半开
    caliber         TEXT NOT NULL CHECK (caliber IN ('pointwise','current')),
    version_no      INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'published'
                        CHECK (status IN ('published','restated','superseded')),
    published_at    TEXT NOT NULL DEFAULT (datetime('now')),
    restate_reason  TEXT,
    change_id       INTEGER REFERENCES changes(id),
    totals_json     TEXT NOT NULL DEFAULT '{}',
    UNIQUE (period_key, caliber, version_no)
);

-- 重算作业（补录时只重算受影响商品 × 日期范围）
CREATE TABLE IF NOT EXISTS recompute_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id       INTEGER NOT NULL REFERENCES changes(id),
    period_key      TEXT NOT NULL,
    affected_products_json TEXT NOT NULL,
    date_from       TEXT NOT NULL,
    date_to         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','done','skipped')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_edges_child_time
    ON category_edges(child_id, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_membership_product_time
    ON product_memberships(product_id, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date);
"""
