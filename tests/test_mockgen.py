"""T2 测试：mock 生成器。

- 单元（不依赖库）：种子可复现、量级、流量网格、字段与枚举有效性、故事指标（内存统计）。
- 集成（依赖本机 PG）：连库用 §5.2 口径 SQL 复核故事指标区间；DDL 约束与只读权限断言。
  集成用例在库缺失或未生成数据时自动 skip（先运行 `uv run python -m data.mock_gen`）。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
import pytest
from dotenv import dotenv_values

from data import mock_gen, stories  # noqa: E402

S1, S2, S3 = stories.STORY1, stories.STORY2, stories.STORY3


@pytest.fixture(scope="module")
def ds():
    return mock_gen.build(seed=stories.SEED)


# ---------- 单元：可复现与分布 ----------

def test_same_seed_reproducible() -> None:
    a = mock_gen.build(seed=314, scale=0.06)
    b = mock_gen.build(seed=314, scale=0.06)
    assert a.stats["counts"] == b.stats["counts"]
    assert a.orders[:200] == b.orders[:200]
    assert a.items[:500] == b.items[:500]
    assert a.stats["story1"] == b.stats["story1"]


def test_different_seed_differs() -> None:
    a = mock_gen.build(seed=1, scale=0.06)
    b = mock_gen.build(seed=2, scale=0.06)
    assert a.orders != b.orders
    assert a.stats["counts"]["orders"] > 0


def test_volumes_in_range(ds) -> None:
    c = ds.stats["counts"]
    for key, tgt in mock_gen.TARGETS.items():
        if key in ("products", "traffic_daily", "promotions"):
            assert c[key] == tgt, key
        else:
            assert abs(c[key] - tgt) / tgt <= mock_gen.TOL, key
    # 六种状态都有出现
    assert set(ds.stats["status"]) == {
        "pending_payment", "paid", "shipped", "completed", "cancelled", "refunded"}


def test_traffic_grid_complete(ds) -> None:
    days = (stories.DATA_END - stories.DATA_START).days + 1
    assert len(ds.traffic) == days * 6 * 4
    seen = set()
    for d, l1, ch, pv, uv in ds.traffic:
        key = (d, l1, ch)
        assert key not in seen
        seen.add(key)
        assert stories.DATA_START <= d <= stories.DATA_END
        assert l1 in stories.CATEGORY_L1 and ch in mock_gen.CHANNELS
        assert uv >= 2 and pv > uv


def test_order_fields_valid(ds) -> None:
    promo_ids = {p[0] for p in ds.promotions}
    for _oid, uid, status, ch, promo_id, created, paid in ds.orders:
        assert status in mock_gen.STATUS_SET
        assert ch in mock_gen.CHANNELS
        assert uid >= 1
        if status in ("pending_payment", "cancelled"):
            assert paid is None
        else:
            assert paid is not None and created <= paid
        assert promo_id is None or promo_id in promo_ids


def test_item_fields_valid(ds) -> None:
    order_ids = {o[0] for o in ds.orders}
    product_ids = {p[0] for p in ds.products}
    for _iid, oid, pid, qty, price in ds.items:
        assert oid in order_ids
        assert pid in product_ids
        assert 1 <= qty <= 10
        assert price > 0


def test_products_shape(ds) -> None:
    assert len(ds.products) == 2000
    valid_pairs = {(l1, l2) for l1 in stories.CATEGORY_L1 for l2 in stories.CATEGORY_L2[l1]}
    names = set()
    for _pid, name, l1, l2, brand, lp in ds.products:
        assert (l1, l2) in valid_pairs
        assert brand in stories.BRANDS[l1]
        assert lp >= 10
        names.add(name)
    assert len(names) == 2000  # 商品名唯一


def test_story1_in_memory(ds) -> None:
    s = ds.stats["story1"]
    lo, hi = S1["gmv_decline_range"]
    assert lo <= s["gmv_decline"] <= hi
    lo, hi = S1["uv_decline_range"]
    assert lo <= s["uv_decline"] <= hi
    # 流量与转化两侧都应为负（两个旋钮都生效），且可乘法分解还原总降幅
    assert s["uv_decline"] > 0 and s["conv_side"] < 0
    assert (1 - s["gmv_decline"]) - (1 - s["uv_decline"]) * (1 + s["conv_side"]) < 1e-9


def test_story2_in_memory(ds) -> None:
    s = ds.stats["story2"]
    assert S2["diff_range"][0] <= s["diff_pp"] <= S2["diff_range"][1]
    assert S2["rate_activity_range"][0] <= s["rate_activity"] <= S2["rate_activity_range"][1]
    assert S2["rate_base_range"][0] <= s["rate_baseline"] <= S2["rate_base_range"][1]
    assert s["n_activity"] > 500 and s["n_baseline"] > 500  # 样本量足够支撑评测


def test_story3_in_memory(ds) -> None:
    s = ds.stats["story3"]
    assert s["top1_vs_top2"] >= S3["min_top1_vs_top2"]
    assert s["stable_months"] == s["months_total"] >= S3["stable_months"]


def test_promotions_design() -> None:
    promos = stories.PROMOTIONS
    assert len(promos) == 30
    p618 = next(p for p in promos if p["name"] == S2["promo_name"])
    assert (p618["start"], p618["end"]) == (date(2026, 5, 31), date(2026, 6, 18))
    assert any(p["name"] == "双11大促" and p["start"] == date(2025, 11, 1)
               and p["end"] == date(2025, 11, 11) for p in promos)
    for p in promos:
        assert stories.DATA_START <= p["start"] <= p["end"] <= stories.DATA_END
        assert p["cat"] is None or p["cat"] in stories.CATEGORY_L1
    # 同一 l1 上活动互不重叠（cat=None 视为覆盖全部 l1）
    for i, a in enumerate(promos):
        for b in promos[i + 1:]:
            l1a = set(stories.CATEGORY_L1) if a["cat"] is None else {a["cat"]}
            l1b = set(stories.CATEGORY_L1) if b["cat"] is None else {b["cat"]}
            if l1a & l1b:
                assert a["end"] < b["start"] or b["end"] < a["start"], (a["name"], b["name"])
    # 故事 1 区间（05-21~06-30）内护肤不受除 618 外的活动搅动
    zone = (date(2026, 5, 21), date(2026, 6, 30))
    for p in promos:
        if p["id"] == p618["id"]:
            continue
        if p["cat"] in (None, "护肤"):
            assert p["end"] < zone[0] or p["start"] > zone[1], p["name"]


# ---------- 集成：连库 ----------

_ENV = dotenv_values(Path(__file__).resolve().parents[1] / ".env")


def _connect(dsn_key: str):
    dsn = _ENV.get(dsn_key)
    if not dsn:
        pytest.skip(f".env 缺少 {dsn_key}")
    try:
        return psycopg.connect(dsn, connect_timeout=3)
    except psycopg.Error:
        pytest.skip("本机 PG 不可连")


@pytest.fixture()
def ro():
    conn = _connect("PG_DSN_RO")
    try:
        if conn.execute("SELECT count(*) FROM orders").fetchone()[0] < 1000:
            pytest.skip("库中无 mock 数据（先运行 uv run python -m data.mock_gen）")
        yield conn
    finally:
        conn.close()


def test_db_self_check_all_pass(ro) -> None:
    st = mock_gen.self_check(ro)
    rows = mock_gen._evaluate(st)
    failed = [r for r in rows if not r[3]]
    assert not failed, failed
    # SQL 实测与生成器内存统计一致（同种子同数据）
    ds = mock_gen.build(seed=stories.SEED)
    assert st["counts"]["orders"] == ds.stats["counts"]["orders"]
    assert abs(st["story1"]["gmv_decline"] - ds.stats["story1"]["gmv_decline"]) < 1e-9
    assert abs(st["story2"]["rate_activity"] - ds.stats["story2"]["rate_activity"]) < 1e-9


def test_db_ddl_constraints_exist() -> None:
    conn = _connect("PG_DSN")
    try:
        if conn.execute("SELECT count(*) FROM orders").fetchone()[0] < 1000:
            pytest.skip("库中无 mock 数据")
        q = "SELECT contype,count(*) FROM pg_constraint WHERE conrelid=%s::regclass GROUP BY 1"
        by_type = dict(conn.execute(q, ("orders",)).fetchall())
        assert by_type.get("c", 0) >= 3  # status 枚举 / paid_at>=created / 待支付空值规则
        assert by_type.get("f", 0) >= 1 and by_type.get("p", 0) >= 1  # promo 外键 + 主键
        by_type = dict(conn.execute(q, ("order_items",)).fetchall())
        assert by_type.get("f", 0) >= 2  # order_id / product_id 外键
        assert by_type.get("c", 0) >= 2  # quantity 范围 / unit_price 非负
    finally:
        conn.close()


def test_db_ro_privileges(ro) -> None:
    sel = ro.execute("SELECT has_table_privilege(current_user, 'orders', 'SELECT')").fetchone()[0]
    ins = ro.execute("SELECT has_table_privilege(current_user, 'orders', 'INSERT')").fetchone()[0]
    upd = ro.execute("SELECT has_table_privilege(current_user, 'products', 'UPDATE')").fetchone()[0]
    assert sel and not ins and not upd
