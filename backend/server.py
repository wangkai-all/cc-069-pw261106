"""Zero-dependency HTTP API + static file server for the workbench.

Run:  python3 server.py  (default :8000, DB at ../data/workbench.db)
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schema import SCHEMA
from temporal_core import (
    TemporalConflict,
    active_category,
    category_lineage,
    compare_calibers,
    current_category,
    detect_cycle,
    month_bounds,
    product_history,
    tree_snapshot,
    validate_all_dates,
)
import operations as ops

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")
DB_PATH = os.environ.get(
    "WORKBENCH_DB", os.path.join(BASE_DIR, "..", "data", "workbench.db"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def ensure_db() -> None:
    if not os.path.exists(DB_PATH):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        con = get_db()
        con.executescript(SCHEMA)
        con.commit()
        con.close()


# --------------------------------------------------------------------------- #
# API handlers — each returns (status, payload); payload dict -> JSON,
# bytes -> sent verbatim (CSV downloads).
# --------------------------------------------------------------------------- #
class Api:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    # ---- read endpoints ---------------------------------------------------
    def tree(self, q: dict) -> tuple[int, object]:
        on_date = q.get("date", [""])[0] or _latest_boundary(self.con)
        snap = tree_snapshot(self.con, on_date)
        nodes = {n["id"]: n for n in snap["nodes"]}
        # Attach live membership counts for the as-of date.
        counts = {r["category_id"]: r["n"] for r in self.con.execute(
            """SELECT category_id, COUNT(DISTINCT product_id) n
                 FROM product_memberships
                WHERE effective_from <= ?
                  AND (effective_to IS NULL OR effective_to > ?)
             GROUP BY category_id""", (on_date, on_date))}
        tree = []
        for root in snap["roots"]:
            tree.append(self._node_tree(snap, nodes, root, counts))
        archived = [{
            **a,
            "product_count": counts.get(a["id"], 0),
        } for a in snap["archived"]]
        return 200, {"date": on_date, "tree": tree, "archived": archived,
                     "validation": validate_all_dates(self.con)}

    def _node_tree(self, snap, nodes, cid, counts):
        return {
            **dict(nodes[cid]),
            "product_count": counts.get(cid, 0),
            "children": [self._node_tree(snap, nodes, c, counts)
                         for c in snap["children"].get(cid, [])],
        }

    def products(self, q) -> tuple[int, object]:
        rows = self.con.execute(
            """SELECT p.*,
                      (SELECT category_id FROM product_memberships m
                        WHERE m.product_id=p.id AND m.effective_to IS NULL
                        ORDER BY effective_from DESC LIMIT 1) AS current_category_id
                 FROM products p ORDER BY p.sku""").fetchall()
        return 200, [dict(r) for r in rows]

    def point_in_time(self, q) -> tuple[int, object]:
        on_date = q["date"][0]
        pid = q.get("product_id", [None])[0]
        products = self.con.execute("SELECT id, sku, name FROM products ORDER BY sku")
        result = []
        for p in products:
            if pid and p["id"] != pid:
                continue
            cat_id = active_category(self.con, p["id"], on_date)
            cat = self.con.execute(
                "SELECT id, code, name FROM categories WHERE id=?",
                (cat_id,)).fetchone() if cat_id else None
            result.append({
                "product_id": p["id"], "sku": p["sku"], "name": p["name"],
                "category": dict(cat) if cat else None,
            })
        return 200, {"date": on_date, "products": result}

    def history(self, q) -> tuple[int, object]:
        pid = q["product_id"][0]
        product = self.con.execute(
            "SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not product:
            return 404, {"error": "商品不存在"}
        return 200, {"product": dict(product),
                     "history": product_history(self.con, pid),
                     "current_category_id":
                         current_category(self.con, pid)}

    def lineage(self, q) -> tuple[int, object]:
        cid = q["category_id"][0]
        return 200, {"lineage": category_lineage(self.con, cid)}

    def changes(self, q) -> tuple[int, object]:
        rows = self.con.execute(
            """SELECT * FROM changes ORDER BY effective_date DESC, id DESC"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return 200, out

    def compare(self, q) -> tuple[int, object]:
        date_from = q["from"][0]
        date_to = q["to"][0]
        if date_to <= date_from:
            return 400, {"error": "结束日期必须晚于开始日期"}
        return 200, compare_calibers(self.con, date_from, date_to)

    def compare_export(self, q) -> tuple[int, object]:
        data = compare_calibers(self.con, q["from"][0], q["to"][0])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["商品SKU", "商品名称", "交易时分类(点时态)",
                         "当前分类", "是否重分类", "销量", "销售额"])
        for d in data["detail"]:
            writer.writerow([d["sku"], d["product_name"],
                             d["pointwise_category"], d["current_category"],
                             "是" if d["reclassified"] else "否",
                             d["qty"], d["amount"]])
        return 200, ("text/csv", buf.getvalue().encode("utf-8-sig"),
                     "caliber_diff.csv")

    def reports(self, q) -> tuple[int, object]:
        rows = self.con.execute(
            """SELECT * FROM report_versions
                ORDER BY period_key, caliber, version_no""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["totals"] = json.loads(d.pop("totals_json"))
            out.append(d)
        return 200, out

    def report_detail(self, q) -> tuple[int, object]:
        rid = int(q["id"][0])
        row = self.con.execute(
            "SELECT * FROM report_versions WHERE id=?", (rid,)).fetchone()
        if not row:
            return 404, {"error": "报表不存在"}
        d = dict(row)
        d["totals"] = json.loads(d.pop("totals_json"))
        return 200, d

    def jobs(self, q) -> tuple[int, object]:
        rows = self.con.execute(
            """SELECT j.*, c.title AS change_title FROM recompute_jobs j
                 JOIN changes c ON c.id = j.change_id
             ORDER BY j.id DESC""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["affected_products"] = json.loads(d.pop(
                "affected_products_json"))
            out.append(d)
        return 200, out

    def categories(self, q) -> tuple[int, object]:
        rows = self.con.execute(
            "SELECT * FROM categories ORDER BY code").fetchall()
        return 200, [dict(r) for r in rows]

    # ---- write endpoints --------------------------------------------------
    def create_category(self, body) -> tuple[int, object]:
        ops.create_category(
            self.con, body["code"], body["name"], body.get("parent_id"),
            body["effective_date"], reason=body.get("reason"))
        self.con.commit()
        return 201, {"ok": True}

    def move_category(self, body) -> tuple[int, object]:
        ops.move_category(
            self.con, body["child_id"], body["new_parent_id"],
            body["effective_date"], reason=body.get("reason"),
            backfill=bool(body.get("backfill")))
        self.con.commit()
        return 200, {"ok": True}

    def move_product(self, body) -> tuple[int, object]:
        result = ops.move_product(
            self.con, body["product_id"], body["target_category_id"],
            body["effective_date"], reason=body.get("reason"),
            backfill=bool(body.get("backfill")))
        if body.get("restate_reports", True) and result.get("recompute_job"):
            self._restate_from_job(body["effective_date"],
                                   body.get("reason", "补录调整"))
        self.con.commit()
        return 200, result

    def split(self, body) -> tuple[int, object]:
        result = ops.split_category(
            self.con, body["source_category_id"], body["targets"],
            body["effective_date"], reason=body.get("reason"))
        self.con.commit()
        return 200, result

    def merge(self, body) -> tuple[int, object]:
        result = ops.merge_categories(
            self.con, body["source_category_ids"], body["target_category_id"],
            body["effective_date"], reason=body.get("reason"))
        self.con.commit()
        return 200, result

    def publish_report(self, body) -> tuple[int, object]:
        result = ops.publish_report(
            self.con, body["period_key"], body["caliber"],
            restate_reason=body.get("restate_reason"))
        self.con.commit()
        return 201, result

    def validate(self, body) -> tuple[int, object]:
        problems = validate_all_dates(self.con)
        return 200, {"ok": not problems, "problems": problems}

    def _restate_from_job(self, eff_from: str, reason: str) -> None:
        change_id = self.con.execute(
            "SELECT MAX(id) AS id FROM changes").fetchone()["id"]
        scope = ops.affected_scope(
            self.con, [r["product_id"] for r in self.con.execute(
                "SELECT DISTINCT product_id FROM lineage_mappings "
                "WHERE change_id=?", (change_id,))], eff_from)
        ops.restate_for_backfill(self.con, change_id, scope, reason)


def _latest_boundary(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT MAX(effective_from) d FROM category_edges").fetchone()
    return row["d"] or "2026-01-01"


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
GET_ROUTES = {
    "/api/tree": "tree",
    "/api/products": "products",
    "/api/point-in-time": "point_in_time",
    "/api/history": "history",
    "/api/lineage": "lineage",
    "/api/changes": "changes",
    "/api/compare": "compare",
    "/api/compare/export": "compare_export",
    "/api/reports": "reports",
    "/api/reports/detail": "report_detail",
    "/api/jobs": "jobs",
    "/api/categories": "categories",
}
POST_ROUTES = {
    "/api/categories/create": "create_category",
    "/api/categories/move": "move_category",
    "/api/products/move": "move_product",
    "/api/categories/split": "split",
    "/api/categories/merge": "merge",
    "/api/reports/publish": "publish_report",
    "/api/validate": "validate",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CategoryWorkbench/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("[api] " + fmt % args + "\n")

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            handler_name = GET_ROUTES.get(parsed.path)
            if not handler_name:
                self._send_json(404, {"error": "未知接口"})
                return
            con = get_db()
            try:
                api = Api(con)
                status, result = getattr(api, handler_name)(
                    parse_qs(parsed.query))
                if isinstance(result, tuple):  # CSV download
                    mime, blob, filename = result
                    self.send_response(status)
                    self.send_header("Content-Type",
                                     f"{mime}; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", str(len(blob)))
                    self.end_headers()
                    self.wfile.write(blob)
                else:
                    self._send_json(status, result)
            except TemporalConflict as exc:
                self._send_json(409, {"error": str(exc)})
            except (KeyError, ValueError) as exc:
                self._send_json(400, {"error": f"参数错误: {exc}"})
            finally:
                con.close()
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        handler_name = POST_ROUTES.get(parsed.path)
        if not handler_name:
            self._send_json(404, {"error": "未知接口"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return
        con = get_db()
        try:
            api = Api(con)
            status, result = getattr(api, handler_name)(body)
            self._send_json(status, result)
        except TemporalConflict as exc:
            con.rollback()
            self._send_json(409, {"error": str(exc)})
        except (KeyError, ValueError) as exc:
            con.rollback()
            self._send_json(400, {"error": f"参数错误: {exc}"})
        except sqlite3.IntegrityError as exc:
            con.rollback()
            self._send_json(409, {"error": f"数据约束冲突: {exc}"})
        finally:
            con.close()

    def _serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        # Prevent path traversal.
        safe = os.path.normpath(path).lstrip("/")
        full = os.path.join(FRONTEND_DIR, safe)
        if not os.path.abspath(full).startswith(
                os.path.abspath(FRONTEND_DIR) + os.sep) or \
                not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        ext = os.path.splitext(full)[1]
        blob = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


def main() -> None:
    ensure_db()
    port = int(os.environ.get("PORT", "8000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"分类沿革工作台: http://localhost:{port}  (DB: {DB_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
