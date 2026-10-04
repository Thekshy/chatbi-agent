# SPEC.md · 垂类电商数据分析 Agent（ChatBI）v1

- 版本：v0.1（草案，待决策者确认）
- 日期：2026-09-14
- 状态：**闸门 2 —— 等待 SPEC 确认**（确认前不拆 ticket、不写业务代码）
- 定位：本文自包含、无歧义，是后续每个开发窗口开工前重新对齐的唯一依据；争议以本文 + 拍板记录为裁判。

---

## 1. 项目目标

面向美妆 / 服饰垂类的自建 mock 电商库，做一个真实可跑的 ChatBI 智能体：运营、买手用自然语言提问（如「上周护肤品类 GMV 为何下滑」「618 活动带来多少复购增量」），Agent 基于 LangGraph StateGraph 做 Plan-Execute 编排，调用工具链（schema 检索 → text2sql → 校验 → 只读查询 → 下钻 / 指标计算），SQL 报错或结果异常时按 Reflexion 回灌自我修正，最终输出**带数据证据的结构化分析结论**。

一句话概括：单用户本地 **Web 端**、单轮问句、**PostgreSQL** mock 库、**智谱 GLM** 编排、50 题离线评测达标即 v1 完成。

## 2. 拍板结论汇总（Grilling 12 问，唯一依据）

| # | 议题 | 拍板结论 |
|---|---|---|
| Q1 | mock 库 | **PostgreSQL**（本机 Homebrew 实例已就绪，17.11；建库 / 账号 / 起服务属环境 ticket 任务） |
| Q2 | 表与行数 | 5 张表：orders / order_items / products / traffic_daily / promotions；**流量域进 v1**；mock 数据人工植入故事 |
| Q3 | 口径 | GMV = 明细求和、已支付口径；复购 = 首单后 30 天内第二单；活动复购增量 = 两期新客复购率之差（详见 §5.2 指标字典） |
| Q4 | LLM | **智谱开放平台 GLM 系列**（OpenAI 兼容接口）；Planner / Executor / Reflexion 同一模型，不做路由 |
| Q5 | 预算限流 | maxSteps=6、LLM 调用 ≤8、run_query ≤5；超限降级输出部分结论；429 指数退避重试 2 次 |
| Q6 | 问法范围 | 4+1 类：单指标取数 / 同环比 / 下钻归因 / TopN / 活动复购增量；下钻归因只做贡献度分解，不做因果推断 |
| Q7 | 交互形态 | **Web 端**（核心 agent 必须是可编程调用的 Python 库，评测批量复用；Web 壳 = FastAPI + 原生 JS 单页） |
| Q8 | 只读边界 | 只读账号连接 + sqlglot AST 校验（单条 SELECT / 表白名单 / 拒 DDL DML）+ 自动 LIMIT 与语句超时；熔断只做调用上限 + 扫描行数日志，不做硬熔断 |
| Q9 | 纠错重试 | 每条 SQL ≤2 次 Reflexion 自修正 → 失败触发一次 Replan → 再失败输出结构化失败报告；SQL 指纹去重拦截重复执行 |
| Q10 | 日志 trace | 每次分析一个 JSONL trace（步骤 / 工具调用 / SQL / 行数 / 耗时 / token / 费用 / 结论证据），存 `runs/<timestamp>/`；LangGraph 用 SqliteSaver checkpointer |
| Q11 | 评测 | 50 题分层（15/10/12/8/5），目标：执行正确率 ≥80%、结论正确率 ≥70%、纠错成功率 ≥50%、一致性 ≥80%；实测如实写 README |
| Q12 | 工程栈 | 目录 `/Users/mrpwn/Project/chatbi-agent`；Python 3.12 + uv；最小依赖；git 单仓库 |

## 3. 范围（v1 支持）

问法 4+1 类，每类示例如下（评测集据此出题）：

1. **单指标取数**：带时间 / 品类 / 渠道 / 品牌 / 活动过滤的指标查询。例：「上个月护肤品类的 GMV 是多少」「直播间渠道最近 30 天的支付订单数」。
2. **同环比**：指标对上月 / 去年同期 / 上一周的对比与变化率。例：「6 月 GMV 环比 5 月变化多少」。
3. **下钻归因**：「为何下滑 / 增长」类问题，做**维度贡献度分解**（哪个二级类目 / 哪些天 / 流量还是转化贡献了变化），不做因果推断。例：「面部精华 6 月 GMV 为何环比下滑」。
4. **TopN 排名**：按指标排序取前 N 的品类 / 品牌 / 商品。例：「618 期间 GMV Top5 品牌及其占比」。
5. **活动复购增量**（简化归因）：按 §5.2 口径对比活动期与基线期新客复购率。例：「618 活动带来了多少复购增量」。

## 4. 非目标（v1 明确不做）

- 多轮对话改写（每次提问独立，不携带上文）
- 图表生成（结论以表格 + 文本呈现）
- 多用户 / 登录 / 权限体系（单用户本地使用，服务绑定 127.0.0.1）
- 实时数据、增量更新（mock 库一次性生成，静态）
- 澄清对话轮（问题信息不足时直接按可答部分回答并在结论中说明局限，不反问）
- SSE / WebSocket 流式输出（同步请求-响应，前端 loading 呈现）
- 模型路由 / 多模型混用、因果推断、生产级熔断

## 5. 数据规格（mock 库契约）

### 5.1 表结构（PostgreSQL 17.11 · 本机 Homebrew 实例，库 `chatbi`）

| 表 | 关键字段 | 规模 | 说明 |
|---|---|---|---|
| `orders` | order_id PK, user_id, status ∈ {pending_payment, paid, shipped, completed, cancelled, refunded}, channel ∈ {app, web, mini, live}, promo_id FK NULL, created_at, paid_at | ≈10 万行，覆盖 2025-09-15 ~ 2026-09-13 | 订单头；paid_at 为指标时间锚点 |
| `order_items` | item_id PK, order_id FK, product_id FK, quantity, unit_price NUMERIC(10,2) | ≈25 万行 | 明细；GMV 由本表求和 |
| `products` | product_id PK, product_name, category_l1 ∈ {护肤, 彩妆, 香水, 女装, 男装, 配饰}, category_l2, brand, list_price | ≈2000 SKU | 两级类目树；l2 约 30 个（如 护肤→面部精华/面霜/面膜） |
| `traffic_daily` | date, category_l1, channel, pv, uv | ≈8,760 行（365 天 × 6 类目 × 4 渠道） | 日粒度流量；支撑「流量 vs 转化」归因 |
| `promotions` | promo_id PK, promo_name, category_l1 NULL(全场), start_date, end_date, promo_type ∈ {full_reduction, discount, gift, flash} | ≈30 场 | 12 个月内的活动（含 618、双 11） |

独立用户 ≈3 万（无独立 users 表，复购口径由 orders 自连接计算）。

**生成方式的自洽约束**：mock 生成器以「日 × 类目 × 渠道」为控制单元，先采 UV，再按转化率 cvr 生成 paid 订单量，最后展开明细与金额。这样「流量跌」「转化跌」两个故事在数值上天然自洽。

### 5.2 指标字典（结论正确率的裁判口径）

| 指标 | 定义 |
|---|---|
| GMV | `SUM(order_items.unit_price × quantity)`，关联订单 status ∈ {paid, shipped, completed}，时间按 paid_at，不含运费，cancelled / refunded 剔除 |
| 支付订单数 | 上述口径的去重 order_id 数 |
| 客单价 | GMV ÷ 支付订单数 |
| 复购（30 日） | 用户首单（paid 口径）后 30 天内产生第二单即计复购；复购率 = 窗口内复购用户 ÷ 窗口内首单新客数 |
| 活动复购增量 | 活动期（paid_at ∈ 活动区间）新客的 30 日复购率 − 基线期（活动前 30 天）新客的 30 日复购率 |
| 同比 / 环比 | 月对月（环比）、对上年同月（同比）、周对周；变化率 = (本期−上期)÷上期 |
| 转化率 | 日粒度：当日 paid 订单数 ÷ 当日 UV（品类 / 渠道维度同理） |

### 5.3 植入故事（数值已定稿：2026-09-15 由 mock 生成器实测定稿，种子 20260915）

> 实测口径与 §5.2 一致，裁判 SQL 见 `data/mock_gen.py::self_check`；`uv run python -m data.mock_gen` 可复现同样数值。
> 分解口径：GMV 环比 = (1+UV 环比) × (1+单位 UV 产出环比) − 1；UV 取护肤 l1 流量（traffic_daily 无 l2 粒度），单位 UV 产出 = 面部精华 GMV ÷ 护肤 UV。

1. **面部精华下滑**：l2=面部精华（属护肤）2026-05-21 起渐弱、6 月全效、7 月上旬回升。6 月 GMV 782,690 元，环比 5 月（1,049,581 元）**下滑 25.4%**；其中护肤 UV 环比 **-15.5%**（流量贡献），单位 UV 产出环比 **-11.7%**（转化贡献其余）。
2. **618 复购拉升**：2026-05-31~06-18「618大促」（全场 discount）：活动期新客（n=1,454）30 日复购率 **17.95%** vs 基线期 2026-05-01~05-30 新客（n=1,321）**10.98%**，差 **+6.97 个百分点**。
3. **品牌 TopN 梯度**：全场 GMV Top1 品牌「雪滢」（护肤）占比 **12.35%**，为 Top2「绯迦」（彩妆，7.35%）的 **1.68 倍**；13 个自然月（2025-09~2026-09，首尾半月）月度 Top1 恒为雪滢。
4. 背景：全年共 **30 场**活动（含双 11 全场大促 2025-11-01~11-11、国庆、88 节等），日历与流量/转化乘子见 `data/stories.py::PROMOTIONS`。

## 6. 架构与技术方案

### 6.1 分层

```
chatbi-agent/
├── SPEC.md
├── pyproject.toml            # uv 管理
├── .env.example              # PG_DSN / PG_DSN_RO / ZHIPU_API_KEY / CHATBI_MODEL
├── data/mock_gen.py          # mock 生成器（§5 契约 + 植入故事）
├── src/chatbi/
│   ├── state.py              # GraphState：plan / steps / results / 预算计数 / SQL 指纹集
│   ├── graph.py              # StateGraph 装配（下）
│   ├── llm.py                # 智谱 OpenAI 兼容客户端封装
│   ├── metrics.py            # 指标字典的 SQL 模式与下钻模板
│   ├── tools/                # get_schema / get_data_profile / validate_sql / run_query
│   └── web/                  # FastAPI app + static/（原生 JS 单页）
├── eval/
│   ├── dataset.yaml          # 50 题标注集
│   ├── runner.py             # 批量执行 + 分层指标计算 + 一致性采样
│   └── report.py             # 汇总报告（喂 README 评测表）
├── runs/                     # trace 落盘（gitignore）
└── tests/                    # pytest
```

### 6.2 LangGraph 图设计

- 节点：`planner`（产出步骤计划）→ `executor`（逐步：schema 检索 + LLM 生成 SQL → validate_sql → run_query；含下钻 / 指标模板注入）→ `reflect`（SQL 报错回灌 error+schema；空集 / 口径不符回灌 schema+数据分布提示）→ `replan`（换路径）→ `reporter`（汇总证据生成结构化结论）。
- 条件边：executor 后按「预算是否耗尽 / 步骤是否完成 / 是否需 Replan」分流到 END / replan / reporter；预算耗尽走 reporter 输出 partial。
- 持久化：SqliteSaver，每次分析一个 thread_id，支持中断恢复。
- 防失控三件套：maxSteps=6 + LLM 调用 ≤8 + run_query ≤5（§Q5）；SQL 规范化（sqlglot 解析后 canonical 输出）取 SHA256 入指纹集，重复即拦截；自动包 LIMIT(1000) 与语句超时 5s。

### 6.3 安全与只读边界（Q8 四件套）

1. run_query 一律走**只读账号** `chatbi_ro`（库 `chatbi`；mock 生成用读写账号 `chatbi_app`，仅生成器持有）。
2. `validate_sql`：sqlglot 解析 AST——仅单条 SELECT、语句级无 DDL/DML/多语句、引用表 ⊆ 5 表白名单、强制 LIMIT。
3. 超时：PostgreSQL `statement_timeout = 5s`（连接级设置）。
4. 扫描行数超过阈值（如 50 万）记 WARN 日志，不做硬熔断。

### 6.4 Web 契约（Q7）

- `POST /api/analyze` `{question}` → 同步返回 `{status: answered|partial|failed, conclusion, evidence[], steps[], usage}`；分析耗时 30~60s，前端 loading 呈现。
- `GET /api/traces`、`GET /api/traces/{id}`：历史 trace 列表与详情（证据留存）。
- `GET /api/schema`：库结构速览（辅助用户提问）。
- 前端：FastAPI 托管的单页（原生 JS + fetch），呈现结论、证据表格、步骤时间线、token/费用。绑定 127.0.0.1。
- 开发调试入口 `python -m chatbi.ask "问题"`（薄壳，非验收范围）。

### 6.5 LLM 接入

智谱开放平台 OpenAI 兼容端点；统一封装在 `llm.py`，温度等参数集中管理；429 指数退避重试 2 次；每次调用 token 数回写 state 供 trace 与费用统计。

### 6.6 备选取舍（已选 vs 备选）

| 决策点 | 选定 | 备选 | 理由 |
|---|---|---|---|
| 数据库 | PostgreSQL | DuckDB | 决策者本地规划有 PG；权限 / statement_timeout 语义贴近生产，评测「只读边界」更真实。代价：需常驻服务，环境准备成本高一点 |
| 前端 | FastAPI + 原生 JS 单页 | React + Vite | v1 只有一个问答页，原生最小成本；组件化留 v2 |
| 接口形态 | 同步 HTTP | SSE 流式 | 单用户本地场景 30~60s 可接受；流式列为非目标 |
| 编排 | LangGraph StateGraph | 手写循环 | 条件边 / checkpointer / 状态持久化开箱即用，且是本项目练习目标本身 |

## 7. 输入输出契约

- **core 库**：`analyze(question: str, *, config: RunConfig) -> AnalysisResult`
  - `AnalysisResult = {status: answered|partial|failed, conclusion: str, evidence: [{sql, row_count, rows_preview, ms}], steps: [...], usage: {llm_calls, tokens_in/out, cost_est}, trace_dir: Path}`
- **数据库**：PG 16+，库名 `chatbi`，账号 `chatbi_app`（读写，仅生成器）/ `chatbi_ro`（只读，运行时唯一）；连接串走 `.env`（`PG_DSN` / `PG_DSN_RO`）。
- **LLM**：`ZHIPU_API_KEY`、`CHATBI_MODEL=glm-5.3`（已锁定；key 复用 `mine/gewu/.env` 中 `LLM_API_KEY`，端点 `https://open.bigmodel.cn/api/coding/paas/v4/`，OpenAI 兼容。订阅制无按 token 单价，`cost_est` 以 token 用量为主、费用估算可选）。

## 8. 验收标准（EARS）

### 8.1 功能验收

- When 用户在 Web 提交 §3 范围内的问题，the system shall 返回结构化结论，且每条结论附带 ≥1 条证据 SQL 及其行数与耗时。
- When 生成的 SQL 语法报错或返回空集 / 口径不符，the system shall 对该 SQL 至多自修正 2 次；仍失败时 shall 触发至多一次 Replan；再失败 shall 输出 `failed` 的结构化报告（含已尝试路径与失败原因）。
- When 单次分析 LLM 调用达 8 次或 run_query 达 5 次或步数达 6，the system shall 停止扩展并输出 `partial` 结论（含已完成部分与未完成标记）。
- When 生成的 SQL 含非 SELECT 语句、白名单之外的表、多条语句或缺 LIMIT，the validate_sql shall 拒绝执行并回灌修正，且不触库。
- When 同一次分析中出现重复 SQL 指纹，the system shall 拦截执行并引导换路径。
- When 一次分析结束（无论成败），the system shall 落盘完整 JSONL trace（消息、工具调用、SQL、行数、耗时、token、结论与证据）。
- When 用户访问 Web 界面，the system shall 提供提问框、结论区、证据表格、步骤时间线、用量显示与历史 trace 查看。

### 8.2 评测验收（离线，50 题）

| 指标 | 定义 | 目标 |
|---|---|---|
| 执行正确率 | SQL 可执行且结果与标准 SQL 一致（数值相对误差 ≤2%，日期边界在标注中显式约定）的题数 ÷ 50 | ≥ 80% |
| 结论正确率 | 结论关键数值与期望一致（±2%）且方向正确（按 §5.2 指标字典裁判）的题数 ÷ 50 | ≥ 70% |
| 纠错成功率 | 5 道脏题中「首轮 SQL 失败但最终 answered 且结果正确」的题数 ÷ 5 | ≥ 50%（即 ≥3 题） |
| 采样一致性 | 同题独立跑 3 次、≥2 次结论一致的题数 ÷ 50 | ≥ 80% |

评测集分层：单指标 15 / 同环比 10 / 下钻归因 12 / TopN 8 / 纠错脏题 5。每题标注：问题、类型、标准 SQL、期望结论（数值 + 容差 + 方向）、期望步骤类型。**实测数字如实写入 README 评测表，不预设、不凑数。**

## 9. 工程与纪律

1. 闸门制：本 SPEC 确认后才拆 ticket；ticket 依赖图确认后才开工。
2. 开发期一个 ticket 一个新窗口：先贴 SPEC + 当前 ticket 对齐，只动修改范围内代码，先测后写。
3. 每窗口完成即汇报：改动文件、验证方式、有无偏离。
4. 依赖最小集：`langgraph / openai / psycopg / sqlglot / fastapi / uvicorn / pyyaml / python-dotenv`（运行）+ `pytest / rich / ruff`（开发）；Python 3.12，uv 管理。
5. git 单仓库，`runs/`、`.env` 进 gitignore。

## 10. 开放项（不阻塞本 SPEC，按指定 ticket 收口）

| 项 | 说明 | 收口位置 |
|---|---|---|
| （无） | | |

已收口：
- PG 环境（2026-09-14）：本机 Homebrew PostgreSQL 17.11 已安装；服务启动、建库 `chatbi`、建账号 `chatbi_app`/`chatbi_ro` 归环境 ticket（T1）。
- 智谱模型（2026-09-14）：`glm-5.3`，key 与端点复用 `mine/gewu/.env`（T1 建 `.env` 时拷贝，不入库）。
- PG 部署形态（2026-09-15）：确认继续用 brew 单实例、**按库隔离**（每项目独立 database + 独立角色，不切 Docker——内网 DNS 封锁全部容器镜像源，离线导镜像维护成本高）；`brew pin postgresql@17` 锁版本防 brew 自动升级动数据目录。
- 植入故事数值（2026-09-15，T2）：§5.3 三个故事实测定稿——面部精华 6 月 GMV 环比 -25.4%（护肤 UV -15.5% / 单位 UV 产出 -11.7%）；618 新客 30 日复购差 +6.97pp（17.95% vs 10.98%）；全场 Top1 雪滢占 12.35%（1.68× 于 Top2），逐月恒定。种子 20260915，`uv run python -m data.mock_gen` 一键复现。
