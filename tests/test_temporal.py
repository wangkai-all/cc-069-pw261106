"""Unit tests for the temporal core and business operations.

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from schema import SCHEMA
from temporal_core import (
    TemporalConflict,
    active_category,
    affected_scope,
    compare_calibers,
    current_category,
    detect_cycle,
    month_bounds,
    set_membership,
    set_parent,
    tree_snapshot,
    validate_all_dates,
)
import operations as ops


def fresh_db():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def make_tree(con):
    """root -> [a, b]; a -> [a1]; with two products."""
    con.execute(
        "INSERT INTO categories (id,code,name,kind,effective_from) "
        "VALUES ('r','R','根','root','2026-01-01')")
    for cid, code, name in [("a", "A", "甲"), ("b", "B", "乙"),
                            ("a1", "A1", "甲一")]:
        con.execute(
            "INSERT INTO categories (id,code,name,kind) VALUES (?,?,?,'normal')",
            (cid, code, name))
    set_parent(con, "a", "r", "2026-01-01")
    set_parent(con, "b", "r", "2026-01-01")
    set_parent(con, "a1", "a", "2026-01-01")
    con.execute("INSERT INTO products (id,sku,name) VALUES "
                "('p1','S1','商品一'),('p2','S2','商品二')")
    set_membership(con, "p1", "a", "2026-01-01")
    set_membership(con, "p2", "b", "2026-01-01")
    return con


class TestTreeInvariants(unittest.TestCase):
    def setUp(self):
        self.con = make_tree(fresh_db())

    def test_no_self_parent(self):
        with self.assertRaises(TemporalConflict):
            set_parent(self.con, "a", "a", "2026-02-01")

    def test_cycle_rejected(self):
        # r -> a -> a1 ; try making r a child of a1 => cycle
        with self.assertRaises(TemporalConflict):
            set_parent(self.con, "r", "a1", "2026-02-01")
        self.assertIsNone(detect_cycle(self.con, "2026-01-15"))

    def test_single_parent_at_any_instant(self):
        # Move a1 under b in March: one parent in Feb, another in March, never two.
        set_parent(self.con, "a1", "b", "2026-03-01")
        snap_feb = tree_snapshot(self.con, "2026-02-15")
        snap_mar = tree_snapshot(self.con, "2026-03-15")
        self.assertEqual(snap_feb["parent_of"]["a1"], "a")
        self.assertEqual(snap_mar["parent_of"]["a1"], "b")
        self.assertEqual(validate_all_dates(self.con), [])

    def test_historical_interval_overlap_rejected(self):
        from temporal_core import insert_historical_edge
        with self.assertRaises(TemporalConflict):
            # Existing a1 edge covers [2026-01-01, 2026-03-01) after move above
            set_parent(self.con, "a1", "b", "2026-03-01")
            insert_historical_edge(self.con, "r", "a1", "2026-02-01",
                                   "2026-04-01", None)


class TestPointInTime(unittest.TestCase):
    def setUp(self):
        self.con = make_tree(fresh_db())

    def test_membership_versions(self):
        self.assertEqual(active_category(self.con, "p1", "2026-01-15"), "a")
        set_membership(self.con, "p1", "b", "2026-03-01")
        self.assertEqual(active_category(self.con, "p1", "2026-02-28"), "a")
        self.assertEqual(active_category(self.con, "p1", "2026-03-01"), "b")
        self.assertEqual(current_category(self.con, "p1"), "b")
        # Old relation retained: two interval rows exist.
        rows = self.con.execute(
            "SELECT COUNT(*) c FROM product_memberships WHERE product_id='p1'"
        ).fetchone()["c"]
        self.assertEqual(rows, 2)

    def test_backdated_change_preserves_later_versions(self):
        set_membership(self.con, "p1", "b", "2026-04-01")  # later move
        ops.move_product(self.con, "p1", "a1", "2026-02-01",
                         backfill=True, reason="补录")
        # Jan: a; Feb-Mar: a1; Apr+: b
        self.assertEqual(active_category(self.con, "p1", "2026-01-31"), "a")
        self.assertEqual(active_category(self.con, "p1", "2026-02-01"), "a1")
        self.assertEqual(active_category(self.con, "p1", "2026-03-31"), "a1")
        self.assertEqual(active_category(self.con, "p1", "2026-04-01"), "b")
        self.assertEqual(current_category(self.con, "p1"), "b")


class TestBackfillScope(unittest.TestCase):
    def setUp(self):
        self.con = make_tree(fresh_db())
        # Sales for p1 in Jan..Mar, p2 only in Jan.
        for pid, month, qty in [("p1", "2026-01-15", 10),
                                ("p1", "2026-02-15", 20),
                                ("p1", "2026-03-15", 30),
                                ("p2", "2026-01-15", 5)]:
            self.con.execute(
                "INSERT INTO sales (product_id,sale_date,qty,amount) "
                "VALUES (?,?,?,?)", (pid, month, qty, qty * 10))

    def test_scope_limited_to_product_and_months(self):
        scope = affected_scope(self.con, ["p1"], "2026-02-01")
        self.assertEqual(scope["products"], ["p1"])
        self.assertEqual(scope["periods"], ["2026-02", "2026-03"])

    def test_recompute_jobs_created_only_for_scope(self):
        result = ops.move_product(self.con, "p1", "b", "2026-02-01",
                                  backfill=True)
        periods = result["recompute_job"]["periods"]
        self.assertEqual(periods, ["2026-02", "2026-03"])
        jobs = self.con.execute(
            "SELECT DISTINCT period_key FROM recompute_jobs").fetchall()
        self.assertEqual({j["period_key"] for j in jobs},
                         {"2026-02", "2026-03"})


class TestReportVersioning(unittest.TestCase):
    def setUp(self):
        self.con = make_tree(fresh_db())
        self.con.execute(
            "INSERT INTO sales (product_id,sale_date,qty,amount) "
            "VALUES ('p1','2026-02-15',10,100),('p2','2026-02-20',4,40)")

    def test_restatement_appends_version_with_reason(self):
        v1 = ops.publish_report(self.con, "2026-02", "pointwise")
        self.assertEqual(v1["version_no"], 1)
        self.assertEqual(v1["status"], "published")
        # Backfill changes p1 history then restatement publishes v2.
        ops.move_product(self.con, "p1", "b", "2026-02-01", backfill=True)
        scope = affected_scope(self.con, ["p1"], "2026-02-01")
        change_id = self.con.execute(
            "SELECT MAX(id) i FROM changes").fetchone()["i"]
        restated = ops.restate_for_backfill(self.con, change_id, scope,
                                            "测试补录原因")
        self.assertEqual(len(restated), 1)
        self.assertEqual(restated[0]["version_no"], 2)
        self.assertEqual(restated[0]["status"], "restated")
        reason = self.con.execute(
            "SELECT restate_reason r FROM report_versions "
            "WHERE version_no=2").fetchone()["r"]
        self.assertIn("测试补录原因", reason)
        # v1 retained but superseded.
        old = self.con.execute(
            "SELECT status FROM report_versions WHERE version_no=1"
        ).fetchone()["status"]
        self.assertEqual(old, "superseded")

    def test_month_without_published_report_is_skipped(self):
        ops.move_product(self.con, "p1", "b", "2026-02-01", backfill=True)
        scope = affected_scope(self.con, ["p1"], "2026-02-01")
        change_id = self.con.execute(
            "SELECT MAX(id) i FROM changes").fetchone()["i"]
        self.assertEqual(
            ops.restate_for_backfill(self.con, change_id, scope, "x"), [])
        job = self.con.execute(
            "SELECT status FROM recompute_jobs").fetchone()["status"]
        self.assertEqual(job, "skipped")


class TestSplitMerge(unittest.TestCase):
    def setUp(self):
        self.con = make_tree(fresh_db())
        for pid, month, qty in [("p1", "2026-05-15", 10),
                                ("p2", "2026-05-15", 8)]:
            self.con.execute(
                "INSERT INTO sales (product_id,sale_date,qty,amount) "
                "VALUES (?,?,?,?)", (pid, month, qty, qty * 10))

    def test_split_redistributes_and_archives_source(self):
        # Sales in May; split effective 2026-05-01 so the scope contains May.
        self.con.execute(
            "UPDATE sales SET sale_date='2026-05-15'")
        result = ops.split_category(
            self.con, "a",
            [{"code": "A-X", "name": "甲X", "parent_id": "r",
              "products": ["p1"]},
             {"code": "A-Y", "name": "甲Y", "parent_id": "r",
              "products": []}],
            "2026-05-01")
        self.assertEqual(current_category(self.con, "p1"), "cat_a-x")
        kind = self.con.execute(
            "SELECT kind FROM categories WHERE id='a'").fetchone()["kind"]
        self.assertEqual(kind, "retired")  # old node kept, not deleted
        lineage = self.con.execute(
            "SELECT COUNT(*) c FROM lineage_mappings "
            "WHERE source_category_id='a'").fetchone()["c"]
        self.assertEqual(lineage, 2)
        self.assertEqual(result["recompute_job"]["periods"], ["2026-05"])

    def test_merge_moves_members_and_keeps_lineage(self):
        ops.merge_categories(self.con, ["a", "b"], "a1", "2026-06-01")
        self.assertEqual(current_category(self.con, "p1"), "a1")
        self.assertEqual(current_category(self.con, "p2"), "a1")
        mappings = self.con.execute(
            "SELECT source_category_id FROM lineage_mappings "
            "WHERE target_category_id='a1'").fetchall()
        self.assertEqual({m["source_category_id"] for m in mappings},
                         {"a", "b"})


class TestCaliberComparison(unittest.TestCase):
    def setUp(self):
        self.con = make_tree(fresh_db())
        # January sales while p1 is in category a.
        self.con.execute(
            "INSERT INTO sales (product_id,sale_date,qty,amount) "
            "VALUES ('p1','2026-01-15',10,100)")
        set_membership(self.con, "p1", "b", "2026-03-01")

    def test_two_calibers_diverge_for_historical_sales(self):
        data = compare_calibers(self.con, "2026-01-01", "2026-02-01")
        by_cat = {r["category_id"]: r for r in data["rows"]}
        # Pointwise: p1's Jan sale stays under a; current caliber moves it to b.
        self.assertEqual(by_cat["a"]["pointwise_qty"], 10)
        self.assertEqual(by_cat["a"]["current_qty"], 0)
        self.assertEqual(by_cat["b"]["current_qty"], 10)
        self.assertEqual(data["totals"]["moved_product_count"], 1)
        # Total amount is conserved across calibers.
        self.assertEqual(data["totals"]["pointwise_amount"],
                         data["totals"]["current_amount"])
        detail = next(d for d in data["detail"] if d["product_id"] == "p1")
        self.assertTrue(detail["reclassified"])


class TestMonthBounds(unittest.TestCase):
    def test_december_rollover(self):
        start, end = month_bounds("2026-12")
        self.assertEqual(start, "2026-12-01")
        self.assertEqual(end, "2027-01-01")

    def test_normal_month(self):
        start, end = month_bounds("2026-02")
        self.assertEqual((start, end), ("2026-02-01", "2026-03-01"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
