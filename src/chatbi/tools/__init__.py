"""chatbi 工具层（T4）：四件工具 + 只读边界四件套（SPEC §6.3）。

- ``validate_sql``：sqlglot AST 校验——单条 SELECT / 表白名单 / 拒 DDL DML 多语句 /
  自动 LIMIT(1000) 取 min / canonical 规范化 + SHA256 指纹；
- ``get_schema``：表 / 字段 / 类型 / 枚举值 / 行数 / 示例行（模块级字面 SQL）；
- ``get_data_profile``：distinct top-k / min / max / null 率（Identifier 安全组合）；
- ``run_query``：只读账号执行（statement_timeout=5s 经 libpq options、
  EXPLAIN 扫描行数 >50 万记 WARN、错误 / 超时收敛进 QueryResult 供 Reflexion 回灌）。
"""

from chatbi.tools.get_data_profile import get_data_profile
from chatbi.tools.get_schema import ENUM_COLUMNS, get_schema
from chatbi.tools.run_query import (
    SCAN_WARN_ROWS,
    STATEMENT_TIMEOUT_MS,
    QueryResult,
    ToolsConfigError,
    connect_ro,
    run_query,
)
from chatbi.tools.validate_sql import (
    DEFAULT_LIMIT,
    TABLE_WHITELIST,
    ValidationResult,
    validate_sql,
)

__all__ = [
    "DEFAULT_LIMIT",
    "ENUM_COLUMNS",
    "QueryResult",
    "SCAN_WARN_ROWS",
    "STATEMENT_TIMEOUT_MS",
    "TABLE_WHITELIST",
    "ToolsConfigError",
    "ValidationResult",
    "connect_ro",
    "get_data_profile",
    "get_schema",
    "run_query",
    "validate_sql",
]
