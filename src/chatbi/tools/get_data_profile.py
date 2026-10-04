"""get_data_profile：列画像（T4）——distinct top-k / min / max / null 率。

空集 / 口径不符纠错的回灌素材（SPEC §6.2 reflect 节点消费本工具产出）。

名称安全：表名先过 TABLE_WHITELIST；列名用字面 SQL + 参数绑定查
information_schema 验证存在性后，才以 psycopg.sql.Identifier（官方安全引用，
标识符无法走参数绑定）组合进数据查询；top_k 阈值以 sql.Literal 下发，不拼串。
"""

from __future__ import annotations

from psycopg import sql

from chatbi.tools.get_schema import _jsonable
from chatbi.tools.run_query import connect_ro
from chatbi.tools.validate_sql import TABLE_WHITELIST

_TOP_K_MAX = 1000
_TOP_K_DEFAULT = 10

# 列存在性与类型：字面 SQL，表名 / 列名经参数绑定
_COLUMN_TYPE_SQL = (
    "SELECT data_type FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s"
)


def get_data_profile(table: str, column: str, *, top_k: int = _TOP_K_DEFAULT) -> dict:
    """列画像：distinct top-k、min / max、null 率。表名不在白名单抛 ValueError。"""
    t, c = table.strip().lower(), column.strip().lower()
    if t not in TABLE_WHITELIST:
        raise ValueError(f"表 {t!r} 不在白名单 {sorted(TABLE_WHITELIST)} 内")
    k = max(1, min(int(top_k), _TOP_K_MAX))
    with connect_ro() as conn:
        row = conn.execute(_COLUMN_TYPE_SQL, (t, c)).fetchone()
        if row is None:
            raise ValueError(f"表 {t} 没有字段 {c!r}（可用字段见 get_schema）")
        tbl, col = sql.Identifier(t), sql.Identifier(c)
        stats = conn.execute(
            sql.SQL(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE {} IS NULL) AS null_count, "
                "count(DISTINCT {}) AS distinct_count, "
                "min({}) AS min_value, max({}) AS max_value FROM {}"
            ).format(col, col, col, col, tbl)
        ).fetchone()
        top = conn.execute(
            sql.SQL(
                "SELECT {} AS value, count(*) AS cnt FROM {} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {}"
            ).format(col, tbl, sql.Literal(k))
        ).fetchall()
    total = stats["total"]
    return {
        "table": t,
        "column": c,
        "type": row["data_type"],
        "total": int(total),
        "null_count": int(stats["null_count"]),
        "null_rate": (int(stats["null_count"]) / total) if total else 0.0,
        "distinct_count": int(stats["distinct_count"]),
        "min": _jsonable(stats["min_value"]),
        "max": _jsonable(stats["max_value"]),
        "top": [{"value": _jsonable(r["value"]), "count": int(r["cnt"])} for r in top],
    }
