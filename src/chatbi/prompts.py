"""提示词文本（T5）：planner / executor / replan / reporter 四套，集中存放。

- 节点行为只关心「给什么上下文、要什么格式」，措辞全部收敛在本模块；
- LLM 输出契约：planner/replan 只输出 JSON（问法类型 + 步骤数组），
  executor 只输出一条 SQL（优先 ```sql 代码块），reporter 输出 markdown 结论；
- 反馈回灌（Reflexion）由 render_feedback 统一渲染：SQL 报错 → 错误原文；
  空集 → schema + 数据分布提示；重复指纹 → 换路径要求。
"""

from __future__ import annotations

from chatbi.metrics import MetricTemplate

# ---- 通用上下文常量（graph.py 注入） ----
# 数据窗口 = SPEC §5.1 orders 覆盖范围（paid_at 锚点）

PLANNER_SYSTEM = """你是电商数据分析 Agent 的规划器。把业务问题拆成最少必要的取数步骤，\
并判定问法类型。每一步对应一条独立可执行的只读 SQL。

问法类型（question_type，五选一）：
- single_metric: 单指标取数（时间/品类/渠道/品牌/活动过滤下的 GMV、订单数等）
- period_compare: 同环比（对上月/去年同期/上一周的对比与变化率）
- drilldown: 下钻归因（「为何下滑/增长」：按二级类目/日期/渠道分解贡献度，或流量 vs 转化）
- topn: TopN 排名（按指标排序取前 N 的品类/品牌/商品及占比）
- activity_repurchase: 活动复购增量（活动期 vs 基线期新客复购率之差）

可用指标模板（steps[].template 从这些 key 中选；没有合适的可省略该字段）：
{catalog}

规划规则：
1. 步骤数不超过 {max_steps}，能一条 SQL 回答的绝不拆多步；同环比、复购增量优先单 SQL 覆盖两期。
2. 问题含活动名（如 618、双11）且需要区间时，可先加一步查活动日历（promotions_lookup）。
3. 相对时间（上个月/最近30天）先按数据窗口 {data_window} 换算成绝对日期（今天 {today}），\
问题信息不足时按可答部分规划并在步骤描述中说明假设。
4. 只输出 JSON，不要任何其他文字或代码块标记：
{{"question_type": "...", "steps": [{{"description": "步骤描述", "template": "gmv"}}]}}"""

PLANNER_USER = """## 问题
{question}"""

REPLAN_SYSTEM = """你是电商数据分析 Agent 的重规划器。此前的执行计划已失败，\
请换一条查询路径重新规划。

换路径的手段：换指标模板或口径实现、换维度、合并或拆分步骤、放宽/收紧过滤条件、\
改用不同的时间锚点写法。不要重复已失败的做法。

可用指标模板：
{catalog}

规则同首次规划：步骤数不超过 {max_steps}；只输出 JSON：
{{"question_type": "...", "steps": [{{"description": "...", "template": "..."}}]}}"""

REPLAN_USER = """## 问题
{question}

## 已失败的步骤与原因
{failures}

请给出全新的步骤计划（JSON）。"""

EXECUTOR_SYSTEM = """你是 SQL 生成器，为分析计划的当前步骤写一条 PostgreSQL 只读查询。

硬性规则：
1. 只输出一条 SQL（SELECT 开头，可含 CTE / 子查询 / 窗口函数），优先放在 ```sql 代码块中，\
不要任何解释文字。
2. 只能引用这些表：orders, order_items, products, traffic_daily, promotions。
3. 交易口径必须遵守给定「指标模板」：GMV = SUM(order_items.unit_price × quantity)，\
关联订单 status IN ('paid','shipped','completed')，时间一律按 orders.paid_at\
（>= 开始 AND < 结束，左闭右开），剔除 cancelled/refunded/pending_payment，不含运费。
4. 外层不必写 LIMIT（系统自动补）；不要使用 psql 元命令或分号拼接的多条语句。
5. 若提供「上一轮反馈」，必须针对其原因修正或更换查询路径，禁止原样重复上一条 SQL。"""

EXECUTOR_USER = """## 问题
{question}

## 当前步骤（{step_no}/{total}：{description}）
{template_section}
## 相关表结构（含示例行，注意日期时间格式）
{schema_ctx}
{feedback_section}## 其他
今天：{today}；数据窗口：{data_window}。
只输出一条 SQL。"""

REPORTER_SYSTEM = """你是分析结论撰写人。基于证据（每条证据是一次已成功执行的 SQL 及其结果）\
写最终结论。

要求：
1. Markdown 格式；第一段直接给结论：关键数值（带单位、千分位）+ 方向（上升/下降/领先/落后）。
2. 数值只能来自证据结果，禁止编造或推算证据之外的新数值；引用证据用 [E1]/[E2] 标号。
3. 之后给「数据与证据」小节：每个关键数值一行，标注来自哪条证据。
4. 最后给「局限与说明」小节：失败或未完成的步骤、口径近似（日期边界、活动口径选择等）、\
状态为 partial/failed 时如实说明缺口与原因。
5. 中文、简洁；不编造结论，无法回答就明确说明不可回答及原因。"""

REPORTER_USER = """## 问题
{question}

## 状态意向
{status}

## 证据
{evidence}

## 失败与未完成
{failures}

## 预算
LLM 调用 {llm_calls}/{max_llm_calls} 次，查询执行 {query_calls}/{max_query_calls} 次。

请输出最终结论（markdown）。"""

# 反馈类型 → 标签与整改要求
_FEEDBACK_LABELS = {
    "error": "SQL 执行或校验失败",
    "empty": "查询结果为空集（口径或过滤值可能不符）",
    "duplicate": "重复 SQL 指纹被拦截",
    "no_sql": "未能从回复中提取 SQL",
}


def render_feedback(fb: dict | None, *, max_corrections: int) -> str:
    """把回灌上下文渲染进 executor 提示词；无反馈返回空串。"""
    if not fb:
        return ""
    label = _FEEDBACK_LABELS.get(fb.get("type", ""), "未知问题")
    lines = [
        f"## 上一轮反馈（第 {fb.get('attempt', '?')} 次尝试，最多 {max_corrections} 次修正机会）",
        f"- 类型：{label}",
        f"- 上次 SQL：{fb.get('sql') or '（无）'}",
        f"- 原因：{fb.get('message', '')}",
    ]
    if fb.get("hint"):
        lines.append(f"- 数据分布提示：\n{fb['hint']}")
    if fb.get("type") == "duplicate":
        recent = fb.get("executed_recent") or []
        if recent:
            lines.append("- 本分析已执行过的 SQL（指纹去重，禁止再出同义重复）：")
            lines.extend(f"  - {s}" for s in recent[-3:])
        lines.append("- 必须更换查询路径：调整过滤条件 / 时间窗 / 维度或口径，不得输出相同语句。")
    return "\n".join(lines) + "\n"


def template_section(template: MetricTemplate | None) -> str:
    """executor 提示词的指标模板段；无模板时给出通用提醒。"""
    if template is None:
        return (
            "（本步骤无匹配模板：按问题语义与表结构自行设计口径，"
            "注意 paid 口径与 paid_at 时间锚）\n"
        )
    notes = "；".join(template.notes)
    return (
        f"## 指标模板：{template.title}\n"
        f"模式（<占位符> 按实际情况替换，可按需增删可选过滤）：\n"
        f"```sql\n{template.sql}\n```\n"
        f"口径要点：{notes}\n"
    )
