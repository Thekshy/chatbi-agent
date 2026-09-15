"""T1 冒烟测试：.env 可读、PG 双账号就绪、只读边界生效。

依赖本机环境：brew services 管理的 PostgreSQL（chatbi 库 + chatbi_app/chatbi_ro 角色）。
"""

from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
REQUIRED_KEYS = ("PG_DSN", "PG_DSN_RO", "ZHIPU_API_KEY", "CHATBI_MODEL")


def _load_env() -> dict[str, str]:
    values = dotenv_values(ENV_PATH)
    missing = [k for k in REQUIRED_KEYS if not values.get(k)]
    assert not missing, f".env 缺少键或为空: {missing}（先 cp .env.example .env 并填值）"
    return values  # type: ignore[return-value]


def test_dotenv_loads_all_keys() -> None:
    env = _load_env()
    assert env["CHATBI_MODEL"] == "glm-5.3"
    assert env["PG_DSN"].startswith("postgresql://chatbi_app:")
    assert env["PG_DSN_RO"].startswith("postgresql://chatbi_ro:")


def test_ro_connect_and_query_version() -> None:
    with psycopg.connect(_load_env()["PG_DSN_RO"]) as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
    assert "PostgreSQL" in version


def test_ro_ddl_denied() -> None:
    with psycopg.connect(_load_env()["PG_DSN_RO"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
            conn.execute("CREATE TABLE t_ro_probe (id int)")


def test_app_can_ddl() -> None:
    with psycopg.connect(_load_env()["PG_DSN"]) as conn:
        conn.execute("CREATE TABLE t_app_probe (id int)")
        conn.execute("DROP TABLE t_app_probe")
