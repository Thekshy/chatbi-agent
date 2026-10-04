# TICKETS.md · ChatBI Agent v1

- 版本：v0.1（待决策者过依赖图）
- 日期：2026-09-14
- 状态：**闸门 3 —— 等待依赖图确认**（确认前不动工）
- 用法：每个 ticket 开一个新窗口，贴 SPEC.md + 本 ticket（playbook `03-ticket.md` 模板 B）；只动修改范围内代码，先测后写，完成即汇报（改动文件 / 验证方式 / 有无偏离）。

## 依赖图

```
T1 骨架+环境 ──┬──> T2 mock 数据 ──┬──> T4 工具层 ──┐
              │                    └──> T9 评测集标注 ─┼──> T10 评测 harness ──┐
              └──> T3 LLM 接入 ──────> T5 图编排 <────┘                      ├──> T11 验收+README
                                      T5 ──> T6 trace ──> T7 核心API ──> T8 Web ┘
```

- 关键路径：T1 → T2 → T4 → T5 → T6 → T7 → T10 → T11（8 个窗口）
- 并行车道：T3 在 T5 前任意时间做；T9 在 T10 前任意时间做（与 T3-T8 全程并行，出题口径依赖 T2 定稿的故事数值）
- 建议顺序：T1 → T2 → T3 → T4 → T9 → T5 → T6 → T7 → T10 → T8 → T11

---

## T1 · 项目骨架与 PG 环境

- **① 目标**：空项目可跑通工具链，PG 库与双账号就绪。
- **② 背景**：SPEC §7 DB 契约、§9 工程栈。本机 Homebrew PG 17.11 已装、服务未常驻；智谱 key 复用本机既有配置（`LLM_API_KEY` / `LLM_BASE_URL`）。
- **③ 修改范围**：仓库根：`pyproject.toml`、`.gitignore`、`.env.example`、`.env`（本地，不入库）、`README.md` 骨架、`src/chatbi/__init__.py`、`tests/`。不写任何业务模块。
- **④ 任务**：
  - [ ] `git init` + uv 初始化（Python 3.12）
  - [ ] 装依赖：运行 `langgraph / openai / psycopg / sqlglot / fastapi / uvicorn / pyyaml / python-dotenv`，开发 `pytest / rich / ruff`
  - [ ] `.env`：`PG_DSN`（chatbi_app）、`PG_DSN_RO`（chatbi_ro）、`ZHIPU_API_KEY`、`CHATBI_MODEL=glm-5.3`（key 从本机既有配置拷贝）
  - [ ] `brew services start` 起 PG；建库 `chatbi`；建角色 `chatbi_app`（读写）、`chatbi_ro`（只读，撤销 public 的 create）
  - [ ] pytest + ruff 配置；冒烟测试一条（chatbi_ro 连库查版本）
- **⑤ 验收**：`uv run pytest` 全绿；`chatbi_ro` 能连 `chatbi` 但建表被拒；`.env` 能被 dotenv 读出全部键。
- **⑥ 测试**：`tests/test_env.py`：只读账号连接成功、尝试 DDL 失败（权限错误）。
- **依赖**：无。

## T2 · mock 数据生成器

- **① 目标**：`data/mock_gen.py` 一键生成符合 SPEC §5 的 5 表数据，植入 3 个故事并定稿数值。
- **② 背景**：§5.1 表结构、§5.2 口径、§5.3 故事。生成必须自洽：按「日×品类×渠道」先采 UV，再乘转化率得 paid 订单量，再展开明细金额。
- **③ 修改范围**：`data/mock_gen.py`、`data/stories.py`（故事参数）、`tests/test_mockgen.py`、SPEC §5.3（数值回填定稿）。不动 `src/`。
- **④ 任务**：
  - [ ] 5 表 DDL（含约束与索引：status/channel/枚举 check、paid_at、类目外键到字典表或 check 约束）
  - [ ] 基础生成：季节性 + 周末效应 + 品类/渠道权重，12 个月 ≈10 万订单 / ≈25 万明细 / ≈2000 SKU / ≈8760 流量行 / ≈30 活动
  - [ ] 植入故事：面部精华 5 月下旬-6 月下滑（流量与转化两个旋钮）、618 复购拉升、品牌 TopN 头部梯度
  - [ ] 幂等：drop + recreate 全量重建
  - [ ] 生成后自检报告：各表行数、故事指标实际达成值（打印并与目标区间比对）
- **⑤ 验收**：重建后行数落在 §5.1 量级 ±10%；面部精华 6 月 GMV 环比降幅 20%-30% 且流量贡献 12%-18%；618 复购率差落在 6-10 个百分点；故事数值回填 SPEC §5.3。
- **⑥ 测试**：单元：生成函数分布性质（种子固定可复现）；集成：连库断言故事指标区间。
- **依赖**：T1。

## T3 · LLM 接入封装

- **① 目标**：`src/chatbi/llm.py` 封装智谱 OpenAI 兼容调用，全项目唯一 LLM 出口。
- **② 背景**：SPEC §6.5、§7。端点 `https://open.bigmodel.cn/api/coding/paas/v4/`，模型 `glm-5.3`，订阅制无按 token 单价。
- **③ 修改范围**：`src/chatbi/llm.py`、`tests/test_llm.py`。不改其他模块。
- **④ 任务**：
  - [ ] 客户端：读 `.env`（key/model/base_url），温度等采样参数集中为配置常量
  - [ ] 429 / 5xx 指数退避重试 2 次（上限后抛特定异常）
  - [ ] 每次调用返回 `(content, usage{tokens_in, tokens_out})`
  - [ ] 真实调用冒烟 1 条（integration 标记，无 key 时 skip）
- **⑤ 验收**：冒烟调用返回非空内容且 token 计数 > 0；退避行为符合预期。
- **⑥ 测试**：单元：mock 客户端验证退避次数与异常；integration：真实调用一条。
- **依赖**：T1（可与 T2 并行）。

## T4 · 工具层（schema 检索 / 校验 / 只读查询）

- **① 目标**：`src/chatbi/tools/` 四件工具，只读边界四件套全部落地。
- **② 背景**：SPEC §6.3。sqlglot AST 校验是「执行正确率」评测的一部分，必须真做。
- **③ 修改范围**：`src/chatbi/tools/get_schema.py`、`get_data_profile.py`、`validate_sql.py`、`run_query.py`、`tests/test_tools.py`。
- **④ 任务**：
  - [ ] **先写用例集**（≥12 条）：UPDATE/DROP/多语句/白名单外表/无 LIMIT 自动补/已有小 LIMIT 保留/CTE 合法/子查询合法/注释绕过尝试…
  - [ ] `get_schema(table?)`：表/字段/类型/枚举值/行数/3 行示例
  - [ ] `get_data_profile(table, column)`：distinct top-k、min/max、null 率（空集纠错的回灌素材）
  - [ ] `validate_sql`：sqlglot 解析——单条 SELECT、表白名单、拒 DDL/DML/多语句、规范 canonical 形态、自动 LIMIT(1000)（取 min）
  - [ ] `run_query`：`PG_DSN_RO` 连接、`statement_timeout=5s`、扫描行数 > 50 万记 WARN
- **⑤ 验收**：用例集 12+ 条全过；超时 SQL（`pg_sleep`）被 5s 截断；只读连接上任何写操作直接失败。
- **⑥ 测试**：TDD——先测后写；用例集即验收清单。
- **依赖**：T2（需要真实表结构与数据）。

## T5 · StateGraph 编排核心

- **① 目标**：`graph.py` 装配完整 Plan-Execute 图，五节点 + 条件边 + 防失控三件套。
- **② 背景**：SPEC §6.2。这是项目核心票，最大的一个窗口。
- **③ 修改范围**：`src/chatbi/state.py`、`graph.py`、`src/chatbi/metrics.py`（指标字典的 SQL 模式与下钻模板）、`tests/test_graph.py`、提示词文本（planner/executor/reflect/reporter，独立文件存放）。
- **④ 任务**：
  - [ ] `state.py`：plan/steps/results、预算计数（llm_calls/query_calls/steps）、SQL 指纹集合、错误栈
  - [ ] `metrics.py`：GMV/订单数/客单价/复购/同环比/TopN 的 SQL 模式与维度下钻模板（注入 executor 上下文）
  - [ ] `planner`：问题 → 步骤计划（含问法类型判别）
  - [ ] `executor`：逐步执行——schema 检索 → LLM 生成 SQL（附指标模板）→ validate → run；记录证据
  - [ ] `reflect`：SQL 报错回灌 error+schema；空集/口径不符回灌 schema+数据分布提示（用 `get_data_profile`）
  - [ ] `replan`：条件边触发，换路径至多一次
  - [ ] `reporter`：汇总证据生成结构化结论（数值+方向+证据引用+局限说明）
  - [ ] 预算守卫：maxSteps=6 / LLM ≤8 / query ≤5，超限走 reporter 输出 partial
  - [ ] SQL 指纹去重：canonical 形态 SHA256，重复拦截
  - [ ] SqliteSaver checkpointer，每次分析独立 thread_id
- **⑤ 验收**：三个冒烟问题端到端 answered（单指标/同环比/TopN，含证据）；FakeLLM 注入下：预算耗尽 → partial、重复 SQL → 拦截并换路径、报错 → ≤2 次自修正后 Replan。
- **⑥ 测试**：FakeLLM 单元测试覆盖条件边与预算；integration 跑 3 个真实问题。
- **依赖**：T3、T4。

## T6 · trace 落盘

- **① 目标**：每次分析产出 `runs/<timestamp>/trace.jsonl`，全量可回放。
- **② 背景**：SPEC §Q10、§8.1。评测「纠错成功率」依赖 trace 判定首轮失败，事件必须可判读。
- **③ 修改范围**：`src/chatbi/tracing.py`、`graph.py`（最小侵入挂回调）、`tests/test_tracing.py`。
- **④ 任务**：
  - [ ] JSONL 事件 schema：type（message/tool_call/sql_result/step/conclusion）、耗时、行数、token、错误
  - [ ] 从图执行回调 + state 采集，写入 `runs/<ts>/trace.jsonl`
  - [ ] usage 汇总（tokens 为主，费用估算开关默认关）
  - [ ] failed/partial 分析同样完整落盘
- **⑤ 验收**：冒烟分析后 trace 覆盖 planner→executor→工具→reporter 全链路；usage 与 LLM 返回一致；失败场景也有 trace。
- **⑥ 测试**：FakeLLM 端到端断言事件序列与字段完整。
- **依赖**：T5。

## T7 · 核心库 API + CLI 薄壳

- **① 目标**：`analyze()` 契约落地，评测与 Web 共用的唯一入口。
- **② 背景**：SPEC §7 输入输出契约。
- **③ 修改范围**：`src/chatbi/api.py`、`src/chatbi/ask.py`、`tests/test_api.py`。
- **④ 任务**：
  - [ ] `AnalysisResult` dataclass：status/conclusion/evidence/steps/usage/trace_dir
  - [ ] `RunConfig`（预算覆写、模型覆写、trace 开关）
  - [ ] `analyze(question, *, config)`：任何内部异常捕获转为 `failed` 结果，不向上抛
  - [ ] `python -m chatbi.ask "问题"`：rich 表格呈现结论与证据（开发调试用，非验收范围）
- **⑤ 验收**：三态结果可分别构造触发；CLI 冒烟跑通一问。
- **⑥ 测试**：状态转换单测；integration 一问。
- **依赖**：T5、T6。

## T8 · Web 壳（FastAPI + 原生 JS 单页）

- **① 目标**：单用户本地 Web 界面，覆盖 §6.4 全部契约。
- **② 背景**：Q7 拍板 Web 端；同步接口 30-60s，前端体验靠 loading 与步骤时间线补。
- **③ 修改范围**：`src/chatbi/web/app.py`、`src/chatbi/web/static/`、`tests/test_web.py`。
- **④ 任务**：
  - [ ] `POST /api/analyze`：调 `analyze()`，返回 §7 JSON 结构；请求体校验与错误处理
  - [ ] `GET /api/traces`、`GET /api/traces/{id}`：历史列表与详情
  - [ ] `GET /api/schema`：库结构速览（复用 get_schema）
  - [ ] `GET /api/health`
  - [ ] 单页：提问框、loading 态、结论区、证据表格（SQL + 结果预览）、步骤时间线、用量显示、历史 trace 查看
  - [ ] 服务绑定 127.0.0.1；无鉴权
- **⑤ 验收**：浏览器手动冒烟全流程；httpx 测试四路由契约；analyze 响应字段与 §7 一致。
- **⑥ 测试**：ASGI 测试（mock analyze 返回三态各一）。
- **依赖**：T7。

## T9 · 评测集标注（独立轨道，可与 T3-T8 并行）

- **① 目标**：`eval/dataset.yaml` 50 题标注完成，标准 SQL 全部可执行、期望数值从库实测定稿。
- **② 背景**：SPEC §8.2。期望结论必须由标准 SQL 对真实 mock 库执行得出，不手拍数值。
- **③ 修改范围**：`eval/dataset.yaml`、`eval/validate_dataset.py`。不动 `src/`。
- **④ 任务**：
  - [ ] 分层出题：单指标 15 / 同环比 10 / 下钻归因 12 / TopN 8 / 纠错脏题 5
  - [ ] 每题字段：id / question / type / standard_sql / expected（数值+容差+方向）/ expected_path / notes（日期边界约定）
  - [ ] 脏题设计：不存在的字段、口径陷阱（客单价分母）、超范围品类、时间边界歧义、无解问法
  - [ ] `validate_dataset.py`：全部 standard_sql 连库执行、结果写回 expected、schema 校验、分层计数校验
- **⑤ 验收**：validate 通过（50/50 可执行、分层计数正确）；人工抽检 10 题口径符合 §5.2 指标字典。
- **⑥ 测试**：`validate_dataset.py` 即自测脚本，纳入 pytest。
- **依赖**：T2。

## T10 · 评测 harness

- **① 目标**：批量执行 + 四指标计算 + 报告生成，README 评测表的数据来源。
- **② 背景**：SPEC §8.2；纪律 4——不预设、不凑数。
- **③ 修改范围**：`eval/runner.py`、`eval/report.py`、`tests/test_runner.py`。
- **④ 任务**：
  - [ ] 批量调 `analyze()`（支持子集运行：按 type / 按 id）
  - [ ] 执行正确率：结果 SQL 集与 standard_sql 比对（数值相对误差 ≤2%）
  - [ ] 结论正确率：结论关键数值 + 方向 vs expected
  - [ ] 纠错成功率：trace 判定「首轮 SQL 失败但最终 answered 且正确」
  - [ ] 一致性：同题 3 次独立采样，≥2 次结论一致
  - [ ] `report.py`：分层 + 总体 markdown 表、失败题明细、token 消耗汇总
- **⑤ 验收**：FakeLLM 合成数据下四指标计算正确；真实跑 5 题子集出报告。
- **⑥ 测试**：指标计算函数单测（构造已知答案）。
- **依赖**：T7、T9。

## T11 · 离线验收与 README 定稿

- **① 目标**：全量评测 50 题 × 3 采样实测，指标对目标，README 定稿。
- **② 背景**：SPEC §8.2。这是 v1 的完成判定窗口。
- **③ 修改范围**：`README.md`、`eval/reports/`（产物入库）、SPEC §5.3（如最终数值有偏差则回填）。
- **④ 任务**：
  - [ ] 全量评测（50 × 3），token 消耗与耗时记录
  - [ ] README：评测表（分层 + 总体，实测值 vs 目标）、未达标项与已知问题清单、使用说明（起服务 / 提问 / 看 trace）、架构简图
  - [ ] 与目标差距如实呈现；未达标不做针对性 hack（留给 v2 结论）
- **⑤ 验收**：README 每项指标有实测数字；差距如实；使用说明可照做跑通。
- **⑥ 测试**：无新增测试，评测产物即验收物。
- **依赖**：T8、T10。
