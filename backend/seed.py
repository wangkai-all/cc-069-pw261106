"""Seed the workbench with a cross-month category-change sample.

Timeline (2026)
---------------
2026-01-01  Initial tree:
              根分类 全部商品
              ├─ 电子产品 (cat_elec)
              ├─ 服饰 (cat_apparel)
              ├─ 家居 (cat_home)
              ├─ 茶饮 (cat_tea)
              └─ 咖啡 (cat_coffee)
2026-03-01  商品「保温瓶」从 家居 → 服饰（移动，跨月生效）
2026-04-01  「茶饮」+「咖啡」合并为「饮品」(cat_drinks)（合并）
2026-05-15  补录：USB-C 数据线 自 2026-02-01 起属于 电子产品（原本误挂家居）
             → 只重算该商品 2 月起的月份，已发布的 1 月报表不受影响、2-4 月报表重述
2026-06-01  「电子产品」拆分为「手机数码」(cat_phone) 与「智能设备」(cat_smart)（拆分）

Sales cover Jan-Jun so every change is visible in the two-caliber comparison.
A pointwise report for 2026-01 is published *before* the backfill to demonstrate
versioning + restatement reasons.
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))

from schema import SCHEMA
from temporal_core import (
    month_bounds,
    set_membership,
    set_parent,
    today,
)
import operations as ops

DB_PATH = os.environ.get(
    "WORKBENCH_DB", os.path.join(os.path.dirname(__file__), "..", "data",
                                 "workbench.db"))

PRODUCTS = [
    ("p_p1001", "SKU1001", "智能手机 X1"),
    ("p_p1002", "SKU1002", "智能手表 S2"),
    ("p_p1003", "SKU1003", "蓝牙耳机 B3"),
    ("p_p1004", "SKU1004", "USB-C 数据线"),
    ("p_p1005", "SKU1005", "纯棉 T 恤"),
    ("p_p1006", "SKU1006", "牛仔裤"),
    ("p_p1007", "SKU1007", "保温瓶"),
    ("p_p1008", "SKU1008", "台灯"),
    ("p_p1009", "SKU1009", "绿茶礼盒"),
    ("p_p1010", "SKU1010", "咖啡豆 1kg"),
]

# Monthly qty per product (Jan..Jun 2026); amount = qty * unit price.
UNIT_PRICE = {
    "p_p1001": 3999, "p_p1002": 1299, "p_p1003": 599, "p_p1004": 49,
    "p_p1005": 89, "p_p1006": 199, "p_p1007": 129, "p_p1008": 159,
    "p_p1009": 168, "p_p1010": 228,
}
MONTHLY_QTY = {
    "p_p1001": [120, 135, 140, 160, 175, 190],
    "p_p1002": [80, 85, 90, 110, 120, 140],
    "p_p1003": [60, 70, 75, 90, 95, 110],
    "p_p1004": [300, 320, 310, 340, 360, 400],
    "p_p1005": [200, 180, 210, 240, 260, 230],
    "p_p1006": [150, 140, 160, 170, 180, 165],
    "p_p1007": [90, 95, 100, 105, 110, 120],
    "p_p1008": [70, 72, 68, 75, 80, 85],
    "p_p1009": [110, 105, 120, 130, 125, 140],
    "p_p1010": [95, 100, 110, 115, 120, 130],
}
MONTHS = [f"2026-{m:02d}" for m in range(1, 7)]


def open_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def seed(con: sqlite3.Connection) -> None:
    # ---- categories (initial tree, effective 2026-01-01) ------------------
    d0 = "2026-01-01"
    con.execute(
        """INSERT INTO categories (id, code, name, kind, effective_from)
           VALUES (?,?,?,?,'2026-01-01')""",
        ("cat_root", "ROOT", "全部商品", "root"))
    initial = [
        ("cat_elec", "ELEC", "电子产品"),
        ("cat_apparel", "APPAREL", "服饰"),
        ("cat_home", "HOME", "家居"),
        ("cat_tea", "TEA", "茶饮"),
        ("cat_coffee", "COFFEE", "咖啡"),
    ]
    for cid, code, name in initial:
        con.execute(
            """INSERT INTO categories (id, code, name, kind, effective_from)
               VALUES (?,?,?,'normal','2026-01-01')""",
            (cid, code, name))
        set_parent(con, cid, "cat_root", d0)

    # ---- products & initial memberships -----------------------------------
    initial_membership = {
        "p_p1001": "cat_elec", "p_p1002": "cat_elec", "p_p1003": "cat_elec",
        "p_p1004": "cat_home",            # 数据线起初误挂家居
        "p_p1005": "cat_apparel", "p_p1006": "cat_apparel",
        "p_p1007": "cat_home", "p_p1008": "cat_home",
        "p_p1009": "cat_tea", "p_p1010": "cat_coffee",
    }
    for pid, sku, name in PRODUCTS:
        con.execute(
            "INSERT INTO products (id, sku, name) VALUES (?,?,?)",
            (pid, sku, name))
        set_membership(con, pid, initial_membership[pid], d0)

    # ---- sales facts (one row per product-month, mid-month date) ----------
    for pid, quantities in MONTHLY_QTY.items():
        price = UNIT_PRICE[pid]
        for month, qty in zip(MONTHS, quantities):
            sale_date = f"{month}-15"
            con.execute(
                """INSERT INTO sales (product_id, sale_date, qty, amount)
                   VALUES (?,?,?,?)""",
                (pid, sale_date, qty, round(qty * price, 2)))

    # ---- publish monthly pointwise reports BEFORE later changes -----------
    for period in ("2026-01", "2026-02", "2026-03", "2026-04"):
        ops.publish_report(con, period, "pointwise")

    # ---- 2026-03-01 move: 保温瓶 家居 → 服饰 ------------------------------
    ops.move_product(con, "p_p1007", "cat_apparel", "2026-03-01",
                     reason="春季促销调整，保温瓶归入服饰穿搭场景")

    # ---- 2026-04-01 merge: 茶饮 + 咖啡 → 饮品 -----------------------------
    ops.create_category(con, "DRINKS", "饮品", "cat_root", "2026-04-01",
                        reason="整合饮料线")
    ops.merge_categories(
        con, ["cat_tea", "cat_coffee"], "cat_drinks", "2026-04-01",
        reason="茶饮与咖啡供应链统一，合并为饮品事业部")

    # ---- 2026-05-15 backfill: 数据线 自 2026-02-01 起属于电子产品 ----------
    ops.move_product(
        con, "p_p1004", "cat_elec", "2026-02-01",
        reason="补录历史调整：数据线开年即按 3C 配件管理，原误挂家居",
        backfill=True)
    # Restate already-published reports inside the affected scope.
    scope = ops.affected_scope(con, ["p_p1004"], "2026-02-01")
    ops.restate_for_backfill(
        con, con.execute("SELECT MAX(id) AS id FROM changes").fetchone()["id"],
        scope, "数据线 2026-02 起改归电子产品")

    # ---- 2026-06-01 split: 电子产品 → 手机数码 + 智能设备 ------------------
    ops.split_category(
        con, "cat_elec",
        [
            {"code": "PHONE", "name": "手机数码", "parent_id": "cat_root",
             "weight": 0.5, "products": ["p_p1001", "p_p1004"]},
            {"code": "SMART", "name": "智能设备", "parent_id": "cat_root",
             "weight": 0.5, "products": ["p_p1002", "p_p1003"]},
        ],
        "2026-06-01", reason="电子产品线扩张，按终端形态拆分为两个事业部")

    con.commit()


def main() -> None:
    con = open_db(DB_PATH)
    seed(con)
    n_changes = con.execute("SELECT COUNT(*) c FROM changes").fetchone()["c"]
    n_reports = con.execute("SELECT COUNT(*) c FROM report_versions").fetchone()["c"]
    print(f"Seeded {DB_PATH}: {n_changes} changes, {n_reports} report versions")
    con.close()


if __name__ == "__main__":
    main()
