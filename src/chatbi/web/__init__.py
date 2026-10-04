"""chatbi.web：Web 壳（T8 / SPEC §6.4）——FastAPI app + 原生 JS 单页（见 app.py / static/）。

启动：``python -m chatbi.web``（默认 127.0.0.1:8000）或
``uvicorn chatbi.web.app:app --host 127.0.0.1 --port 8000``。

注意：本包 ``__init__`` 刻意不重导出 ``app``——否则 FastAPI 实例会遮蔽同名模块
``chatbi.web.app``，monkeypatch / import 解析都会踩坑；请从 ``chatbi.web.app`` 取。
"""
