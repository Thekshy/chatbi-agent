"""故事与世界观参数（T2）：SPEC §5 契约的声明式设计参数。

- 类目树 / 品牌 / 价格带：数据形状设计；品牌权重即故事 3 的头部梯度来源。
- PROMOTIONS：30 场活动日历（含 618 / 双 11），是故事 2 的载体与其余故事的背景噪声。
- STORY1/2/3：三个植入故事的旋钮与验收区间；实测达成值以库内 SQL 为准
  （data/mock_gen.py 的 self_check），定稿后回填 SPEC §5.3。
"""

from datetime import date

SEED = 20260915
DATA_START = date(2025, 9, 15)
DATA_END = date(2026, 9, 13)  # 含端点，共 364 天

CATEGORY_L1 = ["护肤", "彩妆", "香水", "女装", "男装", "配饰"]

# l1 -> l2 -> l2 在该 l1 内的订单/GMV 份额（共 31 个 l2，约 30）
CATEGORY_L2: dict[str, dict[str, float]] = {
    "护肤": {"面部精华": 0.26, "面霜": 0.20, "面膜": 0.18, "化妆水": 0.12,
             "乳液": 0.09, "洁面": 0.08, "防晒": 0.07},
    "彩妆": {"口红": 0.22, "粉底液": 0.20, "气垫": 0.18, "眼影": 0.16,
             "腮红": 0.12, "定妆粉": 0.12},
    "香水": {"女士香水": 0.55, "男士香水": 0.30, "香氛礼盒": 0.15},
    "女装": {"连衣裙": 0.24, "衬衫": 0.20, "毛衣": 0.17, "半身裙": 0.15,
             "牛仔裤": 0.14, "风衣": 0.10},
    "男装": {"T恤": 0.28, "衬衫": 0.22, "休闲裤": 0.20, "卫衣": 0.16, "夹克": 0.14},
    "配饰": {"项链": 0.30, "耳饰": 0.28, "手链": 0.24, "丝巾": 0.18},
}

# l2 -> list_price 基准（元；实际价 = 基准 × 品牌溢价 × 对数正态噪声）
PRICE_BASE: dict[str, float] = {
    "面部精华": 480, "面霜": 320, "面膜": 120, "化妆水": 260, "乳液": 280,
    "洁面": 150, "防晒": 190,
    "口红": 220, "粉底液": 350, "气垫": 300, "眼影": 260, "腮红": 180, "定妆粉": 200,
    "女士香水": 580, "男士香水": 520, "香氛礼盒": 680,
    "连衣裙": 420, "半身裙": 300, "衬衫": 330, "风衣": 680, "毛衣": 400, "牛仔裤": 360,
    "T恤": 180, "男装衬衫": 300, "夹克": 720, "休闲裤": 380, "卫衣": 320,
    "项链": 350, "耳饰": 180, "手链": 260, "丝巾": 240,
}
# 女装/男装共用「衬衫」l2 名，但价格基准不同：用带 l1 前缀的键区分
PRICE_BASE["女装·衬衫"] = 330
PRICE_BASE["男装·衬衫"] = 300

# 品牌（全部虚构）。顺序即权重从高到低，截取 BRAND_WEIGHT_TAIL 前缀后归一化。
BRANDS: dict[str, list[str]] = {
    "护肤": ["雪滢", "澄川", "云栀", "白汐", "屿光", "沐棠", "山野序", "芙未",
             "苔月", "素屿", "南蔓", "序言"],
    "彩妆": ["绯迦", "橘屿", "拾色", "镜屋", "未晚", "芯霁", "霞序", "酥糖",
             "白日梦", "齿轮"],
    "香水": ["雾屿", "拾香", "空港", "岸边", "晚风志", "苔原", "玻璃海", "十号码头"],
    "女装": ["栖枝", "山茶纪", "直线", "折纸", "素生", "阑珊", "空岛", "三月马",
             "慢船", "西窗"],
    "男装": ["常青", "硬壳", "城市地图", "平行", "燃点", "素线", "铁灰", "慢走"],
    "配饰": ["星尘", "细语", "银河指南", "结绳", "小满", "圆环", "叙珠", "拾光"],
}
BRAND_WEIGHT_TAIL = [0.22, 0.15, 0.115, 0.095, 0.08, 0.07, 0.06, 0.055, 0.05, 0.04, 0.035, 0.03]
# 护肤头部梯度更陡（故事 3）：全场 GMV Top1 出在护肤，且足以扛住故事 1 的护肤 6 月下滑
BRAND_WEIGHT_BY_L1: dict[str, list[float]] = {
    "护肤": [0.34, 0.13, 0.09, 0.07, 0.065, 0.06, 0.055, 0.05, 0.045, 0.04, 0.025, 0.02],
}
TOP_BRAND_PREMIUM = {0: 1.12, 1: 1.06}  # 头部品牌价格溢价，强化 GMV 梯度

# 30 场活动。cat=None 表示全场；uv/cvr 为活动期流量/转化乘子；tag 为命中活动订单的
# promo_id 打标率；new_mult 为新客获取放大（仅 618）；p_repeat 为新客 30 日复购标记率。
# 约束：全场活动互不重叠、任一 l1 上活动互不重叠；护肤在故事 1 区间（05-21~06-30）
# 内除全场 618 外无其他活动，避免搅动故事数值。
PROMOTIONS: list[dict] = [
    dict(id=1, name="开业大促", cat=None, start=date(2025, 9, 15), end=date(2025, 9, 30),
         type="full_reduction", uv=1.55, cvr=1.18, tag=0.70),
    dict(id=2, name="国庆出游季", cat=None, start=date(2025, 10, 1), end=date(2025, 10, 7),
         type="discount", uv=1.20, cvr=1.08, tag=0.55),
    dict(id=3, name="彩妆品牌周", cat="彩妆", start=date(2025, 10, 20), end=date(2025, 10, 24),
         type="gift", uv=1.12, cvr=1.05, tag=0.45),
    dict(id=4, name="双11大促", cat=None, start=date(2025, 11, 1), end=date(2025, 11, 11),
         type="full_reduction", uv=1.45, cvr=1.16, tag=0.75),
    dict(id=5, name="双11返场", cat="彩妆", start=date(2025, 11, 14), end=date(2025, 11, 16),
         type="flash", uv=1.10, cvr=1.04, tag=0.40),
    dict(id=6, name="黑五香水周", cat="香水", start=date(2025, 11, 28), end=date(2025, 11, 30),
         type="discount", uv=1.12, cvr=1.05, tag=0.45),
    dict(id=7, name="双12闪购", cat=None, start=date(2025, 12, 12), end=date(2025, 12, 14),
         type="flash", uv=1.12, cvr=1.05, tag=0.45),
    dict(id=8, name="年末焕新", cat="女装", start=date(2025, 12, 19), end=date(2025, 12, 25),
         type="discount", uv=1.15, cvr=1.06, tag=0.50),
    dict(id=9, name="元旦焕新", cat=None, start=date(2026, 1, 1), end=date(2026, 1, 3),
         type="discount", uv=1.10, cvr=1.04, tag=0.40),
    dict(id=10, name="年货节", cat="男装", start=date(2026, 1, 9), end=date(2026, 1, 18),
         type="full_reduction", uv=1.18, cvr=1.07, tag=0.50),
    dict(id=11, name="情人节礼遇", cat="香水", start=date(2026, 2, 9), end=date(2026, 2, 14),
         type="gift", uv=1.15, cvr=1.06, tag=0.55),
    dict(id=12, name="开学焕新", cat="男装", start=date(2026, 2, 20), end=date(2026, 2, 24),
         type="discount", uv=1.08, cvr=1.03, tag=0.40),
    dict(id=13, name="女王节", cat="女装", start=date(2026, 3, 1), end=date(2026, 3, 8),
         type="full_reduction", uv=1.25, cvr=1.10, tag=0.60),
    dict(id=14, name="女王节彩妆", cat="彩妆", start=date(2026, 3, 5), end=date(2026, 3, 8),
         type="gift", uv=1.18, cvr=1.06, tag=0.50),
    dict(id=15, name="春季上新", cat="女装", start=date(2026, 3, 16), end=date(2026, 3, 22),
         type="discount", uv=1.10, cvr=1.04, tag=0.40),
    dict(id=16, name="春季护肤节", cat="护肤", start=date(2026, 3, 23), end=date(2026, 3, 29),
         type="gift", uv=1.15, cvr=1.06, tag=0.50),
    dict(id=17, name="清明出行", cat="配饰", start=date(2026, 4, 3), end=date(2026, 4, 6),
         type="discount", uv=1.10, cvr=1.04, tag=0.40),
    dict(id=18, name="春夏新装", cat="女装", start=date(2026, 4, 10), end=date(2026, 4, 16),
         type="discount", uv=1.10, cvr=1.04, tag=0.40),
    dict(id=19, name="谷雨焕肤", cat="护肤", start=date(2026, 4, 20), end=date(2026, 4, 23),
         type="gift", uv=1.08, cvr=1.03, tag=0.35),
    dict(id=20, name="五一小长假", cat=None, start=date(2026, 5, 1), end=date(2026, 5, 5),
         type="discount", uv=1.15, cvr=1.05, tag=0.50),
    dict(id=21, name="母亲节礼遇", cat="香水", start=date(2026, 5, 8), end=date(2026, 5, 12),
         type="gift", uv=1.15, cvr=1.05, tag=0.50),
    dict(id=22, name="618大促", cat=None, start=date(2026, 5, 31), end=date(2026, 6, 18),
         type="discount", uv=1.05, cvr=1.05, tag=0.80, new_mult=1.8),
    dict(id=23, name="端午配饰节", cat="配饰", start=date(2026, 6, 19), end=date(2026, 6, 22),
         type="discount", uv=1.10, cvr=1.04, tag=0.40),
    dict(id=24, name="毕业季彩妆", cat="彩妆", start=date(2026, 6, 25), end=date(2026, 6, 30),
         type="flash", uv=1.12, cvr=1.05, tag=0.45),
    dict(id=25, name="暑期狂欢", cat=None, start=date(2026, 7, 1), end=date(2026, 7, 12),
         type="discount", uv=1.15, cvr=1.06, tag=0.50),
    dict(id=26, name="夏日男装周", cat="男装", start=date(2026, 7, 18), end=date(2026, 7, 24),
         type="discount", uv=1.10, cvr=1.04, tag=0.40),
    dict(id=27, name="88会员节", cat=None, start=date(2026, 8, 8), end=date(2026, 8, 10),
         type="discount", uv=1.18, cvr=1.07, tag=0.55),
    dict(id=28, name="七夕礼遇", cat="香水", start=date(2026, 8, 17), end=date(2026, 8, 22),
         type="gift", uv=1.20, cvr=1.08, tag=0.55),
    dict(id=29, name="开学季", cat="男装", start=date(2026, 8, 25), end=date(2026, 8, 31),
         type="discount", uv=1.15, cvr=1.05, tag=0.45),
    dict(id=30, name="初秋护肤节", cat="护肤", start=date(2026, 9, 1), end=date(2026, 9, 7),
         type="gift", uv=1.12, cvr=1.05, tag=0.45),
]

# 故事 1：面部精华下滑。流量旋钮作用于护肤 l1 的 UV，转化旋钮作用于面部精华 l2
# 份额；GMV 环比 ≈ (1+UV 环比) × (1+单位 UV 产出环比) − 1，可乘法分解。
STORY1 = {
    "l1": "护肤",
    "l2": "面部精华",
    "ramp_start": date(2026, 5, 21),   # 5 月下旬渐起
    "full_from": date(2026, 6, 1),     # 6 月全效
    "recover_start": date(2026, 7, 1),  # 7 月上旬回升
    "recover_end": date(2026, 7, 10),
    "uv_drop": 0.145,   # 全效护肤 UV 降幅（5→6 月还含 31/30 天日历效应 ≈ −3.2%）
    "conv_drop": 0.185,  # 全效面部精华 l2 份额降幅（实测还叠加深折/客单波动）
    "gmv_decline_range": (0.20, 0.30),
    "uv_decline_range": (0.12, 0.18),
}

# 故事 2：618 复购拉升。活动期/基线期新客 30 日复购率（§5.2 口径）。
STORY2 = {
    "promo_name": "618大促",
    "activity_window": (date(2026, 5, 31), date(2026, 6, 19)),  # [起, 止)，paid_at
    "baseline_window": (date(2026, 5, 1), date(2026, 5, 31)),   # 活动前 30 天
    "p_repeat_activity": 0.18,
    "p_repeat_base": 0.11,
    "new_user_mult": 1.8,
    "diff_range": (0.06, 0.10),          # 复购率差（百分点）
    "rate_activity_range": (0.13, 0.23),
    "rate_base_range": (0.06, 0.16),
}

# 故事 3：品牌 TopN 头部梯度（全场 GMV）。
STORY3 = {
    "min_top1_vs_top2": 1.5,
    "stable_months": 12,  # 逐月 Top1 恒定的月数
}
