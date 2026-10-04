"""get_schema：库结构速览（T4）。

供 executor 检索表 / 字段 / 类型 / 枚举值 / 行数 / 示例行——LLM 写 SQL 前的
上下文素材，也是 Web 端 ``GET /api/schema`` 的数据源（T8 复用）。
连接走只读账号 + 连接级 5s 超时（复用 run_query.connect_ro）。

实现约束：本模块所有语句均为**模块级字面 SQL 常量**、无条件执行（无输入分支、
无动态组合）——列目录 / 行数 / 枚举值各一条 UNION ALL 聚合查询，示例行 5 条
LIMIT 3 常量全部取回后在 Python 侧按需挑选（总开销毫秒级）。白名单 5 表是
SPEC §5.1 固定契约；表名入参先过 TABLE_WHITELIST，白名单外在触库前抛 ValueError。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from psycopg.connection import Connection

from chatbi.tools.run_query import connect_ro
from chatbi.tools.validate_sql import TABLE_WHITELIST

# 低基数码列（DDL CHECK 或业务字典）：枚举值从库实测 DISTINCT 取，避免与数据漂移
ENUM_COLUMNS: dict[str, tuple[str, ...]] = {
    "orders": ("status", "channel"),
    "order_items": (),
    "products": ("category_l1", "category_l2", "brand"),
    "traffic_daily": ("category_l1", "channel"),
    "promotions": ("category_l1", "promo_type"),
}
_ENUM_VALUE_LIMIT = 64  # 单列枚举值上限（防御未来列基数膨胀）

# ---- 模块级字面 SQL 常量（全部无条件只读执行） ----

_COLUMNS_SQL = (
    "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name IN "
    "('orders', 'order_items', 'products', 'traffic_daily', 'promotions') "
    "ORDER BY table_name, ordinal_position"
)

_COUNTS_SQL = (
    "SELECT 'orders' AS t, count(*)::bigint AS n FROM orders "
    "UNION ALL SELECT 'order_items', count(*)::bigint FROM order_items "
    "UNION ALL SELECT 'products', count(*)::bigint FROM products "
    "UNION ALL SELECT 'traffic_daily', count(*)::bigint FROM traffic_daily "
    "UNION ALL SELECT 'promotions', count(*)::bigint FROM promotions"
)

# 9 个 (表, 列) 一条 UNION ALL，每支内部 GROUP BY 第 3 列；
# 频次排序与 _ENUM_VALUE_LIMIT 截断在 Python 侧完成
_ENUMS_SQL = (
    "SELECT 'orders' AS t, 'status' AS c, status::text AS v, count(*)::bigint AS n "
    "FROM orders GROUP BY 3 "
    "UNION ALL SELECT 'orders', 'channel', channel::text, count(*)::bigint FROM orders GROUP BY 3 "
    "UNION ALL SELECT 'products', 'category_l1', category_l1::text, count(*)::bigint "
    "FROM products GROUP BY 3 "
    "UNION ALL SELECT 'products', 'category_l2', category_l2::text, count(*)::bigint "
    "FROM products GROUP BY 3 "
    "UNION ALL SELECT 'products', 'brand', brand::text, count(*)::bigint FROM products GROUP BY 3 "
    "UNION ALL SELECT 'traffic_daily', 'category_l1', category_l1::text, count(*)::bigint "
    "FROM traffic_daily GROUP BY 3 "
    "UNION ALL SELECT 'traffic_daily', 'channel', channel::text, count(*)::bigint "
    "FROM traffic_daily GROUP BY 3 "
    "UNION ALL SELECT 'promotions', 'category_l1', category_l1::text, count(*)::bigint "
    "FROM promotions GROUP BY 3 "
    "UNION ALL SELECT 'promotions', 'promo_type', promo_type::text, count(*)::bigint "
    "FROM promotions GROUP BY 3"
)

_SAMPLES_ORDERS = "SELECT * FROM orders LIMIT 3"
_SAMPLES_ITEMS = "SELECT * FROM order_items LIMIT 3"
_SAMPLES_PRODUCTS = "SELECT * FROM products LIMIT 3"
_SAMPLES_TRAFFIC = "SELECT * FROM traffic_daily LIMIT 3"
_SAMPLES_PROMOS = "SELECT * FROM promotions LIMIT 3"


def _jsonable(value: object) -> object:
    """JSON 友好化：日期时间 / Decimal 转字符串，其余原样。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _fetch(conn: Connection) -> dict:
    """一次性取回全部白名单表的结构素材（8 条模块常量查询，均无条件执行）。"""
    columns: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(_COLUMNS_SQL).fetchall():
        columns[r["table_name"]].append(
            {
                "name": r["column_name"],
                "type": r["data_type"],
                "nullable": r["is_nullable"] == "YES",
            }
        )
    counts = {r["t"]: int(r["n"]) for r in conn.execute(_COUNTS_SQL).fetchall()}
    raw_enums: dict[tuple[str, str], list[tuple[object, int]]] = defaultdict(list)
    for r in conn.execute(_ENUMS_SQL).fetchall():
        raw_enums[(r["t"], r["c"])].append((r["v"], int(r["n"])))
    samples = {
        "orders": conn.execute(_SAMPLES_ORDERS).fetchall(),
        "order_items": conn.execute(_SAMPLES_ITEMS).fetchall(),
        "products": conn.execute(_SAMPLES_PRODUCTS).fetchall(),
        "traffic_daily": conn.execute(_SAMPLES_TRAFFIC).fetchall(),
        "promotions": conn.execute(_SAMPLES_PROMOS).fetchall(),
    }
    return {
        "columns": dict(columns),
        "counts": counts,
        "enums": raw_enums,
        "samples": samples,
    }


def _enum_values(raw: dict[tuple[str, str], list], table: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for col in ENUM_COLUMNS.get(table, ()):
        vals = raw.get((table, col), [])
        if not vals:
            continue
        ordered = sorted(vals, key=lambda x: (-x[1], str(x[0])))
        out[col] = [v for v, _ in ordered][:_ENUM_VALUE_LIMIT]
    return out


def get_schema(table: str | None = None) -> dict:
    """表结构速览：``table`` 省略时返回全部白名单表；表名不在白名单抛 ValueError。"""
    tables = [table.strip().lower()] if table else sorted(TABLE_WHITELIST)
    unknown = [t for t in tables if t not in TABLE_WHITELIST]
    if unknown:
        raise ValueError(f"表 {unknown} 不在白名单 {sorted(TABLE_WHITELIST)} 内")

    with connect_ro() as conn:
        data = _fetch(conn)
    return {
        "tables": [
            {
                "table": t,
                "row_count": data["counts"][t],
                "columns": data["columns"][t],
                "enum_values": _enum_values(data["enums"], t),
                "sample_rows": [
                    {k: _jsonable(v) for k, v in row.items()} for row in data["samples"][t]
                ],
            }
            for t in tables
        ]
    }
