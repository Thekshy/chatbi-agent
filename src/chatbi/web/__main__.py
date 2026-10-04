"""``python -m chatbi.web``：启动本地 Web 服务（默认绑定 127.0.0.1:8000，单用户无鉴权）。

SPEC §6.4 / 非目标：不做多用户 / 登录 / 权限；host 默认回环、仅显式 ``--host``
才对外。分析为同步接口（30~60s），前端靠 loading 与步骤时间线补体验。
"""

from __future__ import annotations

import argparse

import uvicorn

from chatbi.web.app import app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m chatbi.web",
        description="ChatBI Web 本地服务（单用户、无鉴权；分析为同步接口，约 30~60s）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅回环）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
