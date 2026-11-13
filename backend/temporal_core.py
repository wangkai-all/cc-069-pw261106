"""Temporal core: bitemporal category-tree rules, point-in-time queries and
lineage resolution.

Pure functions over a sqlite3 connection.  No HTTP or UI knowledge lives here so
the rules can be unit-tested directly.

Conventions
-----------
* Dates are ``YYYY-MM-DD`` strings, compared lexicographically (valid ISO dates
  sort correctly).
* Every versioned row uses a half-open interval ``[from, to)``; ``to IS NULL``
  means open-ended.
* A subject may never have two overlapping intervals -> at most one active
  parent / category at any instant.
* Version rows are never updated in place: the open row is closed and a new row
  is inserted.  History is therefore append-only and fully retained.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

OPEN_END: Optional[str] = None
FAR_FUTURE = "9999-12-31"


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def today() -> str:
    return date.today().isoformat()


def add_days(value: str, days: int) -> str:
    return (parse_date(value) + timedelta(days=days)).isoformat()


def month_key(value: str) -> str:
    return value[:7]


def month_bounds(period_key: str) -> tuple[str, str]:
    """Return half-open ``[start, end)`` bounds for a ``YYYY-MM`` period."""
    start = parse_date(period_key + "-01")
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


def overlap(a_from: str, a_to: Optional[str], b_from: str,
            b_to: Optional[str]) -> bool:
    """Half-open interval overlap test."""
    return a_from < (b_to or FAR_FUTURE) and b_from < (a_to or FAR_FUTURE)


def contains(lo: str, hi: Optional[str], point: str) -> bool:
    return lo <= point and point < (hi or FAR_FUTURE)


# --------------------------------------------------------------------------- #
# Low-level versioned-row operations
# --------------------------------------------------------------------------- #
def _assert_no_overlap(con: sqlite3.Connection, table: str, subject_col: str,
                       subject_id: str, eff_from: str, eff_to: Optional[str],
                       exclude_row: Optional[int] = None) -> None:
    """Reject a new interval if it overlaps an existing one for the same subject.

    This is the invariant behind "同一时点多个父级" prevention.
    """
    rows = con.execute(
        f"""SELECT id, effective_from, effective_to FROM {table}
            WHERE {subject_col} = ?""",
        (subject_id,),
    ).fetchall()
    for row in rows:
        if exclude_row is not None and row["id"] == exclude_row:
            continue
        if overlap(eff_from, eff_to, row["effective_from"],
                   row["effective_to"]):
            raise TemporalConflict(
                f"{table}.{subject_col}={subject_id} 区间重叠: "
                f"[{eff_from}, {eff_to or '∞'}) 与 "
                f"[{row['effective_from']}, {row['effective_to'] or '∞'})"
            )


def _close_open_row(con: sqlite3.Connection, table: str, subject_col: str,
                    subject_id: str, close_at: str,
                    change_id: Optional[int]) -> bool:
    """Close the subject's currently open interval at ``close_at``.

    Returns True if a row was closed.  Fails when the open row starts on or
    after ``close_at`` (the change would erase the existing version).  The
    closed row keeps its original ``change_id`` so the history of which change
    opened the interval stays traceable; only the boundary is written.
    """
    row = con.execute(
        f"""SELECT id, effective_from FROM {table}
            WHERE {subject_col} = ? AND effective_to IS NULL""",
        (subject_id,),
    ).fetchone()
    if row is None:
        return False
    if row["effective_from"] >= close_at:
        raise TemporalConflict(
            f"生效日 {close_at} 早于或等于当前版本起点 {row['effective_from']}，"
            "无法在该点切分版本；如需补录请先修正后续调整。"
        )
    con.execute(
        f"UPDATE {table} SET effective_to = ? WHERE id = ?",
        (close_at, row["id"]),
    )
    return True


def set_parent(con: sqlite3.Connection, child_id: str, parent_id: str,
               eff_from: str, change_id: Optional[int] = None) -> None:
    """Make ``parent_id`` the parent of ``child_id`` starting at ``eff_from``."""
    if child_id == parent_id:
        raise TemporalConflict("节点不能以自身为父级")
    _close_open_row(con, "category_edges", "child_id", child_id, eff_from,
                    change_id)
    _assert_no_overlap(con, "category_edges", "child_id", child_id, eff_from,
                       OPEN_END)
    con.execute(
        """INSERT INTO category_edges
               (parent_id, child_id, effective_from, effective_to, change_id)
           VALUES (?, ?, ?, NULL, ?)""",
        (parent_id, child_id, eff_from, change_id),
    )
    # A freshly written edge must not create a cycle on the as-of tree.
    cycle = detect_cycle(con, eff_from)
    if cycle:
        raise TemporalConflict("分类树出现循环: " + " -> ".join(cycle))


def set_membership(con: sqlite3.Connection, product_id: str, category_id: str,
                   eff_from: str, change_id: Optional[int] = None) -> None:
    """Move a product into ``category_id`` starting at ``eff_from``."""
    _close_open_row(con, "product_memberships", "product_id", product_id,
                    eff_from, change_id)
    _assert_no_overlap(con, "product_memberships", "product_id", product_id,
                       eff_from, OPEN_END)
    con.execute(
        """INSERT INTO product_memberships
               (product_id, category_id, effective_from, effective_to,
                change_id)
           VALUES (?, ?, ?, NULL, ?)""",
        (product_id, category_id, eff_from, change_id),
    )


# Backfill variant: insert a bounded historical interval (used when recording
# an adjustment that was effective in the past and has since been superseded).
def insert_historical_membership(con: sqlite3.Connection, product_id: str,
                                 category_id: str, eff_from: str,
                                 eff_to: str, change_id: Optional[int]) -> None:
    if eff_to <= eff_from:
        raise TemporalConflict("历史区间必须满足 from < to")
    _assert_no_overlap(con, "product_memberships", "product_id", product_id,
                       eff_from, eff_to)
    con.execute(
        """INSERT INTO product_memberships
               (product_id, category_id, effective_from, effective_to,
                change_id)
           VALUES (?, ?, ?, ?, ?)""",
        (product_id, category_id, eff_from, eff_to, change_id),
    )


def insert_historical_edge(con: sqlite3.Connection, parent_id: str,
                           child_id: str, eff_from: str, eff_to: str,
                           change_id: Optional[int]) -> None:
    if child_id == parent_id:
        raise TemporalConflict("节点不能以自身为父级")
    if eff_to <= eff_from:
        raise TemporalConflict("历史区间必须满足 from < to")
    _assert_no_overlap(con, "category_edges", "child_id", child_id, eff_from,
                       eff_to)
    con.execute(
        """INSERT INTO category_edges
               (parent_id, child_id, effective_from, effective_to, change_id)
           VALUES (?, ?, ?, ?, ?)""",
        (parent_id, child_id, eff_from, eff_to, change_id),
    )
    cycle = detect_cycle(con, eff_from)
    if cycle:
        raise TemporalConflict("分类树出现循环: " + " -> ".join(cycle))


# --------------------------------------------------------------------------- #
# Point-in-time reads
# --------------------------------------------------------------------------- #
def active_parent(con: sqlite3.Connection, child_id: str,
                  on_date: str) -> Optional[str]:
    row = con.execute(
        """SELECT parent_id FROM category_edges
            WHERE child_id = ? AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY effective_from DESC LIMIT 1""",
        (child_id, on_date, on_date),
    ).fetchone()
    return row["parent_id"] if row else None


def active_category(con: sqlite3.Connection, product_id: str,
                    on_date: str) -> Optional[str]:
    """商品在 ``on_date`` 当天所属分类（点时态口径）。"""
    row = con.execute(
        """SELECT category_id FROM product_memberships
            WHERE product_id = ? AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY effective_from DESC LIMIT 1""",
        (product_id, on_date, on_date),
    ).fetchone()
    return row["category_id"] if row else None


def current_category(con: sqlite3.Connection,
                     product_id: str) -> Optional[str]:
    """商品当前所属分类（最新开放区间）。"""
    row = con.execute(
        """SELECT category_id FROM product_memberships
            WHERE product_id = ? AND effective_to IS NULL
            ORDER BY effective_from DESC LIMIT 1""",
        (product_id,),
    ).fetchone()
    return row["category_id"] if row else None


def ancestors(con: sqlite3.Connection, category_id: str,
              on_date: str) -> list[str]:
    """Return the ancestor chain bottom-up (excluding the node itself)."""
    chain: list[str] = []
    seen: set[str] = set()
    cursor = category_id
    while True:
        parent = active_parent(con, cursor, on_date)
        if parent is None:
            break
        if parent in seen:
            raise TemporalConflict(
                "分类树出现循环: " + " -> ".join(chain + [cursor, parent])
            )
        seen.add(parent)
        chain.append(parent)
        cursor = parent
    return chain


def tree_snapshot(con: sqlite3.Connection, on_date: str) -> dict[str, Any]:
    """Full tree as of ``on_date``: nodes + active edges, with validation.

    Nodes whose ``effective_from`` is in the future or whose
    ``retired_at`` has already passed are excluded, so a March snapshot never
    shows a June category.  Returns
    ``{"nodes": [...], "roots": [...], "children": {id: [ids]}}``.
    Raises TemporalConflict on cycle or multi-parent at that instant.
    """
    nodes = [dict(r) for r in con.execute(
        """SELECT * FROM categories
            WHERE effective_from <= ?
              AND (retired_at IS NULL OR retired_at > ?)""",
        (on_date, on_date))]
    node_ids = {n["id"] for n in nodes}
    edges = con.execute(
        """SELECT parent_id, child_id FROM category_edges
            WHERE effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)""",
        (on_date, on_date),
    ).fetchall()

    parent_of: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    for edge in edges:
        child, parent = edge["child_id"], edge["parent_id"]
        # Edges touching not-yet-existing nodes are invisible at this instant.
        if child not in node_ids or parent not in node_ids:
            continue
        if child in parent_of and parent_of[child] != parent:
            raise TemporalConflict(
                f"分类 {child} 在 {on_date} 存在多个父级: "
                f"{parent_of[child]} 与 {parent}"
            )
        parent_of[child] = parent
        children.setdefault(parent, []).append(child)

    cycle = _cycle_from_edges(parent_of)
    if cycle:
        raise TemporalConflict("分类树出现循环: " + " -> ".join(cycle))

    # A node is a root iff it has no active parent edge at this instant.
    child_set = set(parent_of)
    roots = sorted(n["id"] for n in nodes if n["id"] not in child_set)
    for kids in children.values():
        kids.sort()
    archived = [dict(r) for r in con.execute(
        """SELECT * FROM categories
            WHERE effective_from <= ? AND retired_at IS NOT NULL
              AND retired_at <= ? ORDER BY retired_at""",
        (on_date, on_date))]
    return {"date": on_date, "nodes": nodes, "roots": roots,
            "children": children, "parent_of": parent_of,
            "archived": archived}


def detect_cycle(con: sqlite3.Connection, on_date: str) -> Optional[list[str]]:
    edges = con.execute(
        """SELECT parent_id, child_id FROM category_edges
            WHERE effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)""",
        (on_date, on_date),
    ).fetchall()
    parent_of = {r["child_id"]: r["parent_id"] for r in edges}
    return _cycle_from_edges(parent_of)


def _cycle_from_edges(parent_of: dict[str, str]) -> Optional[list[str]]:
    color: dict[str, int] = {}  # 0 = visiting, 1 = done

    def walk(node: str, stack: list[str]) -> Optional[list[str]]:
        state = color.get(node)
        if state == 0:
            idx = stack.index(node)
            return stack[idx:] + [node]
        if state == 1:
            return None
        color[node] = 0
        stack.append(node)
        parent = parent_of.get(node)
        if parent is not None:
            found = walk(parent, stack)
            if found:
                return found
        stack.pop()
        color[node] = 1
        return None

    for node in parent_of:
        found = walk(node, [])
        if found:
            return found
    return None


def validate_all_dates(con: sqlite3.Connection) -> list[dict[str, str]]:
    """Validate the tree at every boundary date (cheap full-history check)."""
    boundaries = {r["effective_from"] for r in
                  con.execute("SELECT DISTINCT effective_from FROM category_edges")}
    problems: list[dict[str, str]] = []
    for day in sorted(boundaries):
        try:
            tree_snapshot(con, day)
        except TemporalConflict as exc:
            problems.append({"date": day, "error": str(exc)})
    return problems


# --------------------------------------------------------------------------- #
# Product history & lineage
# --------------------------------------------------------------------------- #
def product_history(con: sqlite3.Connection,
                    product_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        """SELECT m.effective_from, m.effective_to, m.category_id,
                  c.name AS category_name, c.code AS category_code,
                  m.change_id, ch.title AS change_title, ch.change_type
             FROM product_memberships m
             JOIN categories c ON c.id = m.category_id
        LEFT JOIN changes ch ON ch.id = m.change_id
            WHERE m.product_id = ?
         ORDER BY m.effective_from, m.id""",
        (product_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def category_lineage(con: sqlite3.Connection,
                     category_id: str) -> list[dict[str, Any]]:
    """Category-level lineage only (product-level backfill rows excluded)."""
    rows = con.execute(
        """SELECT l.*, ch.title AS change_title, ch.effective_date
             FROM lineage_mappings l
             JOIN changes ch ON ch.id = l.change_id
            WHERE l.source_category_id = ?
         ORDER BY l.effective_from""",
        (category_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_caliber_category(con: sqlite3.Connection, product_id: str,
                             sale_date: str, caliber: str) -> Optional[str]:
    """Map one sale to a category under the chosen caliber.

    * ``pointwise``: the category the product belonged to on ``sale_date``.
    * ``current``: the product's current open-ended category, following
      lineage mappings when the product's historical category no longer exists
      as a normal node (merge / split targets).
    """
    if caliber == "pointwise":
        return active_category(con, product_id, sale_date)
    if caliber != "current":
        raise ValueError(f"未知口径: {caliber}")

    current = current_category(con, product_id)
    if current is not None:
        return current
    # Product has no open membership: walk lineage from its last category.
    last = con.execute(
        """SELECT category_id FROM product_memberships
            WHERE product_id = ? ORDER BY effective_from DESC LIMIT 1""",
        (product_id,),
    ).fetchone()
    if not last:
        return None
    return _follow_lineage(con, last["category_id"])


def _follow_lineage(con: sqlite3.Connection,
                    category_id: str) -> str:
    seen: set[str] = set()
    cursor = category_id
    while cursor not in seen:
        seen.add(cursor)
        row = con.execute(
            """SELECT target_category_id FROM lineage_mappings
                WHERE source_category_id = ?
             ORDER BY effective_from DESC LIMIT 1""",
            (cursor,),
        ).fetchone()
        if row is None:
            return cursor
        cursor = row["target_category_id"]
    return cursor  # lineage loop guard; tree validation should have caught it


# --------------------------------------------------------------------------- #
# Sales aggregation under both calibers
# --------------------------------------------------------------------------- #
def aggregate_sales(con: sqlite3.Connection, date_from: str, date_to: str,
                    caliber: str,
                    product_filter: Optional[Iterable[str]] = None
                    ) -> list[dict[str, Any]]:
    """Aggregate sales in half-open ``[date_from, date_to)`` by category.

    ``product_filter`` restricts the scan (incremental backfill only touches
    affected products).
    """
    sql = """SELECT s.product_id, s.sale_date, s.qty, s.amount
               FROM sales s
              WHERE s.sale_date >= ? AND s.sale_date < ?"""
    params: list[Any] = [date_from, date_to]
    if product_filter is not None:
        ids = list(product_filter)
        if not ids:
            return []
        sql += " AND s.product_id IN (" + ",".join("?" * len(ids)) + ")"
        params.extend(ids)
    sql += " ORDER BY s.sale_date"

    buckets: dict[str, dict[str, Any]] = {}
    for sale in con.execute(sql, params):
        cat = resolve_caliber_category(con, sale["product_id"],
                                       sale["sale_date"], caliber)
        if cat is None:
            cat = "__unclassified__"
        bucket = buckets.setdefault(cat, {
            "category_id": cat, "qty": 0, "amount": 0.0, "products": set()
        })
        bucket["qty"] += sale["qty"]
        bucket["amount"] += sale["amount"]
        bucket["products"].add(sale["product_id"])

    names = {r["id"]: dict(r) for r in con.execute("SELECT * FROM categories")}
    result = []
    for cat_id, bucket in sorted(buckets.items()):
        info = names.get(cat_id)
        result.append({
            "category_id": cat_id,
            "category_code": info["code"] if info else "—",
            "category_name": info["name"] if info else "未分类",
            "qty": bucket["qty"],
            "amount": round(bucket["amount"], 2),
            "product_count": len(bucket["products"]),
        })
    return result


def compare_calibers(con: sqlite3.Connection, date_from: str,
                     date_to: str) -> dict[str, Any]:
    """Two-caliber comparison plus per-product delta detail (for CSV export)."""
    pointwise = {r["category_id"]: r
                 for r in aggregate_sales(con, date_from, date_to,
                                          "pointwise")}
    current = {r["category_id"]: r
               for r in aggregate_sales(con, date_from, date_to, "current")}
    all_cats = sorted(set(pointwise) | set(current))
    rows = []
    for cat_id in all_cats:
        p = pointwise.get(cat_id, {"qty": 0, "amount": 0.0})
        c = current.get(cat_id, {"qty": 0, "amount": 0.0})
        info = con.execute(
            "SELECT code, name FROM categories WHERE id = ?", (cat_id,)
        ).fetchone()
        rows.append({
            "category_id": cat_id,
            "category_code": info["code"] if info else "—",
            "category_name": info["name"] if info else "未分类",
            "pointwise_qty": p["qty"],
            "current_qty": c["qty"],
            "qty_delta": c["qty"] - p["qty"],
            "pointwise_amount": round(p["amount"], 2),
            "current_amount": round(c["amount"], 2),
            "amount_delta": round(c["amount"] - p["amount"], 2),
        })

    detail = _product_delta_detail(con, date_from, date_to)
    totals = {
        "pointwise_amount": round(sum(r["pointwise_amount"] for r in rows), 2),
        "current_amount": round(sum(r["current_amount"] for r in rows), 2),
        "pointwise_qty": sum(r["pointwise_qty"] for r in rows),
        "current_qty": sum(r["current_qty"] for r in rows),
        "moved_product_count": sum(1 for d in detail
                                   if d["pointwise_category_id"]
                                   != d["current_category_id"]),
    }
    totals["amount_delta"] = round(
        totals["current_amount"] - totals["pointwise_amount"], 2)
    return {"rows": rows, "detail": detail, "totals": totals}


def _product_delta_detail(con: sqlite3.Connection, date_from: str,
                          date_to: str) -> list[dict[str, Any]]:
    sales = con.execute(
        """SELECT product_id, SUM(qty) AS qty, SUM(amount) AS amount
             FROM sales
            WHERE sale_date >= ? AND sale_date < ?
         GROUP BY product_id""",
        (date_from, date_to),
    ).fetchall()
    products = {r["id"]: dict(r)
                for r in con.execute("SELECT id, sku, name FROM products")}
    detail = []
    for sale in sales:
        pid = sale["product_id"]
        # pointwise category is resolved per sale day; report the one attached
        # to the first sale day in range for the detail row.
        first_day = con.execute(
            """SELECT MIN(sale_date) AS d FROM sales
                WHERE product_id = ? AND sale_date >= ? AND sale_date < ?""",
            (pid, date_from, date_to),
        ).fetchone()["d"]
        pw = resolve_caliber_category(con, pid, first_day, "pointwise")
        cur = resolve_caliber_category(con, pid, first_day, "current")
        p_info = con.execute("SELECT code, name FROM categories WHERE id=?",
                             (pw,)).fetchone() if pw else None
        c_info = con.execute("SELECT code, name FROM categories WHERE id=?",
                             (cur,)).fetchone() if cur else None
        prod = products.get(pid, {"sku": "?", "name": "?"})
        detail.append({
            "product_id": pid,
            "sku": prod["sku"],
            "product_name": prod["name"],
            "pointwise_category_id": pw,
            "pointwise_category": p_info["name"] if p_info else "未分类",
            "current_category_id": cur,
            "current_category": c_info["name"] if c_info else "未分类",
            "qty": sale["qty"],
            "amount": round(sale["amount"], 2),
            "reclassified": pw != cur,
        })
    detail.sort(key=lambda d: (not d["reclassified"], -d["amount"]))
    return detail


# --------------------------------------------------------------------------- #
# Incremental backfill scope
# --------------------------------------------------------------------------- #
def affected_scope(con: sqlite3.Connection, product_ids: list[str],
                   eff_from: str) -> dict[str, Any]:
    """Compute the smallest recompute scope for a backfilled change.

    Only the given products and only months intersecting sales on/after the
    change's effective date are returned.  Already-published monthly reports in
    those months become restatement candidates.
    """
    if not product_ids:
        return {"products": [], "periods": [], "date_from": eff_from,
                "date_to": today()}
    placeholders = ",".join("?" * len(product_ids))
    rows = con.execute(
        f"""SELECT DISTINCT sale_date FROM sales
              WHERE product_id IN ({placeholders}) AND sale_date >= ?
             ORDER BY sale_date""",
        [*product_ids, eff_from],
    ).fetchall()
    days = [r["sale_date"] for r in rows]
    periods = sorted({month_key(d) for d in days})
    return {
        "products": product_ids,
        "periods": periods,
        "date_from": eff_from,
        "date_to": today(),
        "sale_days": days,
    }


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class TemporalConflict(Exception):
    """Raised when a temporal invariant (overlap / cycle / multi-parent) breaks."""
