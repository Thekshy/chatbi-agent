"""metrics：指标字典的 SQL 模式与维度下钻模板（T5 / SPEC §5.2）。

每个模板三件套：``sql``（含 ``<占位符>`` 的示意模式，注入 executor 提示词）、
``example_sql``（可执行的真实示例——必须过 validate_sql 并可在 mock 库上跑通，
单测据此钉死模板本身不违反只读边界）、``notes``（口径要点，结论正确率的裁判口径）。

口径与 data/mock_gen.py::self_check 的裁判 SQL 保持一致：
GMV/订单数/客单价走 orders ⋈ order_items ⋈ products 三表、status ∈
{paid, shipped, completed}、时间锚点 paid_at；复购 = 首单后 30 天内第二单。
模板中日期一律左闭右开（paid_at >= start AND < end）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricTemplate:
    key: str
    title: str
    tables: tuple[str, ...]
    sql: str  # 示意模式（<占位符>），仅注入提示词，不执行
    example_sql: str  # 可执行示例（真实日期/值），必须过 validate_sql
    notes: tuple[str, ...]


_GMV_FROM = (
    "FROM orders o\n"
    "JOIN order_items oi ON oi.order_id = o.order_id\n"
    "JOIN products p ON p.product_id = oi.product_id\n"
    "WHERE o.status IN ('paid', 'shipped', 'completed')"
)

_PAID_STATUS_NOTE = "订单状态只取 paid/shipped/completed（剔除 pending_payment/cancelled/refunded）"
_PAID_AT_NOTE = "时间一律按 o.paid_at 过滤，左闭右开（>= 开始 AND < 结束），不含运费"

METRIC_TEMPLATES: dict[str, MetricTemplate] = {
    t.key: t
    for t in (
        MetricTemplate(
            key="gmv",
            title="GMV 金额（元）",
            tables=("orders", "order_items", "products"),
            sql=(
                "SELECT SUM(oi.unit_price * oi.quantity) AS gmv\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= <开始日期> AND o.paid_at < <结束日期>\n"
                "  AND p.category_l1 = <一级类目>   -- 可选过滤\n"
                "  AND p.category_l2 = <二级类目>   -- 可选\n"
                "  AND p.brand = <品牌>             -- 可选\n"
                "  AND o.channel = <渠道>           -- 可选：app/web/mini/live\n"
                "  AND o.promo_id = <活动ID>        -- 可选：活动订单口径"
            ),
            example_sql=(
                "SELECT SUM(oi.unit_price * oi.quantity) AS gmv\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= '2026-06-01' AND o.paid_at < '2026-07-01'\n"
                "  AND p.category_l1 = '护肤'"
            ),
            notes=(
                "GMV = SUM(order_items.unit_price × quantity)，明细求和",
                _PAID_STATUS_NOTE,
                _PAID_AT_NOTE,
            ),
        ),
        MetricTemplate(
            key="order_count",
            title="支付订单数（去重 order_id）",
            tables=("orders", "order_items", "products"),
            sql=(
                "SELECT COUNT(DISTINCT o.order_id) AS paid_orders\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= <开始日期> AND o.paid_at < <结束日期>\n"
                "  -- 品类/品牌/渠道过滤同 gmv 模板（不需要商品属性过滤时可省去两张 join）"
            ),
            example_sql=(
                "SELECT COUNT(DISTINCT o.order_id) AS paid_orders\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= '2026-06-01' AND o.paid_at < '2026-07-01'\n"
                "  AND p.category_l1 = '护肤'"
            ),
            notes=(
                "支付订单数 = 已支付口径的去重 order_id 数",
                _PAID_STATUS_NOTE,
                _PAID_AT_NOTE,
            ),
        ),
        MetricTemplate(
            key="aov",
            title="客单价（GMV ÷ 支付订单数）",
            tables=("orders", "order_items", "products"),
            sql=(
                "SELECT SUM(oi.unit_price * oi.quantity) AS gmv,\n"
                "       COUNT(DISTINCT o.order_id) AS paid_orders,\n"
                "       SUM(oi.unit_price * oi.quantity) / COUNT(DISTINCT o.order_id) AS aov\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= <开始日期> AND o.paid_at < <结束日期>\n"
                "  -- 过滤条件同 gmv 模板"
            ),
            example_sql=(
                "SELECT SUM(oi.unit_price * oi.quantity) AS gmv,\n"
                "       COUNT(DISTINCT o.order_id) AS paid_orders,\n"
                "       SUM(oi.unit_price * oi.quantity) / COUNT(DISTINCT o.order_id) AS aov\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= '2026-06-01' AND o.paid_at < '2026-07-01'"
            ),
            notes=(
                "客单价 = GMV ÷ 支付订单数（分母是去重订单数，不是用户数、不是明细行数）",
                _PAID_STATUS_NOTE,
            ),
        ),
        MetricTemplate(
            key="period_compare",
            title="同环比（两期对比与变化率）",
            tables=("orders", "order_items", "products"),
            sql=(
                "SELECT to_char(o.paid_at, 'YYYY-MM') AS period,\n"
                "       SUM(oi.unit_price * oi.quantity) AS gmv   -- 或其他指标\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= <上期开始> AND o.paid_at < <本期结束>\n"
                "  -- 两期落在同一查询里各占一行\n"
                "GROUP BY 1 ORDER BY 1"
            ),
            example_sql=(
                "SELECT to_char(o.paid_at, 'YYYY-MM') AS period,\n"
                "       SUM(oi.unit_price * oi.quantity) AS gmv\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= '2026-05-01' AND o.paid_at < '2026-07-01'\n"
                "GROUP BY 1 ORDER BY 1"
            ),
            notes=(
                "变化率 = (本期 − 上期) ÷ 上期；环比=月对月、同比=对上年同月、周对周",
                "两期窗口长度应相同（自然月对自然月即可）",
                _PAID_AT_NOTE,
            ),
        ),
        MetricTemplate(
            key="topn",
            title="TopN 排名及占比",
            tables=("orders", "order_items", "products"),
            sql=(
                "SELECT p.brand AS dim,   -- 维度可换 p.category_l1 / p.category_l2 / 产品\n"
                "       SUM(oi.unit_price * oi.quantity) AS gmv,\n"
                "       SUM(oi.unit_price * oi.quantity)\n"
                "         / SUM(SUM(oi.unit_price * oi.quantity)) OVER () AS share\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= <开始日期> AND o.paid_at < <结束日期>\n"
                "GROUP BY 1 ORDER BY 2 DESC LIMIT <N>"
            ),
            example_sql=(
                "SELECT p.brand AS dim,\n"
                "       SUM(oi.unit_price * oi.quantity) AS gmv,\n"
                "       SUM(oi.unit_price * oi.quantity)\n"
                "         / SUM(SUM(oi.unit_price * oi.quantity)) OVER () AS share\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= '2026-05-31' AND o.paid_at < '2026-06-19'\n"
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
            ),
            notes=(
                "占比 = 该维度 GMV ÷ 同窗口总 GMV（窗口函数 OVER () 一次算出）",
                _PAID_STATUS_NOTE,
                _PAID_AT_NOTE,
            ),
        ),
        MetricTemplate(
            key="repurchase_30d",
            title="新客 30 日复购（单窗口复购率）",
            tables=("orders",),
            sql=(
                "WITH paid AS (\n"
                "  SELECT user_id, paid_at FROM orders\n"
                "  WHERE status IN ('paid', 'shipped', 'completed')),\n"
                "f AS (SELECT user_id, MIN(paid_at) AS fp FROM paid GROUP BY 1),\n"
                "w AS (SELECT user_id, fp FROM f\n"
                "      WHERE fp >= <窗口开始> AND fp < <窗口结束>),\n"
                "r AS (SELECT DISTINCT w.user_id FROM w JOIN paid\n"
                "      ON paid.user_id = w.user_id AND paid.paid_at > w.fp\n"
                "      AND paid.paid_at <= w.fp + interval '30 days')\n"
                "SELECT (SELECT count(*) FROM w) AS new_users,\n"
                "       (SELECT count(*) FROM r) AS repeaters"
            ),
            example_sql=(
                "WITH paid AS (\n"
                "  SELECT user_id, paid_at FROM orders\n"
                "  WHERE status IN ('paid', 'shipped', 'completed')),\n"
                "f AS (SELECT user_id, MIN(paid_at) AS fp FROM paid GROUP BY 1),\n"
                "w AS (SELECT user_id, fp FROM f\n"
                "      WHERE fp >= '2026-05-31' AND fp < '2026-06-19'),\n"
                "r AS (SELECT DISTINCT w.user_id FROM w JOIN paid\n"
                "      ON paid.user_id = w.user_id AND paid.paid_at > w.fp\n"
                "      AND paid.paid_at <= w.fp + interval '30 days')\n"
                "SELECT (SELECT count(*) FROM w) AS new_users,\n"
                "       (SELECT count(*) FROM r) AS repeaters"
            ),
            notes=(
                "复购 = 首单（paid 口径）后 30 天内产生第二单；窗口内无首单的老客不计入",
                "复购率 = 复购用户数 ÷ 窗口内首单新客数（分母是 new_users）",
                "自连接 orders 即可，无独立用户表",
            ),
        ),
        MetricTemplate(
            key="activity_repurchase_lift",
            title="活动复购增量（活动期 vs 基线期新客复购率之差）",
            tables=("orders",),
            sql=(
                "WITH paid AS (SELECT user_id, paid_at FROM orders\n"
                "              WHERE status IN ('paid', 'shipped', 'completed')),\n"
                "f AS (SELECT user_id, MIN(paid_at) AS fp FROM paid GROUP BY 1),\n"
                "w AS (SELECT user_id, fp, CASE\n"
                "        WHEN fp >= <活动开始> AND fp < <活动结束> THEN 'activity'\n"
                "        WHEN fp >= <基线开始> AND fp < <基线结束> THEN 'baseline'\n"
                "      END AS cohort\n"
                "      FROM f\n"
                "      WHERE (fp >= <活动开始> AND fp < <活动结束>)\n"
                "         OR (fp >= <基线开始> AND fp < <基线结束>)),\n"
                "r AS (SELECT DISTINCT w.user_id, w.cohort FROM w JOIN paid\n"
                "      ON paid.user_id = w.user_id AND paid.paid_at > w.fp\n"
                "      AND paid.paid_at <= w.fp + interval '30 days')\n"
                "SELECT w.cohort, count(*) AS new_users,\n"
                "       count(*) FILTER (WHERE r.user_id IS NOT NULL) AS repeaters\n"
                "FROM w LEFT JOIN r ON r.user_id = w.user_id AND r.cohort = w.cohort\n"
                "GROUP BY 1"
            ),
            example_sql=(
                "WITH paid AS (SELECT user_id, paid_at FROM orders\n"
                "              WHERE status IN ('paid', 'shipped', 'completed')),\n"
                "f AS (SELECT user_id, MIN(paid_at) AS fp FROM paid GROUP BY 1),\n"
                "w AS (SELECT user_id, fp, CASE\n"
                "        WHEN fp >= '2026-05-31' AND fp < '2026-06-19' THEN 'activity'\n"
                "        WHEN fp >= '2026-05-01' AND fp < '2026-05-31' THEN 'baseline'\n"
                "      END AS cohort\n"
                "      FROM f\n"
                "      WHERE (fp >= '2026-05-31' AND fp < '2026-06-19')\n"
                "         OR (fp >= '2026-05-01' AND fp < '2026-05-31')),\n"
                "r AS (SELECT DISTINCT w.user_id, w.cohort FROM w JOIN paid\n"
                "      ON paid.user_id = w.user_id AND paid.paid_at > w.fp\n"
                "      AND paid.paid_at <= w.fp + interval '30 days')\n"
                "SELECT w.cohort, count(*) AS new_users,\n"
                "       count(*) FILTER (WHERE r.user_id IS NOT NULL) AS repeaters\n"
                "FROM w LEFT JOIN r ON r.user_id = w.user_id AND r.cohort = w.cohort\n"
                "GROUP BY 1"
            ),
            notes=(
                "活动复购增量 = 活动期新客 30 日复购率 − 基线期（活动前同期长）新客复购率",
                "基线期取活动开始前、与活动期等长的窗口（618 例：05-01~05-31）",
                "复购率 = repeaters ÷ new_users，逐 cohort 计算",
            ),
        ),
        MetricTemplate(
            key="conversion",
            title="转化率（当日 paid 订单数 ÷ 当日 UV）",
            tables=("traffic_daily", "orders", "order_items", "products"),
            sql=(
                "SELECT t.date, t.uv, COALESCE(o.paid_orders, 0) AS paid_orders,\n"
                "       CASE WHEN t.uv > 0\n"
                "            THEN COALESCE(o.paid_orders, 0)::numeric / t.uv END AS cvr\n"
                "FROM traffic_daily t\n"
                "LEFT JOIN (\n"
                "  SELECT o.paid_at::date AS d, COUNT(DISTINCT o.order_id) AS paid_orders\n"
                f"  {_GMV_FROM}\n"
                "    AND o.paid_at >= <开始日期> AND o.paid_at < <结束日期>\n"
                "    AND p.category_l1 = <类目>\n"
                "  GROUP BY 1) o ON o.d = t.date\n"
                "WHERE t.category_l1 = <类目> AND t.date >= <开始日期> AND t.date < <结束日期>\n"
                "ORDER BY t.date"
            ),
            example_sql=(
                "SELECT t.date, t.uv, COALESCE(o.paid_orders, 0) AS paid_orders,\n"
                "       CASE WHEN t.uv > 0\n"
                "            THEN COALESCE(o.paid_orders, 0)::numeric / t.uv END AS cvr\n"
                "FROM traffic_daily t\n"
                "LEFT JOIN (\n"
                "  SELECT o.paid_at::date AS d, COUNT(DISTINCT o.order_id) AS paid_orders\n"
                f"  {_GMV_FROM}\n"
                "    AND o.paid_at >= '2026-06-01' AND o.paid_at < '2026-07-01'\n"
                "    AND p.category_l1 = '护肤'\n"
                "  GROUP BY 1) o ON o.d = t.date\n"
                "WHERE t.category_l1 = '护肤'\n"
                "  AND t.date >= '2026-06-01' AND t.date < '2026-07-01'\n"
                "ORDER BY t.date"
            ),
            notes=(
                "转化率 = 当日 paid 订单数 ÷ 当日 UV（品类/渠道维度同理）",
                "traffic_daily 是日粒度（date/category_l1/channel/pv/uv），无二级类目",
                "订单侧日期用 paid_at::date 对齐流量日期",
            ),
        ),
        MetricTemplate(
            key="drilldown_by_dim",
            title="维度贡献度分解（下钻归因）",
            tables=("orders", "order_items", "products"),
            sql=(
                "SELECT p.category_l2 AS dim,   -- 维度可换 to_char(o.paid_at,'YYYY-MM-DD')\n"
                "                                    -- / o.channel / p.brand\n"
                "       to_char(o.paid_at, 'YYYY-MM') AS period,\n"
                "       SUM(oi.unit_price * oi.quantity) AS gmv\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= <上期开始> AND o.paid_at < <本期结束>\n"
                "  AND p.category_l1 = <类目>   -- 可选：限定归因范围\n"
                "GROUP BY 1, 2 ORDER BY 1, 2"
            ),
            example_sql=(
                "SELECT p.category_l2 AS dim,\n"
                "       to_char(o.paid_at, 'YYYY-MM') AS period,\n"
                "       SUM(oi.unit_price * oi.quantity) AS gmv\n"
                f"{_GMV_FROM}\n"
                "  AND o.paid_at >= '2026-05-01' AND o.paid_at < '2026-07-01'\n"
                "  AND p.category_l1 = '护肤'\n"
                "GROUP BY 1, 2 ORDER BY 1, 2"
            ),
            notes=(
                "贡献度 = 该维度两期变化量 ÷ 整体两期变化量（正负都保留）",
                "只做贡献度分解，不做因果推断（SPEC §3）",
                "按天下钻用 date_trunc('day', o.paid_at)，渠道用 o.channel",
            ),
        ),
        MetricTemplate(
            key="traffic_vs_conversion",
            title="流量 vs 转化拆解（乘法分解）",
            tables=("orders", "order_items", "products", "traffic_daily"),
            sql=(
                "WITH gmv AS (\n"
                "  SELECT to_char(o.paid_at, 'YYYY-MM') AS ym,\n"
                "         SUM(oi.unit_price * oi.quantity) AS gmv\n"
                f"  {_GMV_FROM}\n"
                "    AND o.paid_at >= <上期开始> AND o.paid_at < <本期结束>\n"
                "    AND p.category_l1 = <类目>\n"
                "  GROUP BY 1),\n"
                "uv AS (SELECT to_char(\"date\", 'YYYY-MM') AS ym, SUM(uv) AS uv\n"
                "       FROM traffic_daily\n"
                "       WHERE category_l1 = <类目>\n"
                "         AND \"date\" >= <上期开始> AND \"date\" < <本期结束>\n"
                "       GROUP BY 1)\n"
                "SELECT gmv.ym, gmv.gmv, uv.uv, gmv.gmv / uv.uv AS gmv_per_uv\n"
                "FROM gmv JOIN uv ON uv.ym = gmv.ym ORDER BY 1"
            ),
            example_sql=(
                "WITH gmv AS (\n"
                "  SELECT to_char(o.paid_at, 'YYYY-MM') AS ym,\n"
                "         SUM(oi.unit_price * oi.quantity) AS gmv\n"
                f"  {_GMV_FROM}\n"
                "    AND o.paid_at >= '2026-05-01' AND o.paid_at < '2026-07-01'\n"
                "    AND p.category_l1 = '护肤'\n"
                "  GROUP BY 1),\n"
                "uv AS (SELECT to_char(\"date\", 'YYYY-MM') AS ym, SUM(uv) AS uv\n"
                "       FROM traffic_daily\n"
                "       WHERE category_l1 = '护肤'\n"
                "         AND \"date\" >= '2026-05-01' AND \"date\" < '2026-07-01'\n"
                "       GROUP BY 1)\n"
                "SELECT gmv.ym, gmv.gmv, uv.uv, gmv.gmv / uv.uv AS gmv_per_uv\n"
                "FROM gmv JOIN uv ON uv.ym = gmv.ym ORDER BY 1"
            ),
            notes=(
                "GMV 环比 = (1+UV 环比) × (1+单位 UV 产出环比) − 1，两因子分别量化"
                "流量与转化贡献",
                "单位 UV 产出 = GMV ÷ UV；UV 取所属一级类目（traffic_daily 无二级粒度）",
                "归因二级类目时用其所属 l1 的流量近似",
            ),
        ),
        MetricTemplate(
            key="promotions_lookup",
            title="活动日历查询（活动名 → 区间/类型/类目）",
            tables=("promotions",),
            sql=(
                "SELECT promo_id, promo_name, category_l1, start_date, end_date, promo_type\n"
                "FROM promotions\n"
                "WHERE promo_name LIKE <模糊活动名>   -- 或按日期/类型过滤"
            ),
            example_sql=(
                "SELECT promo_id, promo_name, category_l1, start_date, end_date, promo_type\n"
                "FROM promotions WHERE promo_name LIKE '%618%'"
            ),
            notes=(
                "活动过滤两种口径：orders.promo_id = 活动ID（命中打标订单），或"
                " paid_at 落在 start_date~end_date（活动期间成交），结论须说明用哪种",
                "category_l1 为 NULL 表示全场活动",
            ),
        ),
    )
}


def get_template(key: str | None) -> MetricTemplate | None:
    """按 key 取模板；未知/缺失返回 None（executor 退化为通用生成）。"""
    if not key:
        return None
    return METRIC_TEMPLATES.get(key.strip().lower())


def catalog_text() -> str:
    """planner 提示词用的模板目录（一行一个，含表集提示）。"""
    return "\n".join(
        f"- {t.key}: {t.title}（涉及表：{', '.join(t.tables)}）"
        for t in METRIC_TEMPLATES.values()
    )
