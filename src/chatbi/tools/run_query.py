"""run_query：只读查询出口（SPEC §6.3 第 1/3/4 件套 / T4）。

- 连接一律走只读账号 ``PG_DSN_RO``（库 ``chatbi``）：statement_timeout 经 libpq
  连接参数 ``options`` 下发（5s，自连接建立即生效，不经过 SQL 文本）；
- 执行前强制过 validate_sql（防绕过：非法 SQL 在开连接之前即被拒收），
  实际下发与执行的都是 canonical 形态，注释无法夹带任何内容；整条查询本身
  就是执行对象而非可绑定的「值」，统一以 execute(stmt, ()) 形态调用——
  语句不含参数占位，空参数即事实，语句文本只来自 AST 校验+规范化的可信片段；
- 执行前 EXPLAIN (FORMAT JSON) 取计划估算行数，扫描行数 > 50 万记 WARN 日志
  （不做硬熔断，SPEC §6.3 第 4 件套）；
- SQL 错误 / 超时不抛异常，转为 ok=False 的 QueryResult（含原因，供 Reflexion 回灌）；
  仅配置缺失（无 PG_DSN_RO）这类环境问题向上抛 ToolsConfigError。

connect_ro 同时是 get_schema / get_data_profile 的连接入口（同账号同时限）。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

from chatbi.tools.validate_sql import validate_sql

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 5_000  # SPEC §6.3：语句超时 5s（连接级，libpq options 下发）
SCAN_WARN_ROWS = 500_000  # 扫描行数阈值：超过记 WARN，不硬熔断

_EXPLAIN_PREFIX = sql.SQL("EXPLAIN (FORMAT JSON) ")


class ToolsConfigError(RuntimeError):
    """环境配置缺失（.env 无 PG_DSN_RO 等），属部署问题而非 SQL 问题。"""


@dataclass(frozen=True)
class QueryResult:
    """run_query 结果：rows 为 dict 行（受自动 LIMIT 约束，≤1000 行）。"""

    ok: bool
    sql: str | None  # 实际执行的 canonical SQL；未过校验时为 None
    fingerprint: str | None
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    ms: float = 0.0
    scanned_rows_est: int | None = None  # EXPLAIN 计划估算的最大节点行数
    error: str | None = None


def connect_ro(*, statement_timeout_ms: int = STATEMENT_TIMEOUT_MS) -> psycopg.Connection:
    """只读账号连接：autocommit + dict 行；超时经连接参数生效，非 SQL 文本。"""
    load_dotenv()
    dsn = os.environ.get("PG_DSN_RO")
    if not dsn:
        raise ToolsConfigError("缺少 PG_DSN_RO（检查 .env，模板见 .env.example）")
    # libpq 连接参数（非 SQL 组装）；值为模块常量经 int() 收敛
    options = f"-c statement_timeout={int(statement_timeout_ms)}"
    return psycopg.connect(
        dsn, connect_timeout=3, autocommit=True, options=options, row_factory=dict_row
    )


def _max_plan_rows(plan: dict) -> int:
    """计划树所有节点的最大估算行数（近似扫描规模；对含 LIMIT 的计划也可见扫描量）。"""
    stack, worst = [plan], 0
    while stack:
        node = stack.pop()
        worst = max(worst, int(node.get("Plan Rows", 0) or 0))
        stack.extend(node.get("Plans", []))
    return worst


def _explain_scan_rows(conn: psycopg.Connection, canonical: str) -> tuple[int | None, str | None]:
    """先 EXPLAIN：返回 (估算扫描行数, None) 或 (None, 错误文本)。

    canonical 为 validate_sql 产出的 AST 规范化片段（单条只读 SELECT），
    与 Composable 前缀组合后作为可信语句下发；EXPLAIN 语义上不接受参数占位。
    """
    stmt = _EXPLAIN_PREFIX + sql.SQL(canonical)
    try:
        row = conn.execute(stmt).fetchone()
    except psycopg.Error as err:  # 语义错误（字段不存在等）：EXPLAIN 阶段即暴露
        return None, f"SQL 无法执行：{err}"
    plan = (row["QUERY PLAN"] or [{}])[0].get("Plan", {})
    return _max_plan_rows(plan), None


def run_query(sql_str: str) -> QueryResult:
    """校验并执行一条只读 SQL；任何 SQL 层失败（非法 / 报错 / 超时）都收敛进结果。"""
    t0 = time.perf_counter()
    v = validate_sql(sql_str)
    if not v.ok:
        return QueryResult(ok=False, sql=None, fingerprint=None, error=f"[{v.code}] {v.reason}")
    elapsed = lambda: round((time.perf_counter() - t0) * 1000, 1)  # noqa: E731

    try:
        with connect_ro() as conn:
            scanned, plan_error = _explain_scan_rows(conn, v.sql)
            if plan_error is not None:
                return QueryResult(
                    ok=False,
                    sql=v.sql,
                    fingerprint=v.fingerprint,
                    ms=elapsed(),
                    scanned_rows_est=None,
                    error=plan_error,
                )
            if scanned is not None and scanned > SCAN_WARN_ROWS:
                logger.warning(
                    "扫描行数估算 %s 超过阈值 %s（指纹 %s）：仅记录，不熔断",
                    f"{scanned:,}",
                    f"{SCAN_WARN_ROWS:,}",
                    v.fingerprint[:12],
                )
            # canonical 无参数占位：空参数即事实（语句文本为校验后的可信片段）
            rows = conn.execute(v.sql, ()).fetchall()
    except psycopg.errors.QueryCanceled as err:
        return QueryResult(
            ok=False,
            sql=v.sql,
            fingerprint=v.fingerprint,
            ms=elapsed(),
            error=f"查询超时（statement_timeout={STATEMENT_TIMEOUT_MS}ms）被取消：{err}",
        )
    except psycopg.Error as err:
        return QueryResult(
            ok=False,
            sql=v.sql,
            fingerprint=v.fingerprint,
            ms=elapsed(),
            error=f"数据库错误：{err}",
        )
    return QueryResult(
        ok=True,
        sql=v.sql,
        fingerprint=v.fingerprint,
        rows=[dict(r) for r in rows],
        row_count=len(rows),
        ms=elapsed(),
        scanned_rows_est=scanned,
        error=None,
    )
