"""mock 数据生成器（T2）：一键重建 SPEC §5 契约的 5 表数据并植入 3 个故事。

生成自洽性（SPEC §5.1）：以「日 × 类目(l1) × 渠道」为控制单元——先采 UV（季节 /
周末 / 活动 / 故事旋钮），再乘转化率 cvr 得 paid 订单量（小数部分跨日累计进位，
保证月度聚合紧贴设计值），最后按 l2 份额（含故事份额旋钮）展开明细与金额。
故事 1 的流量旋钮作用在护肤 l1 UV、转化旋钮作用在面部精华 l2 份额，两者乘法可分解；
故事 2 用「新客标记 + 预留复购槽位」精确控制 30 日复购率；故事 3 由品牌权重静态梯度保证。

口径对齐 §5.2：GMV 只计 status ∈ {paid, shipped, completed}、时间锚 paid_at；
cvr 的分子（支付订单数）= 同口径去重订单数，refunded/cancelled/pending 为纯噪声行。

用法：
    uv run python -m data.mock_gen                # 全量重建（drop+recreate）+ 自检报告
    uv run python -m data.mock_gen --check-only   # 只对现有库跑自检（优先只读账号）
    uv run python -m data.mock_gen --seed 42      # 换种子（默认 stories.SEED）
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import psycopg
from dotenv import load_dotenv
from psycopg import sql

from data import stories

# ---- 量级与基础参数（scale=1 为全量） ----
CHANNELS = ("app", "web", "mini", "live")

UV_BASE = 6350.0  # 日总 UV 基准
L1_UV_SHARE = {"护肤": 0.32, "彩妆": 0.22, "香水": 0.08, "女装": 0.20, "男装": 0.10, "配饰": 0.08}
L1_CVR_MULT = {"护肤": 1.00, "彩妆": 1.15, "香水": 0.80, "女装": 0.72, "男装": 0.90, "配饰": 1.00}
CH_UV_SHARE_DEFAULT = {"app": 0.44, "web": 0.16, "mini": 0.26, "live": 0.14}
CH_UV_SHARE_BY_L1 = {
    "彩妆": {"app": 0.38, "web": 0.10, "mini": 0.22, "live": 0.30},  # 直播卖彩妆
    "女装": {"app": 0.50, "web": 0.16, "mini": 0.24, "live": 0.10},
    "男装": {"app": 0.40, "web": 0.22, "mini": 0.24, "live": 0.14},
}
CH_CVR = {"app": 0.030, "web": 0.024, "mini": 0.036, "live": 0.060}
PV_RATIO = {"app": 4.5, "web": 3.6, "mini": 5.2, "live": 2.2}
WEEKEND_UV_MULT = 1.12
WEEKEND_CVR_MULT = 1.05
LIVE_WEEKEND_UV_MULT = 1.15

# l1 -> 月 -> UV 季节乘子（5 月与 6 月对护肤持平，故事 1 全由旋钮驱动）
SEASONAL_UV: dict[str, dict[int, float]] = {
    "护肤": {9: 1.02, 10: 1.02, 11: 1.05, 12: 0.97, 1: 0.95, 2: 0.93, 3: 1.00,
             4: 1.00, 5: 1.00, 6: 1.00, 7: 0.93, 8: 0.96},
    "彩妆": {9: 0.98, 10: 1.00, 11: 1.04, 12: 0.96, 1: 0.94, 2: 0.98, 3: 1.06,
             4: 1.04, 5: 1.02, 6: 1.00, 7: 1.00, 8: 1.00},
    "香水": {9: 0.95, 10: 1.00, 11: 1.10, 12: 1.08, 1: 0.98, 2: 1.10, 3: 0.98,
             4: 0.95, 5: 1.05, 6: 0.95, 7: 0.92, 8: 1.05},
    "女装": {9: 1.05, 10: 1.05, 11: 1.02, 12: 0.98, 1: 0.90, 2: 0.92, 3: 1.08,
             4: 1.05, 5: 1.00, 6: 0.98, 7: 0.92, 8: 0.98},
    "男装": {9: 1.00, 10: 1.02, 11: 1.03, 12: 0.97, 1: 0.92, 2: 0.95, 3: 1.00,
             4: 1.00, 5: 0.98, 6: 0.97, 7: 0.95, 8: 1.02},
    "配饰": {9: 0.98, 10: 1.06, 11: 1.06, 12: 1.05, 1: 0.95, 2: 1.05, 3: 0.98,
             4: 0.98, 5: 1.00, 6: 0.95, 7: 0.92, 8: 0.95},
}

SKU_COUNT_BY_L1 = {"护肤": 600, "彩妆": 400, "女装": 400, "男装": 240, "香水": 200, "配饰": 160}
# 每单件数分布（1..5 件的概率）与单行数量分布（qty=1/2/3）
ITEMS_DIST: dict[str, tuple[float, ...]] = {
    "护肤": (0.18, 0.28, 0.29, 0.15, 0.10),
    "彩妆": (0.18, 0.28, 0.28, 0.15, 0.11),
    "香水": (0.38, 0.32, 0.18, 0.08, 0.04),
    "女装": (0.44, 0.30, 0.16, 0.06, 0.04),
    "男装": (0.36, 0.32, 0.20, 0.08, 0.04),
    "配饰": (0.26, 0.30, 0.24, 0.12, 0.08),
}
QTY_DIST: dict[str, tuple[float, ...]] = {
    "护肤": (0.72, 0.20, 0.08), "彩妆": (0.72, 0.20, 0.08), "香水": (0.80, 0.15, 0.05),
    "女装": (0.90, 0.08, 0.02), "男装": (0.90, 0.08, 0.02), "配饰": (0.78, 0.16, 0.06),
}
DISC_NORMAL = (0.90, 1.00)   # 常规成交折扣区间（对 list_price）
DISC_PROMO = (0.85, 0.95)    # 活动打标订单折扣区间
RATE_REFUND, RATE_CANCEL, RATE_PENDING = 0.045, 0.075, 0.06  # 噪声行 = eligible × 比例

# 用户获取曲线：n_new(di) = K·exp(−di/τ) + floor，上限 CAP×当日剩余槽位
NEW_USER_K, NEW_USER_TAU, NEW_USER_FLOOR, NEW_USER_CAP = 240.0, 58.0, 40, 0.62
WARMUP_USERS = 2000          # 数据起点前已存在的老客（born_di 为负，首单被截尾）
USER_FREQ_SIGMA = 0.7        # 用户购买频率权重 ~ 对数正态
USER_COOLDOWN_DAYS = 5       # 同一用户两单最小间隔（有机单）
NEW_USER_SHIELD_DAYS = 31    # 新客首单后 31 天内不参与有机抽取，隔离 30 日复购窗口
REPEAT_GAP_MIN, REPEAT_GAP_MAX = 2, 29  # 复购单与首单的天间隔（≤29 保证任意时刻 < 30d）
REPEAT_SAME_L1 = 0.72        # 复购单仍买原 l1 的概率

# 下单小时分布（小时, 权重）


def _mk_cum(weights: list[float]) -> tuple[list[float], float]:
    cum, s = [], 0.0
    for w in weights:
        s += w
        cum.append(s)
    return cum, s


_HOURS: dict[str, list[tuple[int, int]]] = {
    "app": [(9, 1), (10, 1), (11, 1), (12, 2), (13, 2), (14, 2), (15, 2), (16, 2),
            (17, 2), (18, 3), (19, 6), (20, 8), (21, 8), (22, 6), (23, 3)],
    "web": [(9, 3), (10, 4), (11, 4), (12, 2), (13, 2), (14, 4), (15, 4), (16, 4),
            (17, 3), (18, 2), (20, 2), (21, 2), (22, 1)],
    "mini": [(10, 2), (11, 3), (12, 3), (13, 2), (14, 2), (15, 2), (16, 2), (17, 2),
             (18, 2), (19, 4), (20, 5), (21, 5), (22, 4), (23, 2)],
    "live": [(12, 1), (13, 1), (14, 1), (15, 1), (16, 1), (17, 1), (18, 2), (19, 6),
             (20, 7), (21, 7), (22, 5), (23, 3)],
}
_HOURS_CUM = {ch: _mk_cum([w for _, w in hs]) for ch, hs in _HOURS.items()}
_ITEMS_CUM = {l1: _mk_cum(list(d)) for l1, d in ITEMS_DIST.items()}
_QTY_CUM = {l1: _mk_cum(list(d)) for l1, d in QTY_DIST.items()}
_L1_ORDER_CUM = _mk_cum([L1_UV_SHARE[x] * L1_CVR_MULT[x] for x in stories.CATEGORY_L1])
_CH_CUM = {"default": _mk_cum([CH_UV_SHARE_DEFAULT[c] for c in CHANNELS])}
_CH_CUM.update({l1: _mk_cum([m[c] for c in CHANNELS]) for l1, m in CH_UV_SHARE_BY_L1.items()})

# 自检目标（§5.1 量级；traffic = 364 天 × 6 l1 × 4 渠道）
TARGETS = {"orders": 100_000, "order_items": 250_000, "products": 2_000,
           "traffic_daily": 8_736, "promotions": 30, "users": 30_000}
TOL = 0.10

STATUS_SET = ("pending_payment", "paid", "shipped", "completed", "cancelled", "refunded")


def _pick(rng: random.Random, cum: list[float], total: float) -> int:
    return min(bisect_right(cum, rng.random() * total), len(cum) - 1)


def _ramp(d: date, r0: date, r1: date, rec0: date, rec1: date) -> float:
    """故事 1 旋钮形状：r0 起线性升至 1（r1 全效），rec0 起线性降回 0（rec1 归零）。"""
    if d < r0:
        return 0.0
    if d < r1:
        return (d - r0).days / (r1 - r0).days
    if d < rec0:
        return 1.0
    if d < rec1:
        return 1.0 - (d - rec0).days / (rec1 - rec0).days
    return 0.0


@dataclass(slots=True)
class Product:
    pid: int
    name: str
    l1: str
    l2: str
    brand: str
    list_price: float
    weight: float


@dataclass(slots=True)
class _Order:
    seq: int
    di: int
    ts: datetime
    created: datetime
    paid: datetime | None
    user: int  # 0 起的用户下标
    status: str
    channel: str
    promo_id: int | None
    items: list[tuple[int, int, float]]  # (product_id, qty, unit_price)


@dataclass
class Dataset:
    promotions: list[tuple]
    products: list[tuple]
    traffic: list[tuple]
    orders: list[tuple]
    items: list[tuple]
    stats: dict


_SERIES_WORDS = ["修护", "焕亮", "水润", "紧致", "赋活", "舒缓", "净澈", "轻盈", "经典",
                 "臻选", "恒润", "焕新", "柔雾", "清透", "夜用", "晨间", "臻萃", "限定"]
_L2_SUFFIX = {"面部精华": "精华", "化妆水": "爽肤水", "女士香水": "香水", "男士香水": "香水"}
_SPECS: dict[str, list[str]] = {
    "护肤": ["30ml", "50ml", "100ml", "15ml", "5片装", "2支装"],
    "彩妆": ["3.5g", "5g", "30ml", "替换装", "礼盒装"],
    "香水": ["30ml", "50ml", "100ml", "礼盒装"],
    "女装": ["S", "M", "L", "XL", "均码"],
    "男装": ["M", "L", "XL", "XXL", "均码"],
    "配饰": ["常规款", "礼盒装", "限定款"],
}


def _largest_remainder(total: int, weights: list[float], min_count: int = 0) -> list[int]:
    s = sum(weights)
    exact = [w / s * total for w in weights]
    counts = [max(min_count, math.floor(e)) for e in exact]
    rest = total - sum(counts)
    assert rest >= 0, "min_count 抬高后超出总额"
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - math.floor(exact[i])), i))
    for k in range(rest):
        counts[order[k % len(order)]] += 1
    return counts


def _build_products(rng: random.Random) -> tuple[list[Product], dict[tuple[str, str], tuple]]:
    products: list[Product] = []
    used_names: set[str] = set()
    for l1 in stories.CATEGORY_L1:
        brands = stories.BRANDS[l1]
        tail = stories.BRAND_WEIGHT_BY_L1.get(l1, stories.BRAND_WEIGHT_TAIL)[: len(brands)]
        s = sum(tail)
        bw = [w / s for w in tail]
        l2w = stories.CATEGORY_L2[l1]
        pairs = [(bi, b, l2) for bi, b in enumerate(brands) for l2 in l2w]
        weights = [bw[bi] * l2w[l2] for bi, _, l2 in pairs]
        counts = _largest_remainder(SKU_COUNT_BY_L1[l1], weights, min_count=2)
        for (bi, b, l2), c in zip(pairs, counts, strict=True):
            premium = stories.TOP_BRAND_PREMIUM.get(bi, 1.0)
            base = stories.PRICE_BASE.get(f"{l1}·{l2}", stories.PRICE_BASE[l2])
            for _ in range(c):
                pid = len(products) + 1
                lp = base * premium * math.exp(rng.gauss(0, 0.22)) * rng.uniform(0.95, 1.05)
                lp = max(10.0, round(lp, 2))
                name = (f"{b}·{rng.choice(_SERIES_WORDS)}{_L2_SUFFIX.get(l2, l2)} "
                        f"{rng.choice(_SPECS[l1])}")
                if name in used_names:
                    name = f"{name} No.{pid:04d}"
                used_names.add(name)
                products.append(Product(pid, name, l1, l2, b, lp, math.exp(rng.gauss(0, 0.5))))
    sku: dict[tuple[str, str], tuple] = {}
    agg: dict[tuple[str, str], list[Product]] = defaultdict(list)
    for p in products:
        agg[(p.l1, p.l2)].append(p)
    for key, plist in agg.items():
        cum, s = _mk_cum([p.weight for p in plist])
        sku[key] = (plist, cum, s)
    return products, sku


def build(seed: int = stories.SEED, scale: float = 1.0) -> Dataset:
    """纯内存生成全部数据。随机数仅用于可复现的 mock 数据生成（非加密用途），
    刻意使用带种子的确定性 PRNG——这是 T2 验收「种子固定可复现」的前提。"""
    rng = random.Random(seed)
    s1, s2 = stories.STORY1, stories.STORY2
    days = [stories.DATA_START + timedelta(i)
            for i in range((stories.DATA_END - stories.DATA_START).days + 1)]
    last_di = len(days) - 1

    def ramp(d: date) -> float:
        return _ramp(d, s1["ramp_start"], s1["full_from"], s1["recover_start"], s1["recover_end"])

    products, sku = _build_products(rng)

    # 活动网格：(di, l1) -> 活动（设计上同 l1 不重叠）
    promo_grid: dict[tuple[int, str], dict] = {}
    di_of = {d: i for i, d in enumerate(days)}
    for pr in stories.PROMOTIONS:
        assert stories.DATA_START <= pr["start"] <= pr["end"] <= stories.DATA_END, pr
        for di in range(di_of[pr["start"]], di_of[pr["end"]] + 1):
            for l1 in stories.CATEGORY_L1 if pr["cat"] is None else [pr["cat"]]:
                key = (di, l1)
                assert key not in promo_grid, f"活动重叠: {pr['name']} @ {days[di]} {l1}"
                promo_grid[key] = pr
    act0, act1 = s2["activity_window"]
    new_mult_618 = next(p.get("new_mult", 1.0) for p in stories.PROMOTIONS
                        if p["name"] == s2["promo_name"])

    # ---- 流量层：UV/PV + paid 订单量（小数跨日累计进位） ----
    carry: dict[tuple[str, str], float] = {}
    n_elig: dict[tuple[int, str, str], int] = {}
    traffic: list[tuple] = []
    uv_month_skincare: dict[tuple[int, int], float] = defaultdict(float)
    for di, d in enumerate(days):
        weekend = d.weekday() >= 5
        for l1 in stories.CATEGORY_L1:
            chs = CH_UV_SHARE_BY_L1.get(l1, CH_UV_SHARE_DEFAULT)
            story_uv = 1.0 - s1["uv_drop"] * ramp(d) if l1 == s1["l1"] else 1.0
            for ch in CHANNELS:
                pr = promo_grid.get((di, l1))
                wk = WEEKEND_UV_MULT if weekend else 1.0
                if weekend and ch == "live":
                    wk *= LIVE_WEEKEND_UV_MULT
                uv_f = (UV_BASE * scale * L1_UV_SHARE[l1] * chs[ch]
                        * SEASONAL_UV[l1][d.month] * wk * (pr["uv"] if pr else 1.0)
                        * story_uv * math.exp(rng.gauss(0, 0.07)))
                uv = max(2, round(uv_f))
                pv = max(uv + 1, round(uv * PV_RATIO[ch] * math.exp(rng.gauss(0, 0.05))))
                traffic.append((d, l1, ch, pv, uv))
                if l1 == s1["l1"]:
                    uv_month_skincare[(d.year, d.month)] += uv
                cvr = (CH_CVR[ch] * L1_CVR_MULT[l1]
                       * (WEEKEND_CVR_MULT if weekend else 1.0) * (pr["cvr"] if pr else 1.0))
                k = (l1, ch)
                c = carry.get(k, 0.0) + uv * cvr
                n = int(c)
                carry[k] = c - n
                n_elig[(di, l1, ch)] = n

    # ---- 订单层 ----
    users_w: list[float] = []
    users_born: list[int] = []
    users_last: list[int] = []
    for _ in range(WARMUP_USERS):  # 老客：born 为负，首单被截尾，不会落入故事窗口
        users_w.append(math.exp(rng.gauss(0, USER_FREQ_SIGMA)))
        users_born.append(-rng.randint(60, 400))
        users_last.append(-10_000)
    cumw: list[float] = []
    cum_total = 0.0

    def rebuild_cumw() -> None:
        nonlocal cumw, cum_total
        cumw, cum_total = _mk_cum(list(users_w))

    rebuild_cumw()

    def pick_organic(di: int) -> int | None:
        def ok(i: int) -> bool:
            return (users_born[i] <= di - NEW_USER_SHIELD_DAYS
                    and users_last[i] <= di - USER_COOLDOWN_DAYS)

        for _ in range(50):
            i = _pick(rng, cumw, cum_total)
            if ok(i):
                return i
        start = rng.randrange(len(users_w))
        for k in range(len(users_w)):
            i = (start + k) % len(users_w)
            if ok(i):
                return i
        return None

    reserved: dict[tuple[int, str, str], list[int]] = defaultdict(list)
    # 逐窗阶梯计数：让每窗复购标记数精确到 ±1，不随随机流漂移（否则参数微调会重掷所有骰子）
    flag_bal = {"act": 0.0, "base": 0.0, "bg": 0.0}
    flag_p = {"act": s2["p_repeat_activity"], "base": s2["p_repeat_base"],
              "bg": s2["p_repeat_base"]}
    base0, base1 = s2["baseline_window"]

    def flag_window(d: date) -> str:
        if act0 <= d < act1:
            return "act"
        if base0 <= d < base1:
            return "base"
        return "bg"

    orders: list[_Order] = []
    paid_days: dict[int, list[tuple[int, datetime]]] = defaultdict(list)
    gmv_brand_month: dict[tuple[tuple[int, int], str], float] = defaultdict(float)
    gmv_s1_month: dict[tuple[int, int], float] = defaultdict(float)
    counters = {"gmv_total": 0.0, "items": 0, "repeat_giveups": 0,
                "status": defaultdict(int)}

    l2_names = {l1: list(stories.CATEGORY_L2[l1]) for l1 in stories.CATEGORY_L1}
    l2_static = {l1: _mk_cum(list(stories.CATEGORY_L2[l1].values()))
                 for l1 in stories.CATEGORY_L1 if l1 != s1["l1"]}
    sk_cache: tuple[float, tuple | None] = (-1.0, None)

    def l2_cum_for(l1: str, di: int) -> tuple:
        nonlocal sk_cache
        if l1 != s1["l1"]:
            return l2_static[l1]
        r = ramp(days[di])
        if sk_cache[0] != r:
            ws = [w * ((1.0 - s1["conv_drop"] * r) if l2 == s1["l2"] else 1.0)
                  for l2, w in stories.CATEGORY_L2[l1].items()]
            sk_cache = (r, _mk_cum(ws))
        assert sk_cache[1] is not None
        return sk_cache[1]

    def emit_order(di: int, l1: str, ch: str, kind: str, user: int) -> None:
        d = days[di]
        hcum, htot = _HOURS_CUM[ch]
        hour = _HOURS[ch][_pick(rng, hcum, htot)][0]
        ts = datetime.combine(d, time(hour, rng.randrange(60), rng.randrange(60)))
        pr = promo_grid.get((di, l1))
        promo_id = None
        if pr:
            tagp = pr["tag"] * (1.0 if kind in ("eligible", "refunded") else 0.5)
            if rng.random() < tagp:
                promo_id = pr["id"]
        if kind in ("eligible", "refunded"):
            lag = min(-math.log(1.0 - rng.random()) * 1800.0, 21600.0)
            created, paid = ts - timedelta(seconds=lag), ts
        else:
            created, paid = ts, None
        if kind == "refunded":
            status = "refunded"
        elif kind == "eligible":
            status = ("paid" if di >= last_di - 2
                      else "shipped" if di >= last_di - 13 else "completed")
        else:
            status = "cancelled" if kind == "cancelled" else "pending_payment"
        cum, tot = l2_cum_for(l1, di)
        names = l2_names[l1]
        first_l2 = names[_pick(rng, cum, tot)]
        icum, itot = _ITEMS_CUM[l1]
        n_items = 1 + _pick(rng, icum, itot)
        qcum, qtot = _QTY_CUM[l1]
        its: list[tuple[int, int, float]] = []
        gmv = 0.0
        d_lo, d_hi = DISC_PROMO if promo_id else DISC_NORMAL
        for j in range(n_items):
            l2 = first_l2 if (j and rng.random() < 0.40) else names[_pick(rng, cum, tot)]
            plist, pcum, ptot = sku[(l1, l2)]
            p = plist[_pick(rng, pcum, ptot)]
            qty = 1 + _pick(rng, qcum, qtot)
            price = max(0.01, round(p.list_price * rng.uniform(d_lo, d_hi), 2))
            its.append((p.pid, qty, price))
            if kind == "eligible":
                v = qty * price
                gmv += v
                gmv_brand_month[((d.year, d.month), p.brand)] += v
                if p.l1 == s1["l1"] and p.l2 == s1["l2"]:
                    gmv_s1_month[(d.year, d.month)] += v
        counters["items"] += len(its)
        counters["status"][status] += 1
        if kind == "eligible":
            counters["gmv_total"] += gmv
            paid_days[user].append((di, ts))
            users_last[user] = di
        orders.append(_Order(len(orders), di, ts, created, paid, user, status, ch, promo_id, its))

    def schedule_repeat(di: int, user: int, first_l1: str) -> bool:
        tdi = di + rng.randint(REPEAT_GAP_MIN, REPEAT_GAP_MAX)
        if rng.random() < REPEAT_SAME_L1:
            l1 = first_l1
        else:
            l1 = stories.CATEGORY_L1[_pick(rng, *_L1_ORDER_CUM)]
        chcum, chtot = _CH_CUM.get(first_l1, _CH_CUM["default"])
        ch = CHANNELS[_pick(rng, chcum, chtot)]
        for off in (0, 1, -1, 2, -2, 3, -3):
            d2 = tdi + off
            if not (di + REPEAT_GAP_MIN <= d2 <= min(di + REPEAT_GAP_MAX, last_di)):
                continue
            key = (d2, l1, ch)
            if len(reserved[key]) < n_elig.get(key, 0):
                reserved[key].append(user)
                return True
        return False

    day_cells = [(l1, ch) for l1 in stories.CATEGORY_L1 for ch in CHANNELS]
    for di, d in enumerate(days):
        if di:
            rebuild_cumw()
        remaining: dict[tuple[str, str], int] = {}
        for l1, ch in day_cells:
            n = n_elig[(di, l1, ch)]
            reps = reserved.pop((di, l1, ch), [])
            if len(reps) > n:
                counters["repeat_giveups"] += len(reps) - n
                reps = reps[:n]
            for u in reps:
                emit_order(di, l1, ch, "eligible", u)
            remaining[(l1, ch)] = n - len(reps)
        # 新客（活动期放大获取）
        tot_rem = sum(remaining.values())
        if tot_rem:
            target = NEW_USER_K * math.exp(-di / NEW_USER_TAU) + NEW_USER_FLOOR
            if act0 <= d < act1:
                target *= new_mult_618
            target = min(round(target), round(NEW_USER_CAP * tot_rem))
            alloc = _largest_remainder(target, [float(remaining[k]) for k in day_cells])
            for (l1, ch), a in zip(day_cells, alloc, strict=True):
                for _ in range(min(a, remaining[(l1, ch)])):
                    users_w.append(math.exp(rng.gauss(0, USER_FREQ_SIGMA)))
                    users_born.append(di)
                    users_last.append(di)
                    u = len(users_w) - 1
                    emit_order(di, l1, ch, "eligible", u)
                    remaining[(l1, ch)] -= 1
                    wk_key = flag_window(d)
                    flag_bal[wk_key] += flag_p[wk_key]
                    if flag_bal[wk_key] >= 1.0:
                        flag_bal[wk_key] -= 1.0
                        if not schedule_repeat(di, u, l1):
                            counters["repeat_giveups"] += 1
        # 有机老客
        for l1, ch in day_cells:
            while remaining[(l1, ch)] > 0:
                u = pick_organic(di)
                if u is None:
                    break
                emit_order(di, l1, ch, "eligible", u)
                remaining[(l1, ch)] -= 1
        # 噪声行：refunded / cancelled / pending
        for l1, ch in day_cells:
            n = n_elig[(di, l1, ch)]
            for kind, rate in (("refunded", RATE_REFUND), ("cancelled", RATE_CANCEL),
                               ("pending", RATE_PENDING)):
                for _ in range(round(n * rate)):
                    emit_order(di, l1, ch, kind, _pick(rng, cumw, cum_total))

    # ---- 定稿：排序、编号、元组化 ----
    orders.sort(key=lambda o: (o.di, o.ts, o.seq))
    order_rows: list[tuple] = []
    item_rows: list[tuple] = []
    for oid, o in enumerate(orders, start=1):
        order_rows.append((oid, o.user + 1, o.status, o.channel, o.promo_id, o.created, o.paid))
        for pid, qty, price in o.items:
            item_rows.append((len(item_rows) + 1, oid, pid, qty, price))

    # ---- 统计（与 self_check 的 SQL 同口径） ----
    def repurchase(ws: date, we: date) -> tuple[int, int]:
        n = rep = 0
        for plist in paid_days.values():
            if not plist:
                continue
            fdi, fts = plist[0]
            if ws <= days[fdi] < we:
                n += 1
                if len(plist) > 1:
                    d2i, t2 = plist[1]
                    if (d2i, t2) > (fdi, fts) and t2 <= fts + timedelta(days=30):
                        rep += 1
        return n, rep

    n_a, r_a = repurchase(*s2["activity_window"])
    n_b, r_b = repurchase(*s2["baseline_window"])
    gmay, gjun = gmv_s1_month[(2026, 5)], gmv_s1_month[(2026, 6)]
    umay, ujun = uv_month_skincare[(2026, 5)], uv_month_skincare[(2026, 6)]
    brand_total: dict[str, float] = defaultdict(float)
    for (_, b), v in gmv_brand_month.items():
        brand_total[b] += v
    top2 = sorted(brand_total.items(), key=lambda kv: -kv[1])[:2]
    monthly_top1: list[tuple[str, str]] = []
    for ym in sorted({k[0] for k in gmv_brand_month}):
        b = max((b for (m, b) in gmv_brand_month if m == ym),
                key=lambda b: gmv_brand_month[(ym, b)])
        monthly_top1.append((f"{ym[0]}-{ym[1]:02d}", b))

    stats = {
        "counts": {
            "orders": len(order_rows), "order_items": len(item_rows),
            "products": len(products), "traffic_daily": len(traffic),
            "promotions": len(stories.PROMOTIONS), "users": len(users_w),
        },
        "status": dict(counters["status"]),
        "gmv_total": counters["gmv_total"],
        "repeat_giveups": counters["repeat_giveups"],
        "story1": {
            "gmv_may": gmay, "gmv_jun": gjun,
            "gmv_decline": 1.0 - gjun / gmay,
            "uv_may": umay, "uv_jun": ujun,
            "uv_decline": 1.0 - ujun / umay,
            "conv_side": (gjun / gmay) / (ujun / umay) - 1.0,
        },
        "story2": {
            "n_activity": n_a, "repeaters_activity": r_a,
            "rate_activity": r_a / n_a if n_a else 0.0,
            "n_baseline": n_b, "repeaters_baseline": r_b,
            "rate_baseline": r_b / n_b if n_b else 0.0,
            "diff_pp": (r_a / n_a if n_a else 0.0) - (r_b / n_b if n_b else 0.0),
        },
        "story3": {
            "top1_brand": top2[0][0], "top1_share": top2[0][1] / counters["gmv_total"],
            "top2_brand": top2[1][0], "top2_share": top2[1][1] / counters["gmv_total"],
            "top1_vs_top2": top2[0][1] / top2[1][1],
            "monthly_top1": monthly_top1,
            "stable_months": sum(1 for _, b in monthly_top1 if b == top2[0][0]),
            "months_total": len(monthly_top1),
        },
    }
    promo_rows = [(p["id"], p["name"], p["cat"], p["start"], p["end"], p["type"])
                  for p in stories.PROMOTIONS]
    product_rows = [(p.pid, p.name, p.l1, p.l2, p.brand, p.list_price) for p in products]
    return Dataset(promo_rows, product_rows, traffic, order_rows, item_rows, stats)


# ---- DDL（字面量来自 stories 常量，用 psycopg.sql 安全组合） ----

_L1_VALS = sql.SQL(",").join(sql.Literal(x) for x in stories.CATEGORY_L1)
_PAIR_VALS = sql.SQL(",").join(
    sql.SQL("({},{})").format(sql.Literal(l1), sql.Literal(l2))
    for l1 in stories.CATEGORY_L1 for l2 in stories.CATEGORY_L2[l1])


def _ddl() -> list[sql.Composed]:
    return [
        sql.SQL("""CREATE TABLE promotions (
            promo_id    int PRIMARY KEY,
            promo_name  text NOT NULL UNIQUE,
            category_l1 text CHECK (category_l1 IN ({l1s})),
            start_date  date NOT NULL,
            end_date    date NOT NULL,
            promo_type  text NOT NULL
                CHECK (promo_type IN ('full_reduction','discount','gift','flash')),
            CHECK (end_date >= start_date)
        )""").format(l1s=_L1_VALS),
        sql.SQL("""CREATE TABLE products (
            product_id   int PRIMARY KEY,
            product_name text NOT NULL,
            category_l1  text NOT NULL CHECK (category_l1 IN ({l1s})),
            category_l2  text NOT NULL,
            brand        text NOT NULL,
            list_price   numeric(10,2) NOT NULL CHECK (list_price > 0),
            CHECK ((category_l1, category_l2) IN ({pairs}))
        )""").format(l1s=_L1_VALS, pairs=_PAIR_VALS),
        sql.SQL("""CREATE TABLE traffic_daily (
            "date"      date NOT NULL,
            category_l1 text NOT NULL CHECK (category_l1 IN ({l1s})),
            channel     text NOT NULL CHECK (channel IN ('app','web','mini','live')),
            pv int NOT NULL CHECK (pv > 0),
            uv int NOT NULL CHECK (uv > 0),
            PRIMARY KEY ("date", category_l1, channel)
        )""").format(l1s=_L1_VALS),
        sql.SQL("""CREATE TABLE orders (
            order_id   bigint PRIMARY KEY,
            user_id    int NOT NULL,
            status     text NOT NULL CHECK (status IN
                       ('pending_payment','paid','shipped','completed','cancelled','refunded')),
            channel    text NOT NULL CHECK (channel IN ('app','web','mini','live')),
            promo_id   int REFERENCES promotions(promo_id),
            created_at timestamp NOT NULL,
            paid_at    timestamp,
            CHECK (paid_at IS NULL OR paid_at >= created_at),
            CHECK ((status IN ('pending_payment','cancelled')) = (paid_at IS NULL))
        )"""),
        sql.SQL("""CREATE TABLE order_items (
            item_id    bigint PRIMARY KEY,
            order_id   bigint NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
            product_id int NOT NULL REFERENCES products(product_id),
            quantity   int NOT NULL CHECK (quantity BETWEEN 1 AND 10),
            unit_price numeric(10,2) NOT NULL CHECK (unit_price >= 0)
        )"""),
        # 运行时只读账号（SPEC §6.3）：重建后的表对 chatbi_ro 可 SELECT
        sql.SQL("GRANT SELECT ON promotions, products, traffic_daily, "
                "orders, order_items TO chatbi_ro"),
    ]


_INDEXES = [
    "CREATE INDEX idx_orders_paid_at ON orders (paid_at)",
    "CREATE INDEX idx_orders_status ON orders (status)",
    "CREATE INDEX idx_orders_channel ON orders (channel)",
    "CREATE INDEX idx_orders_user_paid ON orders (user_id, paid_at)",
    "CREATE INDEX idx_orders_promo ON orders (promo_id)",
    "CREATE INDEX idx_items_order ON order_items (order_id)",
    "CREATE INDEX idx_items_product ON order_items (product_id)",
    "CREATE INDEX idx_products_cat ON products (category_l1, category_l2)",
    "CREATE INDEX idx_products_brand ON products (brand)",
]


def write_dataset(conn: psycopg.Connection, ds: Dataset) -> None:
    """幂等重建：drop 全部表 -> 建表 -> COPY -> 索引 -> ANALYZE（单事务）。"""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS order_items, orders, traffic_daily, "
                        "products, promotions CASCADE")
            for stmt in _ddl():
                cur.execute(stmt)
            with cur.copy("COPY promotions (promo_id, promo_name, category_l1, "
                          "start_date, end_date, promo_type) FROM STDIN") as cp:
                for row in ds.promotions:
                    cp.write_row(row)
            with cur.copy("COPY products (product_id, product_name, category_l1, "
                          "category_l2, brand, list_price) FROM STDIN") as cp:
                for row in ds.products:
                    cp.write_row(row)
            with cur.copy('COPY traffic_daily ("date", category_l1, channel, pv, uv) '
                          "FROM STDIN") as cp:
                for row in ds.traffic:
                    cp.write_row(row)
            with cur.copy("COPY orders (order_id, user_id, status, channel, promo_id, "
                          "created_at, paid_at) FROM STDIN") as cp:
                for row in ds.orders:
                    cp.write_row(row)
            with cur.copy("COPY order_items (item_id, order_id, product_id, quantity, "
                          "unit_price) FROM STDIN") as cp:
                for row in ds.items:
                    cp.write_row(row)
            for stmt in _INDEXES:
                cur.execute(stmt)
            cur.execute("ANALYZE")


# ---- 自检（§5.2 口径；全部参数化查询，只读连接可跑） ----

_GMV_JOIN = ("FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
             "JOIN products p ON p.product_id = oi.product_id "
             "WHERE o.status IN ('paid','shipped','completed')")
_REPURCHASE_SQL = """
WITH paid AS (SELECT user_id, paid_at FROM orders
              WHERE status IN ('paid','shipped','completed')),
f AS (SELECT user_id, MIN(paid_at) AS fp FROM paid GROUP BY user_id),
w AS (SELECT user_id, fp FROM f WHERE fp >= %s AND fp < %s),
r AS (SELECT DISTINCT w.user_id FROM w
      JOIN paid ON paid.user_id = w.user_id
      AND paid.paid_at > w.fp AND paid.paid_at <= w.fp + interval '30 days')
SELECT (SELECT count(*) FROM w), (SELECT count(*) FROM r)"""


def self_check(conn: psycopg.Connection) -> dict:
    def q(stmt: str, *args: object) -> list[tuple]:
        return conn.execute(stmt, args).fetchall()

    s1, s2 = stories.STORY1, stories.STORY2
    counts = q(
        "SELECT (SELECT count(*) FROM orders), (SELECT count(*) FROM order_items),"
        " (SELECT count(*) FROM products), (SELECT count(*) FROM traffic_daily),"
        " (SELECT count(*) FROM promotions), (SELECT count(DISTINCT user_id) FROM orders)")[0]
    status = dict(q("SELECT status, count(*) FROM orders GROUP BY 1"))
    gmv = dict(q(
        "SELECT to_char(o.paid_at, 'YYYY-MM'), SUM(oi.unit_price * oi.quantity) "
        + _GMV_JOIN
        + " AND o.paid_at >= %s AND o.paid_at < %s AND p.category_l1 = %s"
          " AND p.category_l2 = %s GROUP BY 1",
        date(2026, 5, 1), date(2026, 7, 1), s1["l1"], s1["l2"]))
    uv = dict(q(
        'SELECT to_char("date", \'YYYY-MM\'), SUM(uv) FROM traffic_daily '
        "WHERE category_l1 = %s AND %s <= \"date\" AND \"date\" < %s GROUP BY 1",
        s1["l1"], date(2026, 5, 1), date(2026, 7, 1)))
    n_a, r_a = q(_REPURCHASE_SQL, *s2["activity_window"])[0]
    n_b, r_b = q(_REPURCHASE_SQL, *s2["baseline_window"])[0]
    total_gmv = q("SELECT COALESCE(SUM(oi.unit_price * oi.quantity), 0) " + _GMV_JOIN)[0][0]
    top_brands = q("SELECT p.brand, SUM(oi.unit_price * oi.quantity) AS gmv " + _GMV_JOIN
                   + " GROUP BY 1 ORDER BY 2 DESC LIMIT 2")
    monthly = q("SELECT to_char(o.paid_at, 'YYYY-MM'), p.brand, "
                "SUM(oi.unit_price * oi.quantity) " + _GMV_JOIN + " GROUP BY 1, 2")
    best: dict[str, tuple[float, str]] = {}
    for ym, brand, g in monthly:
        if ym not in best or g > best[ym][0]:
            best[ym] = (g, brand)
    monthly_top1 = [(ym, best[ym][1]) for ym in sorted(best)]

    gmay, gjun = float(gmv["2026-05"]), float(gmv["2026-06"])
    umay, ujun = float(uv["2026-05"]), float(uv["2026-06"])
    top1, top2 = top_brands[0], top_brands[1]
    stable = sum(1 for _, b in monthly_top1 if b == top1[0])
    return {
        "counts": dict(zip(("orders", "order_items", "products", "traffic_daily",
                            "promotions", "users"), (int(x) for x in counts), strict=True)),
        "status": status,
        "gmv_total": float(total_gmv),
        "story1": {
            "gmv_may": gmay, "gmv_jun": gjun,
            "gmv_decline": 1.0 - gjun / gmay,
            "uv_may": umay, "uv_jun": ujun,
            "uv_decline": 1.0 - ujun / umay,
            "conv_side": (gjun / gmay) / (ujun / umay) - 1.0,
        },
        "story2": {
            "n_activity": n_a, "repeaters_activity": r_a,
            "rate_activity": r_a / n_a if n_a else 0.0,
            "n_baseline": n_b, "repeaters_baseline": r_b,
            "rate_baseline": r_b / n_b if n_b else 0.0,
            "diff_pp": (r_a / n_a if n_a else 0.0) - (r_b / n_b if n_b else 0.0),
        },
        "story3": {
            "top1_brand": top1[0], "top1_share": float(top1[1]) / float(total_gmv),
            "top2_brand": top2[0], "top2_share": float(top2[1]) / float(total_gmv),
            "top1_vs_top2": float(top1[1]) / float(top2[1]),
            "monthly_top1": monthly_top1,
            "stable_months": stable,
            "months_total": len(monthly_top1),
        },
    }


def _evaluate(st: dict) -> list[tuple[str, str, str, bool]]:
    """检查行 = (名称, 实测值, 目标, 是否通过)。"""
    c, s1, s2, s3 = st["counts"], st["story1"], st["story2"], st["story3"]
    rows: list[tuple[str, str, str, bool]] = []
    for key, tgt in TARGETS.items():
        v = c[key]
        if key in ("products", "traffic_daily", "promotions"):
            rows.append((f"count.{key}", f"{v:,}", f"== {tgt:,}", v == tgt))
        else:
            lo, hi = tgt * (1 - TOL), tgt * (1 + TOL)
            rows.append((f"count.{key}", f"{v:,}", f"{lo:,.0f}~{hi:,.0f}", lo <= v <= hi))
    for name, v, (lo, hi) in (
        ("story1.gmv_decline", s1["gmv_decline"], stories.STORY1["gmv_decline_range"]),
        ("story1.uv_decline", s1["uv_decline"], stories.STORY1["uv_decline_range"]),
        ("story2.rate_activity", s2["rate_activity"], stories.STORY2["rate_activity_range"]),
        ("story2.rate_baseline", s2["rate_baseline"], stories.STORY2["rate_base_range"]),
        ("story2.diff_pp", s2["diff_pp"], stories.STORY2["diff_range"]),
    ):
        rows.append((name, f"{v:.4f}", f"[{lo:.2f}, {hi:.2f}]", lo <= v <= hi))
    rows.append(("story3.top1_vs_top2", f"{s3['top1_vs_top2']:.2f}",
                 f">= {stories.STORY3['min_top1_vs_top2']}",
                 s3["top1_vs_top2"] >= stories.STORY3["min_top1_vs_top2"]))
    rows.append(("story3.stable_months", f"{s3['stable_months']}/{s3['months_total']}",
                 f"全 月 顶 一 致 且 ≥{stories.STORY3['stable_months']} 个月",
                 s3["stable_months"] == s3["months_total"]
                 and s3["months_total"] >= stories.STORY3["stable_months"]))
    return rows


def _report(st: dict) -> tuple[bool, str]:
    rows = _evaluate(st)
    lines = ["=" * 76, "mock 数据自检报告（目标区间见 data/stories.py；口径 SPEC §5.2）", "=" * 76]
    for name, val, tgt, ok in rows:
        lines.append(f"{'PASS' if ok else 'FAIL'}  {name:<22} {val:>14}   目标 {tgt}")
    s1, s2, s3 = st["story1"], st["story2"], st["story3"]
    lines.append("-" * 76)
    lines.append(f"故事1: 5月GMV={s1['gmv_may']:,.0f} 6月GMV={s1['gmv_jun']:,.0f} "
                 f"环比={-s1['gmv_decline']:+.1%}；护肤UV 环比={-s1['uv_decline']:+.1%}；"
                 f"转化侧(单位UV产出) 环比={s1['conv_side']:+.1%}")
    lines.append(f"故事2: 活动期新客={s2['n_activity']} 复购率={s2['rate_activity']:.2%}；"
                 f"基线新客={s2['n_baseline']} 复购率={s2['rate_baseline']:.2%}；"
                 f"差={s2['diff_pp']:+.2%}")
    lines.append(f"故事3: Top1={s3['top1_brand']} 占比={s3['top1_share']:.2%}；"
                 f"Top2={s3['top2_brand']} 占比={s3['top2_share']:.2%}；逐月Top1="
                 + ",".join(b for _, b in s3["monthly_top1"]))
    lines.append(f"其余: 状态分布={st.get('status', {})} "
                 f"复购槽位放弃={st.get('repeat_giveups', '-')} 总GMV={st['gmv_total']:,.0f}")
    return all(r[3] for r in rows), "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ChatBI mock 数据生成器（T2）")
    ap.add_argument("--dsn", help="覆盖 .env 的连接串（默认重建用 PG_DSN，检查用 PG_DSN_RO）")
    ap.add_argument("--seed", type=int, default=stories.SEED)
    ap.add_argument("--check-only", action="store_true", help="不重建，只跑自检")
    args = ap.parse_args(argv)
    load_dotenv()
    dsn = args.dsn or os.environ.get("PG_DSN_RO" if args.check_only else "PG_DSN")
    if not dsn:
        print("缺少 DSN：请在 .env 配置 PG_DSN / PG_DSN_RO，或用 --dsn 指定", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn:
        if args.check_only:
            st = self_check(conn)
        else:
            ds = build(args.seed)
            write_dataset(conn, ds)
            st = self_check(conn)
    ok, text = _report(st)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
