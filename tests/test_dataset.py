"""T9 测试：评测集标注（eval/dataset.yaml + eval/validate_dataset.py）。

validate_dataset.py 即自测脚本（T9 ⑥），本文件把它纳入 pytest——
- 单元（不触库）：结构校验（字段/分层计数/id 前缀/SQL lint）、期望值已连库写回；
- integration（本机 PG + mock 数据，缺失自动 skip）：--check 连库复核
  50/50 可执行且与写回值一致（防止手改 expected 或库重建后标注过期）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
import pytest  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from eval import validate_dataset as vd  # noqa: E402

# ---------- 单元：不触库 ----------


def test_dataset_schema_and_layer_counts() -> None:
    errors = vd.validate_schema(vd.load_dataset())
    assert not errors, errors


def test_dataset_expected_values_written_back() -> None:
    for q in vd.load_dataset()["questions"]:
        e = q["expected"]
        assert e["value"] is not None, f"{q['id']} 缺少实测 value（先跑 validate_dataset 写回）"
        assert e["direction"] in ("up", "down", "flat", "none"), q["id"]
        assert isinstance(e["extras"], dict), q["id"]


def test_dataset_sql_single_statement() -> None:
    for q in vd.load_dataset()["questions"]:
        assert vd.lint_sql(q["standard_sql"]) is None, q["id"]


def test_dataset_lint_catches_bad_sql() -> None:
    assert vd.lint_sql("UPDATE orders SET status = 'paid'") == "非 SELECT/WITH 开头"
    assert vd.lint_sql("SELECT 1; SELECT 2") == "含多条语句"
    assert vd.lint_sql("   ") == "SQL 为空"


def test_dataset_dirty_layer_design() -> None:
    """5 道脏题覆盖 ticket 要求的陷阱类型且 notes 说明陷阱。"""
    dirty = [q for q in vd.load_dataset()["questions"] if q["type"] == "dirty"]
    assert len(dirty) == 5
    needles = ("字段", "客单价", "品类", "锚", "无解")
    for q, needle in zip(dirty, needles, strict=True):
        assert needle in q["notes"], f"{q['id']} notes 未覆盖 {needle}"


# ---------- integration：连库复核（validate_dataset.py --check 即自测） ----------

_ENV = dotenv_values(Path(__file__).resolve().parents[1] / ".env")


@pytest.mark.integration
def test_dataset_check_against_db() -> None:
    dsn = _ENV.get("PG_DSN_RO")
    if not dsn:
        pytest.skip(".env 缺少 PG_DSN_RO")
    try:
        psycopg.connect(dsn, connect_timeout=3).close()
    except psycopg.Error:
        pytest.skip("本机 PG 不可连")
    assert vd.main(["--check"]) == 0
