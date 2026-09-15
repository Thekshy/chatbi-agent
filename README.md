# chatbi-agent

垂类电商（美妆 / 服饰）ChatBI 智能体：自然语言提问 → LangGraph Plan-Execute 编排 →
schema 检索 → text2sql → 只读查询 → 下钻 / 指标计算，输出带数据证据的结构化分析结论。

规格见 [SPEC.md](SPEC.md)，任务拆解见 [TICKETS.md](TICKETS.md)。当前进度：**T1 项目骨架与 PG 环境**。

## 技术栈

Python 3.12 + uv · PostgreSQL 17（本机 Homebrew）· LangGraph · 智谱 GLM（`glm-5.3`，OpenAI 兼容）
· FastAPI + 原生 JS 单页（v1 Web 壳）

## 目录结构

```
├── SPEC.md / TICKETS.md    # 规格与任务（唯一对齐依据）
├── pyproject.toml          # uv 管理，运行 + 开发依赖
├── .env.example            # 环境变量模板（复制为 .env）
├── src/chatbi/             # 核心库（后续 ticket 逐步填充）
├── data/                   # mock 生成器（T2）
├── eval/                   # 50 题评测集与 harness（T9/T10）
├── runs/                   # trace 落盘（gitignore）
└── tests/                  # pytest
```

## 环境准备（一次性）

```bash
# 1. 依赖
uv sync

# 2. PostgreSQL（Homebrew，已装 17.11 的前提下）
brew services start postgresql@17

# 3. 建库与双账号（超级用户执行；密码自行生成替换）
psql -h 127.0.0.1 -d postgres \
  -c "CREATE ROLE chatbi_app LOGIN PASSWORD '<app_password>';" \
  -c "CREATE ROLE chatbi_ro LOGIN PASSWORD '<ro_password>';" \
  -c "CREATE DATABASE chatbi OWNER chatbi_app;" \
  -c "REVOKE ALL ON DATABASE chatbi FROM PUBLIC;" \
  -c "GRANT CONNECT ON DATABASE chatbi TO chatbi_app, chatbi_ro;" \
  -c "REVOKE CREATE ON SCHEMA public FROM PUBLIC;" \
  -c "GRANT USAGE, CREATE ON SCHEMA public TO chatbi_app;" \
  -c "GRANT USAGE ON SCHEMA public TO chatbi_ro;" \
  -c "ALTER DEFAULT PRIVILEGES FOR ROLE chatbi_app IN SCHEMA public GRANT SELECT ON TABLES TO chatbi_ro;"

# 4. 配置 .env（ZHIPU_API_KEY 来自智谱开放平台）
cp .env.example .env && $EDITOR .env
```

账号约定（SPEC §6.3 / §7）：`chatbi_app` 读写、仅 mock 生成器持有；`chatbi_ro` 只读、
运行时（工具层 run_query）唯一连接方式，public schema 无 CREATE 权限。

## 测试

```bash
uv run pytest        # 冒烟：.env 全键可读、chatbi_ro 连库+DDL 被拒、chatbi_app 可读写
uv run ruff check .  # lint
```

## 评测

50 题离线评测（分层：单指标 15 / 同环比 10 / 下钻归因 12 / TopN 8 / 纠错脏题 5）。
harness 与实测结果待 T10 / T11 落地后回填本节。
