"""T4 测试：工具层四件套（get_schema / get_data_profile / validate_sql / run_query）。

先测后写（T4 ⑥）：本用例集即验收清单——
- 单元（不触库）：validate_sql 恶意/边界用例集 ≥12 条（SPEC §6.3 第 2 件套：
  单条 SELECT / 表白名单 / 拒 DDL DML 多语句 / 自动 LIMIT(1000) 取 min /
  canonical 规范化 + SHA256 指纹）；
- 集成（本机 PG + mock 数据，缺失自动 skip）：四件工具真实行为——超时 5s 截断
  pg_sleep、扫描行数 >50 万记 WARN、自动 LIMIT 实测、只读账号写拒绝。
"""

from __future__ import annotations

import importlib
import logging
import time

import psycopg
import pytest

from chatbi.tools import (
    TABLE_WHITELIST,
    get_data_profile,
    get_schema,
    run_query,
    validate_sql,
)

# 包 __init__ 重导出的函数 run_query 会遮蔽同名子模块属性，
# 经 import_module 从 sys.modules 取真实模块（monkeypatch / connect_ro 用）
rq = importlib.import_module("chatbi.tools.run_query")

STATUS_SET = {
    "pending_payment",
    "paid",
    "shipped",
    "completed",
    "cancelled",
    "refunded",
}


# ---------- 断言助手 ----------


def _reject(sql: str, *, code: str | None = None, needle: str = "") -> None:
    res = validate_sql(sql)
    assert res.ok is False, f"应被拒收: {sql!r} -> {res.sql!r}"
    assert res.sql is None and res.fingerprint is None
    if code:
        assert res.code == code, f"{sql!r}: code={res.code}"
    if needle:
        assert needle.lower() in res.reason.lower(), f"{sql!r}: reason={res.reason!r}"


def _pass(sql: str, *, expect_sql: str | None = None) -> str:
    res = validate_sql(sql)
    assert res.ok is True, f"应通过: {sql!r} -> [{res.code}] {res.reason}"
    assert res.reason is None and res.code is None
    assert res.sql and res.fingerprint and len(res.fingerprint) == 64
    if expect_sql:
        assert res.sql == expect_sql, res.sql
    return res.sql


# ---------- 单元：validate_sql 恶意/边界用例集（不触库） ----------


# 1) DML：UPDATE / DELETE / INSERT 一律拒绝
def test_reject_update() -> None:
    _reject("UPDATE orders SET status = 'paid'", code="NOT_SELECT", needle="UPDATE")


def test_reject_delete() -> None:
    _reject("DELETE FROM orders", code="NOT_SELECT", needle="DELETE")


def test_reject_insert() -> None:
    _reject(
        "INSERT INTO orders VALUES (1, 1, 'paid', 'app', NULL, now(), now())",
        code="NOT_SELECT",
        needle="INSERT",
    )


# 2) DDL / 非只读语句：DROP、COPY、VACUUM 拒绝
def test_reject_drop() -> None:
    _reject("DROP TABLE orders", code="NOT_SELECT", needle="DROP")


def test_reject_copy_and_vacuum() -> None:
    _reject("COPY orders TO '/tmp/x'", code="NOT_SELECT")
    _reject("VACUUM orders", code="NOT_SELECT")


# 3) 多语句拒绝（含「SELECT 打头 + 尾随写语句」的拼接攻击）
def test_reject_multi_statement() -> None:
    _reject("SELECT order_id FROM orders; DELETE FROM orders", code="MULTI_STATEMENT")
    _reject("SELECT 1; SELECT 2", code="MULTI_STATEMENT")


# 4) 白名单之外的表拒绝（系统表 / 不存在的表）
def test_reject_foreign_table() -> None:
    _reject("SELECT * FROM pg_catalog.pg_class", code="TABLE_NOT_ALLOWED", needle="pg_class")


def test_reject_unknown_table() -> None:
    _reject("SELECT * FROM users", code="TABLE_NOT_ALLOWED", needle="users")


# 5) 解析失败 / 空语句拒绝（含 psql 元命令）
def test_reject_parse_error() -> None:
    _reject("SELEC order_id FROMM orders", code="PARSE_ERROR")
    _reject("\\dt", code="PARSE_ERROR")


def test_reject_empty() -> None:
    _reject("", code="EMPTY")
    _reject("   \n  ", code="EMPTY")


# 6) SELECT INTO（写目标）拒绝
def test_reject_select_into() -> None:
    _reject("SELECT order_id, user_id INTO tmp FROM orders", code="NOT_SELECT", needle="INTO")


# 7) CTE 包裹 DML 的走私拒绝（顶层是 SELECT，但 CTE 内是 UPDATE）
def test_reject_cte_wrapped_dml() -> None:
    _reject(
        "WITH x AS (UPDATE orders SET status = 'paid' RETURNING *) SELECT * FROM x",
        code="NOT_SELECT",
        needle="UPDATE",
    )


# 8) 合法：CTE（引用名放行，内层表须在白名单）
def test_allow_cte() -> None:
    _pass("WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT count(*) FROM paid")
    # CTE 内引用白名单外表同样拒绝
    _reject(
        "WITH x AS (SELECT * FROM users) SELECT * FROM x", code="TABLE_NOT_ALLOWED", needle="users"
    )


# 9) 合法：子查询
def test_allow_subquery() -> None:
    _pass(
        "SELECT order_id FROM orders WHERE user_id IN "
        "(SELECT user_id FROM orders WHERE channel = 'live')"
    )


# 10) 合法：UNION（集合操作仍是只读 SELECT）
def test_allow_union() -> None:
    _pass("SELECT order_id FROM orders UNION SELECT order_id FROM order_items")


# 11) 合法：尾随分号 / schema 限定 / 大小写折叠
def test_allow_trailing_semicolon() -> None:
    _pass("SELECT 1;")


def test_allow_schema_qualified_and_case_fold() -> None:
    _pass("SELECT count(*) FROM public.orders")
    _pass("SELECT count(*) FROM ORDERS")


# 12) LIMIT 规则：无则补 1000；小值保留；大值截到 1000（取 min）
def test_no_limit_gets_1000() -> None:
    _pass("SELECT order_id FROM orders", expect_sql="SELECT order_id FROM orders LIMIT 1000")


def test_small_limit_kept() -> None:
    _pass("SELECT order_id FROM orders LIMIT 5", expect_sql="SELECT order_id FROM orders LIMIT 5")
    _pass(
        "SELECT order_id FROM orders LIMIT 5 OFFSET 10",
        expect_sql="SELECT order_id FROM orders LIMIT 5 OFFSET 10",
    )


def test_large_limit_capped() -> None:
    _pass(
        "SELECT order_id FROM orders LIMIT 5000",
        expect_sql="SELECT order_id FROM orders LIMIT 1000",
    )


def test_non_literal_limit_replaced() -> None:
    sql = _pass("SELECT order_id FROM orders LIMIT 2 + 3")
    assert sql.endswith("LIMIT 1000"), sql


# 13) 注释绕过尝试：注释内容不进 canonical，也无法夹带第二条语句
def test_comment_smuggle_neutralized() -> None:
    sql = _pass("SELECT order_id FROM orders -- ; DROP TABLE orders\nWHERE status = 'paid'")
    assert "--" not in sql
    assert "DROP" not in sql.upper()
    assert "WHERE" in sql  # 注释后的 WHERE 仍生效（未被注释吞掉语义）
    # 块注释同理
    sql2 = _pass("SELECT /* anything ; DELETE */ order_id FROM orders")
    assert "DELETE" not in sql2.upper()


# 14) canonical + 指纹：空白/注释/尾分号变体归一为同一指纹（T5 去重拦截的前提）
def test_canonical_fingerprint_stable() -> None:
    a = validate_sql("select  order_id\nfrom orders limit 1000")
    b = validate_sql("SELECT order_id FROM orders LIMIT 1000 -- trailing comment;")
    assert a.ok and b.ok
    assert a.sql == b.sql
    assert a.fingerprint == b.fingerprint


# 15) pg_sleep 是合法 SELECT：validate 不拒（超时截断是 run_query 的职责）
def test_pg_sleep_is_valid_select() -> None:
    _pass("SELECT pg_sleep(1)")


# ---------- 单元：白名单参数校验（不触库） + run_query 拒收路径不触库 ----------


def test_get_schema_unknown_table_raises() -> None:
    with pytest.raises(ValueError, match="白名单"):
        get_schema("products2")
    with pytest.raises(ValueError, match="白名单"):
        get_schema("pg_class")


def test_get_data_profile_unknown_table_raises() -> None:
    with pytest.raises(ValueError, match="白名单"):
        get_data_profile("information_schema.tables", "table_name")


def test_run_query_invalid_never_touches_db(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("非法 SQL 不应触库")

    monkeypatch.setattr(rq, "connect_ro", _boom)
    t0 = time.monotonic()
    res = run_query("UPDATE orders SET status = 'paid'")
    assert res.ok is False
    assert res.rows == [] and res.row_count == 0
    assert "NOT_SELECT" in (res.error or "")
    assert time.monotonic() - t0 < 0.5  # 未走网络即返回


# ---------- 集成：本机 PG + mock 数据（缺失自动 skip） ----------


@pytest.fixture(scope="module")
def db():
    try:
        res = run_query("SELECT count(*) AS n FROM orders")
    except Exception:  # noqa: BLE001 —— 环境不可用一律转 skip
        pytest.skip("本机 PG 不可连（先 brew services 起 PG）")
    if not (res.ok and res.rows[0]["n"] > 1000):
        pytest.skip("库中无 mock 数据（先运行 uv run python -m data.mock_gen）")


def test_run_query_happy(db) -> None:
    res = run_query("SELECT order_id, status FROM orders WHERE order_id <= 3")
    assert res.ok and res.error is None
    assert res.sql == "SELECT order_id, status FROM orders WHERE order_id <= 3 LIMIT 1000"
    assert res.row_count == 3
    assert set(res.rows[0]) == {"order_id", "status"}
    assert res.rows[0]["status"] in STATUS_SET
    assert res.ms >= 0 and res.fingerprint and len(res.fingerprint) == 64


def test_run_query_auto_limit_caps_rows(db) -> None:
    res = run_query("SELECT order_id FROM orders")
    assert res.ok
    assert res.row_count == 1000  # 自动 LIMIT 1000 真正限制了返回行数


def test_run_query_statement_timeout(db) -> None:
    t0 = time.monotonic()
    res = run_query("SELECT pg_sleep(6)")  # 超过 5s statement_timeout
    elapsed = time.monotonic() - t0
    assert res.ok is False
    assert "超时" in (res.error or "") or "timeout" in (res.error or "").lower()
    assert 4.5 <= elapsed <= 9.0  # 被 5s 截断，而非跑满 6s 或挂死


def test_run_query_missing_column_returns_error(db) -> None:
    res = run_query("SELECT nonexistent_col FROM orders")
    assert res.ok is False
    assert "nonexistent_col" in (res.error or "")


def test_run_query_scan_warn(db, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="chatbi.tools.run_query"):
        res = run_query("SELECT o.order_id FROM orders o, order_items oi")  # 估算笛卡尔积
    assert res.ok and res.row_count == 1000  # LIMIT 短路，执行很快
    assert res.scanned_rows_est is not None and res.scanned_rows_est > 500_000
    assert any("扫描行数" in rec.getMessage() for rec in caplog.records)


def test_run_query_ro_account_denies_write(db) -> None:
    with rq.connect_ro() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
            conn.execute("UPDATE orders SET status = 'paid'")


def test_get_schema_all(db) -> None:
    schema = get_schema()
    tables = {t["table"]: t for t in schema["tables"]}
    assert set(tables) == set(TABLE_WHITELIST)
    orders = tables["orders"]
    assert orders["row_count"] > 90_000
    cols = {c["name"] for c in orders["columns"]}
    assert {
        "order_id",
        "user_id",
        "status",
        "channel",
        "promo_id",
        "created_at",
        "paid_at",
    } <= cols
    assert set(orders["enum_values"]["status"]) == STATUS_SET
    assert set(orders["enum_values"]["channel"]) == {"app", "web", "mini", "live"}
    assert len(orders["sample_rows"]) == 3
    traffic = tables["traffic_daily"]
    assert set(traffic["enum_values"]["category_l1"]) == {
        "护肤",
        "彩妆",
        "香水",
        "女装",
        "男装",
        "配饰",
    }


def test_get_schema_single(db) -> None:
    schema = get_schema("products")
    assert [t["table"] for t in schema["tables"]] == ["products"]
    products = schema["tables"][0]
    types = {c["name"]: c["type"] for c in products["columns"]}
    assert "numeric" in types["list_price"]
    assert 25 <= len(products["enum_values"]["category_l2"]) <= 40  # SPEC：l2 约 30 个


def test_get_data_profile(db) -> None:
    p = get_data_profile("orders", "status")
    assert p["table"] == "orders" and p["column"] == "status"
    assert p["distinct_count"] == 6
    assert p["null_count"] == 0 and p["null_rate"] == 0.0
    assert sum(e["count"] for e in p["top"]) == p["total"]  # 6 个值全部进 top
    brand = get_data_profile("products", "brand")
    assert brand["distinct_count"] > 10
    paid = get_data_profile("orders", "paid_at")
    assert isinstance(paid["min"], str) and isinstance(paid["max"], str)  # isoformat
    assert paid["min"] <= paid["max"]


def test_get_data_profile_rejects_bad_column(db) -> None:
    with pytest.raises(ValueError, match="字段"):
        get_data_profile("orders", "status; DROP TABLE orders")  # 注入式字段名
    with pytest.raises(ValueError, match="字段"):
        get_data_profile("orders", "no_such_column")
