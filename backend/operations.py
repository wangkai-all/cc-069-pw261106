"""Business operations on top of the temporal core.

All mutating operations run inside one transaction and record a ``changes``
row plus lineage where relevant.  Backdated operations additionally create
``recompute_jobs`` limited to the affected products and months, and mark
already-published reports for restatement.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from temporal_core import (
    TemporalConflict,
    active_category,
    add_days,
    affected_scope,
    aggregate_sales,
    current_category,
    insert_historical_edge,
    insert_historical_membership,
    month_bounds,
    month_key,
    set_membership,
    set_parent,
    today,
    to_json,
)


# --------------------------------------------------------------------------- #
# Categories & products
# --------------------------------------------------------------------------- #
def create_category(con: sqlite3.Connection, code: str, name: str,
                    parent_id: Optional[str], eff_from: str,
                    kind: str = "normal", note: Optional[str] = None,
                    reason: Optional[str] = None) -> dict[str, Any]:
    cat_id = "cat_" + code.lower().replace(" ", "_")
    if con.execute("SELECT 1 FROM categories WHERE id=? OR code=?",
                   (cat_id, code)).fetchone():
        raise TemporalConflict(f"分类编码 {code} 已存在")
    change_id = _log_change(con, "create", f"新建分类 {name}", reason,
                            eff_from, {"code": code, "parent_id": parent_id})
    con.execute(
        """INSERT INTO categories
               (id, code, name, kind, effective_from, note)
         VALUES (?,?,?,?,?,?)""",
        (cat_id, code, name,
         "root" if parent_id is None else kind, eff_from, note),
    )
    if parent_id is not None:
        set_parent(con, cat_id, parent_id, eff_from, change_id)
    return {"category_id": cat_id, "change_id": change_id}


def move_category(con: sqlite3.Connection, child_id: str, new_parent_id: str,
                  eff_from: str, reason: Optional[str] = None,
                  backfill: bool = False) -> dict[str, Any]:
    """Drag-and-drop reparenting. ``backfill=True`` allows a past date."""
    child = _must_category(con, child_id)
    parent = _must_category(con, new_parent_id)
    change_type = "backfill" if backfill and eff_from < today() else "move"
    change_id = _log_change(
        con, change_type, f"移动分类 {child['name']} → {parent['name']}",
        reason, eff_from,
        {"child_id": child_id, "new_parent_id": new_parent_id,
         "backfill": backfill})

    if backfill and eff_from < today():
        # Bounded historical edge: the existing open edge stays untouched;
        # callers normally split the open edge first (see reparent_backdated).
        insert_historical_edge(con, new_parent_id, child_id, eff_from,
                               add_days(eff_from, 1), change_id)
    else:
        set_parent(con, child_id, new_parent_id, eff_from, change_id)
    return {"change_id": change_id}


def reparent_backdated(con: sqlite3.Connection, child_id: str,
                       old_parent_id: str, new_parent_id: str,
                       eff_from: str, eff_to: str,
                       reason: Optional[str]) -> int:
    """Record a past parent change bounded by ``[eff_from, eff_to)``.

    Used by 补录: the current open edge remains the source of truth for later
    dates, while the historical segment is archived.
    """
    _must_category(con, child_id)
    _must_category(con, new_parent_id)
    change_id = _log_change(
        con, "backfill", f"补录分类移动 {child_id} → {new_parent_id}",
        reason, eff_from,
        {"child_id": child_id, "old_parent_id": old_parent_id,
         "new_parent_id": new_parent_id, "range": [eff_from, eff_to]})
    insert_historical_edge(con, new_parent_id, child_id, eff_from, eff_to,
                           change_id)
    return change_id


# --------------------------------------------------------------------------- #
# Product moves / splits / merges
# --------------------------------------------------------------------------- #
def move_product(con: sqlite3.Connection, product_id: str,
                 target_category_id: str, eff_from: str,
                 reason: Optional[str] = None,
                 backfill: bool = False) -> dict[str, Any]:
    product = _must_product(con, product_id)
    target = _must_category(con, target_category_id)
    change_type = "backfill" if backfill and eff_from < today() else "move"
    payload = {"product_id": product_id, "target_category_id":
               target_category_id, "backfill": backfill}
    change_id = _log_change(
        con, change_type,
        f"商品 {product['name']} 移动至 {target['name']}", reason, eff_from,
        payload)

    if backfill and eff_from < today():
        job = _backfill_membership(con, product_id, target_category_id,
                                   eff_from, change_id)
    else:
        set_membership(con, product_id, target_category_id, eff_from,
                       change_id)
        job = None
    return {"change_id": change_id, "recompute_job": job}


def split_category(con: sqlite3.Connection, source_category_id: str,
                   new_categories: list[dict[str, Any]],
                   eff_from: str, reason: Optional[str] = None
                   ) -> dict[str, Any]:
    """Split a category into >=2 new ones.

    Each new category is ``{"code", "name", "parent_id", "products": [ids],
    "weight"}``.  Source products are redistributed; the source node is kept
    (retired/archived) and lineage rows preserve the old relation.
    """
    source = _must_category(con, source_category_id)
    if len(new_categories) < 2:
        raise TemporalConflict("拆分至少需要两个新分类")
    change_id = _log_change(
        con, "split", f"拆分分类 {source['name']}", reason, eff_from,
        {"source": source_category_id,
         "targets": [{"code": c["code"], "weight": c.get("weight", 1.0)}
                     for c in new_categories]})

    all_products: list[str] = []
    for spec in new_categories:
        new_id = "cat_" + spec["code"].lower().replace(" ", "_")
        if con.execute("SELECT 1 FROM categories WHERE id=?",
                       (new_id,)).fetchone():
            raise TemporalConflict(f"分类编码 {spec['code']} 已存在")
        con.execute(
            """INSERT INTO categories
                   (id, code, name, kind, effective_from)
             VALUES (?,?,?,'normal',?)""",
            (new_id, spec["code"], spec["name"], eff_from))
        if spec.get("parent_id"):
            set_parent(con, new_id, spec["parent_id"], eff_from, change_id)
        con.execute(
            """INSERT INTO lineage_mappings
                   (change_id, source_category_id, target_category_id,
                    weight, effective_from)
             VALUES (?, ?, ?, ?, ?)""",
            (change_id, source_category_id, new_id,
             spec.get("weight", 1.0), eff_from))
        for pid in spec["products"]:
            set_membership(con, pid, new_id, eff_from, change_id)
            all_products.append(pid)

    # Retire the source node but keep it for history (旧关系留档).
    con.execute(
        "UPDATE categories SET kind='retired', retired_at=? WHERE id=?",
        (eff_from, source_category_id))
    scope = affected_scope(con, all_products, eff_from)
    job = _queue_recompute(con, change_id, scope)
    return {"change_id": change_id, "recompute_job": job,
            "products": all_products}


def merge_categories(con: sqlite3.Connection, source_category_ids: list[str],
                     target_category_id: str, eff_from: str,
                     reason: Optional[str] = None) -> dict[str, Any]:
    """Merge N source categories into one existing target category."""
    if len(source_category_ids) < 2:
        raise TemporalConflict("合并至少需要两个源分类")
    target = _must_category(con, target_category_id)
    change_id = _log_change(
        con, "merge",
        f"合并 {len(source_category_ids)} 个分类至 {target['name']}",
        reason, eff_from,
        {"sources": source_category_ids, "target": target_category_id})

    product_ids: list[str] = []
    for source_id in source_category_ids:
        source = _must_category(con, source_id)
        con.execute(
            """INSERT INTO lineage_mappings
                   (change_id, source_category_id, target_category_id,
                    weight, effective_from)
             VALUES (?, ?, ?, 1.0, ?)""",
            (change_id, source_id, target_category_id, eff_from))
        members = con.execute(
            """SELECT DISTINCT product_id FROM product_memberships
                WHERE category_id = ? AND effective_to IS NULL""",
            (source_id,),
        ).fetchall()
        for row in members:
            set_membership(con, row["product_id"], target_category_id,
                           eff_from, change_id)
            product_ids.append(row["product_id"])
        con.execute(
            "UPDATE categories SET kind='merged', retired_at=? WHERE id=?",
            (eff_from, source_id))
    scope = affected_scope(con, product_ids, eff_from)
    job = _queue_recompute(con, change_id, scope)
    return {"change_id": change_id, "recompute_job": job,
            "products": product_ids}


def _backfill_membership(con: sqlite3.Connection, product_id: str,
                         target_category_id: str, eff_from: str,
                         change_id: int) -> dict[str, Any]:
    """Insert a bounded past membership without disturbing later versions.

    The product's membership interval covering ``eff_from`` is split:
    ``[old_from, eff_from)`` keeps the old category,
    ``[eff_from, next_boundary)`` carries the backfilled category, and the
    existing later interval stays in place.
    """
    covering = con.execute(
        """SELECT id, category_id, effective_from, effective_to
             FROM product_memberships
            WHERE product_id = ? AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY effective_from DESC LIMIT 1""",
        (product_id, eff_from, eff_from),
    ).fetchone()
    if covering is None:
        raise TemporalConflict(
            f"商品 {product_id} 在 {eff_from} 没有可补录的归属区间")
    if covering["category_id"] == target_category_id:
        raise TemporalConflict("补录的分类与当时分类相同，无需调整")

    eff_to = covering["effective_to"]
    if eff_to is None:
        # Open interval: the correction propagates to the present — close the
        # old interval and open a new open-ended one on the target category.
        con.execute(
            "UPDATE product_memberships SET effective_to=? WHERE id=?",
            (eff_from, covering["id"]))
        set_membership(con, product_id, target_category_id, eff_from,
                       change_id)
        lineage_to = None
    else:
        # Bounded historical interval: split into up to three pieces, leaving
        # any later versions untouched.
        con.execute(
            "UPDATE product_memberships SET effective_to=? WHERE id=?",
            (eff_from, covering["id"]))
        insert_historical_membership(con, product_id, target_category_id,
                                     eff_from, eff_to, change_id)
        if eff_to < covering["effective_to"]:
            insert_historical_membership(
                con, product_id, covering["category_id"], eff_to,
                covering["effective_to"], change_id)
        lineage_to = eff_to

    con.execute(
        """INSERT INTO lineage_mappings
               (change_id, source_product_id, target_category_id,
                weight, effective_from, effective_to)
         VALUES (?, ?, ?, 1.0, ?, ?)""",
        (change_id, product_id, target_category_id, eff_from, lineage_to))
    scope = affected_scope(con, [product_id], eff_from)
    return _queue_recompute(con, change_id, scope)


# --------------------------------------------------------------------------- #
# Reports & restatement
# --------------------------------------------------------------------------- #
def publish_report(con: sqlite3.Connection, period_key: str,
                   caliber: str, restate_reason: Optional[str] = None,
                   change_id: Optional[int] = None,
                   product_filter: Optional[list[str]] = None,
                   mark_restated: bool = True) -> dict[str, Any]:
    """Publish (or restate) a monthly report version.

    Existing published versions are never overwritten: a new version row is
    appended and the previous one marked ``superseded``.
    """
    p_start, p_end = month_bounds(period_key)
    rows = aggregate_sales(con, p_start, p_end, caliber, product_filter)
    previous = con.execute(
        """SELECT * FROM report_versions
            WHERE period_key=? AND caliber=?
         ORDER BY version_no DESC LIMIT 1""",
        (period_key, caliber),
    ).fetchone()
    version_no = (previous["version_no"] + 1) if previous else 1
    if previous and mark_restated:
        con.execute(
            "UPDATE report_versions SET status='superseded' WHERE id=?",
            (previous["id"],))

    status = "restated" if previous else "published"
    cur = con.execute(
        """INSERT INTO report_versions
               (period_key, period_start, period_end, caliber, version_no,
                status, restate_reason, change_id, totals_json)
         VALUES (?,?,?,?,?,?,?,?,?)""",
        (period_key, p_start, p_end, caliber, version_no, status,
         restate_reason, change_id, to_json(
             {"rows": rows,
              "total_qty": sum(r["qty"] for r in rows),
              "total_amount": round(sum(r["amount"] for r in rows), 2)})))
    return {"report_id": cur.lastrowid, "period_key": period_key,
            "caliber": caliber, "version_no": version_no, "status": status,
            "rows": rows}


def restate_for_backfill(con: sqlite3.Connection, change_id: int,
                         scope: dict[str, Any],
                         reason: str) -> list[dict[str, Any]]:
    """Re-publish only the affected months after a backfill.

    Already-published pointwise reports get a new version carrying the
    restatement reason; months without a published report are skipped.  The
    pending recompute jobs created by the backfill are marked done/skipped in
    place rather than duplicated.
    """
    results = []
    for period_key in scope["periods"]:
        existing = con.execute(
            """SELECT id FROM report_versions
                WHERE period_key=? AND caliber='pointwise'
                  AND status IN ('published','restated')""",
            (period_key,),
        ).fetchall()
        if not existing:
            con.execute(
                """UPDATE recompute_jobs SET status='skipped',
                       finished_at=datetime('now')
                     WHERE change_id=? AND period_key=? AND status='pending'""",
                (change_id, period_key))
            continue
        result = publish_report(
            con, period_key, "pointwise",
            restate_reason=f"补录调整重述: {reason}",
            change_id=change_id)
        con.execute(
            """UPDATE recompute_jobs SET status='done',
                   finished_at=datetime('now')
                 WHERE change_id=? AND period_key=? AND status='pending'""",
            (change_id, period_key))
        results.append(result)
    return results


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _queue_recompute(con: sqlite3.Connection, change_id: int,
                     scope: dict[str, Any]) -> dict[str, Any]:
    if not scope["periods"]:
        return {"periods": [], "status": "no-sales-in-scope"}
    for period_key in scope["periods"]:
        con.execute(
            """INSERT INTO recompute_jobs
                   (change_id, period_key, affected_products_json,
                    date_from, date_to, status)
             VALUES (?,?,?,?,?, 'pending')""",
            (change_id, period_key, to_json(scope["products"]),
             scope["date_from"], scope["date_to"]))
    return {"periods": scope["periods"], "status": "pending"}


def _log_change(con: sqlite3.Connection, change_type: str, title: str,
                reason: Optional[str], eff_date: str,
                payload: dict[str, Any]) -> int:
    cur = con.execute(
        """INSERT INTO changes
               (change_type, title, reason, effective_date, payload_json)
         VALUES (?,?,?,?,?)""",
        (change_type, title, reason, eff_date, to_json(payload)))
    return int(cur.lastrowid)


def _must_category(con: sqlite3.Connection, cat_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM categories WHERE id=?",
                      (cat_id,)).fetchone()
    if row is None:
        raise TemporalConflict(f"分类不存在: {cat_id}")
    return row


def _must_product(con: sqlite3.Connection, product_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM products WHERE id=?",
                      (product_id,)).fetchone()
    if row is None:
        raise TemporalConflict(f"商品不存在: {product_id}")
    return row
