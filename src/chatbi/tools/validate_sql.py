"""validate_sql：LLM 生成 SQL 的唯一入关口（SPEC §6.3 第 2 件套 / T4）。

sqlglot 按 PostgreSQL 方言解析 AST，白名单外一律拒收（拒收即不触库）：

- 仅接受单条只读 SELECT（含 CTE / 子查询 / UNION 等集合操作）；
- 语句级拒 DDL / DML / 多语句 / SELECT INTO / 无法解析的语句（含 psql 元命令），
  并全树扫描禁写节点，堵住「CTE 包裹 DML」的走私路径；
- 引用表（含子查询与 CTE 内层，表名大小写折叠）必须 ⊆ TABLE_WHITELIST；
  CTE 别名可被外层引用，但 CTE 自身来源表仍须在白名单内；
  表值函数（generate_series 等）无表名，按白名单外处理——v1 指标模板不需要；
- 输出 canonical 形态（去注释、统一格式），外层自动 LIMIT 1000：无 LIMIT 则补、
  已有更小值则保留、更大值或非字面量则截到 1000（取 min）；
- canonical 文本的 SHA256 即 SQL 指纹，T5 的重复执行拦截直接复用。
  指纹对空白 / 注释 / 尾分号稳定；标识符保留书写大小写（PG 端按 search_path 折叠）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# SPEC §6.3：引用表 ⊆ 5 张 mock 表
TABLE_WHITELIST = frozenset({"orders", "order_items", "products", "traffic_daily", "promotions"})

DEFAULT_LIMIT = 1000

# 顶层允许的只读查询形态：SELECT / 集合操作
_SELECT_LIKE = (exp.Select, exp.Union, exp.Intersect, exp.Except)

# 全树禁写节点：出现在任何位置（含 CTE 内层）都拒收。
# Command 是 sqlglot 对无法识别语句（EXPLAIN / VACUUM 等）的兜底形态，一并拒绝。
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Copy,
    exp.Command,
    exp.Set,
    exp.Use,
)

# 拒收码（机器可读，回灌 Reflexion 用）
CODE_EMPTY = "EMPTY"
CODE_PARSE_ERROR = "PARSE_ERROR"
CODE_MULTI_STATEMENT = "MULTI_STATEMENT"
CODE_NOT_SELECT = "NOT_SELECT"
CODE_TABLE_NOT_ALLOWED = "TABLE_NOT_ALLOWED"


@dataclass(frozen=True)
class ValidationResult:
    """validate_sql 结果：ok 时 sql 为 canonical 形态（含 LIMIT 处理）+ SHA256 指纹。"""

    ok: bool
    code: str | None  # 拒收码；ok 时为 None
    reason: str | None  # 拒收原因（人话，供回灌）；ok 时为 None
    sql: str | None  # canonical SQL；拒收时为 None
    fingerprint: str | None  # canonical 的 SHA256 hex；拒收时为 None


def _reject(code: str, reason: str) -> ValidationResult:
    return ValidationResult(ok=False, code=code, reason=reason, sql=None, fingerprint=None)


def _literal_limit(stmt: exp.Expression) -> int | None:
    """读外层现有 LIMIT 的字面量值；无 / 非字面量返回 None。"""
    limit = stmt.args.get("limit")
    if not isinstance(limit, exp.Limit):
        return None
    lit = limit.expression
    if isinstance(lit, exp.Literal) and lit.is_int and not lit.is_string:
        return int(lit.name)
    return None


def _apply_limit(stmt: exp.Expression, default_limit: int) -> None:
    """外层自动 LIMIT：无则补、更大或非字面量则截到 default_limit（取 min）。"""
    current = _literal_limit(stmt)
    if current is None or current > default_limit:
        stmt.set("limit", exp.Limit(expression=exp.Literal.number(default_limit)))


def validate_sql(sql: str, *, default_limit: int = DEFAULT_LIMIT) -> ValidationResult:
    """校验并规范化一条 SQL；不触库，纯内存操作。"""
    if not sql or not sql.strip():
        return _reject(CODE_EMPTY, "SQL 为空")

    try:
        stmts = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as err:
        return _reject(CODE_PARSE_ERROR, f"无法解析为 SQL：{err}")

    stmts = [s for s in stmts if s is not None and not isinstance(s, exp.Semicolon)]
    if not stmts:
        return _reject(CODE_EMPTY, "SQL 为空（仅含分号或空白）")
    if len(stmts) > 1:
        return _reject(
            CODE_MULTI_STATEMENT, f"仅允许单条语句，解析出 {len(stmts)} 条（禁止多语句拼接）"
        )

    stmt = stmts[0]
    if not isinstance(stmt, _SELECT_LIKE):
        return _reject(CODE_NOT_SELECT, f"仅允许单条只读 SELECT，收到 {type(stmt).__name__}")
    if stmt.args.get("into") is not None:
        return _reject(CODE_NOT_SELECT, "SELECT ... INTO 带写目标，不允许")
    for node_type in _FORBIDDEN_NODES:
        node = stmt.find(node_type)
        if node is not None:
            return _reject(
                CODE_NOT_SELECT, f"语句内含非只读节点 {node_type.__name__.upper()}（含 CTE 内层）"
            )

    # 表白名单：CTE 别名可被引用，但白名单外表（含 CTE 内层、系统表、未知表）拒收
    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    referenced = {t.name.lower() for t in stmt.find_all(exp.Table)}
    illegal = sorted(referenced - TABLE_WHITELIST - cte_names)
    if illegal:
        listed = ", ".join("(匿名表值函数)" if not x else repr(x) for x in illegal)
        return _reject(
            CODE_TABLE_NOT_ALLOWED,
            f"引用了白名单之外的表：{listed}；仅允许 {sorted(TABLE_WHITELIST)}",
        )

    _apply_limit(stmt, default_limit)
    canonical = stmt.sql(dialect="postgres", comments=False)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ValidationResult(ok=True, code=None, reason=None, sql=canonical, fingerprint=fingerprint)
