"""T10 测试：评测 harness——四指标判定逻辑用合成数据钉死（T10 ⑥；SPEC §8.2）。

覆盖（与 runner.py 判定口径一一对应）：
- 数值容差：±2% 边界（1.98% 过 / 2.07% 不过）、率类百分比尺度回退与 |expected|<10 守卫；
- 结论词元：%/个百分点 ÷100、万/亿 放大、千分位逗号、Unicode 负号；率类取反候选
  （「下滑 8.7%」= -0.087）；方向 = 方向词 ∨ 数值符号；
- 执行正确率形态：标量 / compare 主命中与 cur-prev 反推 / drilldown 因子全命中 /
  drilldown 多行按 dim 查行 / topn 名次序列（错序即挂）；
- 纠错：steps 首条 outcome 判首轮失败（与 trace sql_result 事件同源）+ answered + 执行正确；
- 一致性：3 采样签名聚类 ≥2（稳定地错也算一致）；题级严格多数（平票判否）；
- run_eval 端到端：合成 4 题 × 3 采样 + 假 analyze_fn + 假 executor → 四指标、
  多数决翻转、报告 markdown 与落盘；
- integration（真实 PG + key，缺失自动 skip）：runner.main 真实跑 5 题子集出报告。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from chatbi.api import AnalysisResult  # noqa: E402
from eval import runner as R  # noqa: E402
from eval.report import build_report, save_report  # noqa: E402

# ---------- 助手 ----------


def _expected(value, direction="none", extras=None, tol=0.02):
    return {"value": value, "direction": direction, "extras": extras or {}, "tolerance": tol}


def _steps(*outcomes: str) -> list[dict]:
    return [{"step_id": 1, "description": "d", "attempt": i + 1, "outcome": o, "error": None}
            for i, o in enumerate(outcomes)]


def _result(status="answered", conclusion="", evidence=None, steps=None, usage=None):
    ev = [{"id": f"E{i + 1}", "description": "d", "sql": s, "fingerprint": f"f{i}",
           "row_count": 1, "rows_preview": [], "ms": 1.0}
          for i, s in enumerate(evidence or [])]
    return AnalysisResult(
        status=status, conclusion=conclusion, evidence=ev,
        steps=steps if steps is not None else _steps("ok"),
        usage=usage or {"llm_calls": 3, "query_calls": 1, "tokens_in": 30, "tokens_out": 15},
        trace_dir=None,
    )


# ---------- 数值容差与词元提取 ----------


def test_values_match_tolerance_and_scale() -> None:
    assert R.values_match(101.99, 100.0)  # 1.99% ≤ 2%
    assert not R.values_match(102.01, 100.0)  # 2.01% > 2%
    assert R.values_match(100, 100)
    assert R.values_match("123.45", 123.45)  # 字符串单元格
    assert R.values_match(8.7, 0.087)  # 率类：百分比尺度回退
    assert R.values_match(-25.4, -0.254)
    assert not R.values_match(5300, 53)  # |expected| ≥ 10 不回退（防计数 53↔5300 误配）
    assert not R.values_match("雪滢", 1)
    assert R.values_match(0, 0)
    assert not R.values_match(5, 0)


def test_extract_numbers_units() -> None:
    tokens = R.extract_numbers("环比下滑 25.4%，GMV 254.87 万；复购差 +6.97 个百分点；−8.7%")
    assert [round(t.value, 6) for t in tokens] == [0.254, 2548700.0, 0.0697, -0.087]
    assert tokens[0].unit == "%" and tokens[1].unit == "万" and tokens[2].unit == "个百分点"


def test_classify_direction() -> None:
    assert R.classify_direction("GMV 环比下滑 25.4%", 0.254) == "down"
    assert R.classify_direction("GMV 增长 8%", 0.08) == "up"
    assert R.classify_direction("变化 -25.4%", -0.254) == "down"  # 无词退符号
    assert R.classify_direction("变化 25.4%", 0.254) == "up"
    assert R.classify_direction("UV 下降而转化上升", 0.05) == "up"  # 两组词同现退符号
    assert R.classify_direction("无数字无方向词", None) == "none"


# ---------- 指标 2：结论正确率 ----------


def test_judge_conclusion_scalar_boundary() -> None:
    exp = _expected(123.45)
    assert R.judge_conclusion("上个月护肤 GMV 为 125.9 元", "answered", exp)[0]  # 1.98%
    ok, detail = R.judge_conclusion("GMV 为 126.0 元", "answered", exp)  # 2.07%
    assert not ok and "偏差" in detail
    assert R.judge_conclusion("GMV 约 254.87 万", "answered", _expected(2548736.59))[0]


def test_judge_conclusion_compare_direction() -> None:
    exp = _expected(-0.2543, direction="down")
    assert R.judge_conclusion("面部精华 GMV 环比下滑 25.43%", "answered", exp)[0]
    assert R.judge_conclusion("环比 -25.43%", "answered", exp)[0]  # 符号判向
    ok, detail = R.judge_conclusion("环比增长 25.43%", "answered", exp)  # 方向错
    assert not ok and "方向" in detail
    assert not R.judge_conclusion("环比下滑 30.1%", "answered", exp)[0]  # 数值超差
    assert not R.judge_conclusion("无法回答", "failed", exp)[0]  # 可解题 failed 不过


def test_judge_conclusion_pp_and_unanswerable() -> None:
    assert R.judge_conclusion("618 使新客复购率提升 6.97 个百分点", "answered",
                              _expected(0.0697, direction="up"))[0]
    exp = _expected(53)
    assert R.judge_conclusion("库无竞对数据，无法对比", "failed", exp, "unanswerable")[0]
    assert R.judge_conclusion("竞对数据缺失，仅能给出局限说明", "partial", exp, "unanswerable")[0]
    assert not R.judge_conclusion("竞对价格 100 元", "answered", exp, "unanswerable")[0]


def test_judge_conclusion_drilldown_factors_required() -> None:
    exp = _expected(-0.2543, direction="down",
                    extras={"uv_change": -0.1552, "yield_change": -0.1173})
    text = "GMV 下滑 25.43%：流量（UV）降 15.52%，单位 UV 产出降 11.73%"
    assert R.judge_conclusion(text, "answered", exp, qtype="drilldown")[0]
    ok, detail = R.judge_conclusion("GMV 下滑 25.43%，流量降 15.52%", "answered", exp,
                                    qtype="drilldown")
    assert not ok and "yield_change" in detail  # 缺因子


def test_judge_conclusion_topn_rows() -> None:
    exp = _expected([["雪滢", 469214.49, 0.109892], ["绯迦", 355477.37, 0.083254]])
    assert R.judge_conclusion("Top2：雪滢 GMV 46.92 万，绯迦 35.55 万", "answered", exp,
                              qtype="topn")[0]
    assert not R.judge_conclusion("Top2：绯迦 35.55 万，雪滢 GMV 46.92 万", "answered", exp,
                                  qtype="topn")[0]  # 顺序错
    assert not R.judge_conclusion("Top 品牌为雪滢", "answered", exp, qtype="topn")[0]  # 缺维度


def test_judge_conclusion_drilldown_multi_rows() -> None:
    exp = _expected([["直播", -0.10, 0.62], ["app", -0.05, 0.31]],
                    direction="down", extras={"overall_change": -0.07})
    assert R.judge_conclusion("整体下滑 7%：直播贡献 -10%，app 贡献 -5%", "answered", exp,
                              qtype="drilldown")[0]
    ok, detail = R.judge_conclusion("直播贡献 -10%，app 贡献 -5%", "answered", exp,
                                    qtype="drilldown")
    assert not ok and "overall_change" in detail  # 缺总变化


# ---------- 指标 1：执行正确率 ----------


def test_sql_match_scalar() -> None:
    exp = _expected(123.45)
    assert R.sql_result_matches("single_metric", exp, [[123.45]], 0.02)[0]
    assert R.sql_result_matches("single_metric", exp, [[1, 123.45]], 0.02)[0]  # 任一格
    assert R.sql_result_matches("single_metric", exp, [[125.9]], 0.02)[0]  # 1.98%
    assert not R.sql_result_matches("single_metric", exp, [[126.0]], 0.02)[0]  # 2.07%
    assert not R.sql_result_matches("single_metric", exp, [], 0.02)[0]
    assert R.sql_result_matches("single_metric", exp, [["123.45"]], 0.02)[0]


def test_sql_match_compare_with_fallback() -> None:
    exp = _expected(-0.0871, extras={"cur": 6650712.33, "prev": 7285455.96})
    assert R.sql_result_matches("compare", exp, [[-0.0871, 6650712.33, 7285455.96]], 0.02)[0]
    ok, why = R.sql_result_matches("compare", exp, [[7285455.96, 6650712.33]], 0.02)
    assert ok and "反推" in why  # 列序颠倒，由 cur/prev 反推
    assert not R.sql_result_matches("compare", exp, [[6650712.33, 6285455.96]], 0.02)[0]


def test_sql_match_drilldown_factor_extras_required() -> None:
    exp = _expected(-0.2543, direction="down",
                    extras={"uv_change": -0.1552, "yield_change": -0.1173})
    assert R.sql_result_matches("drilldown", exp, [[-0.2543, -0.1552, -0.1173]], 0.02)[0]
    assert not R.sql_result_matches("drilldown", exp, [[-0.2543, -0.1552]], 0.02)[0]


def test_sql_match_drilldown_rows_by_dim() -> None:
    exp = _expected([["直播", -0.10, 0.62], ["app", -0.05, 0.31]])
    rows = [["app", -0.05, 0.31], ["直播", -0.10, 0.62]]  # 顺序不同也过（dim 查行）
    assert R.sql_result_matches("drilldown", exp, rows, 0.02)[0]
    assert not R.sql_result_matches("drilldown", exp, [["app", -0.05, 0.31]], 0.02)[0]
    assert not R.sql_result_matches("drilldown", exp,
                                    [["app", -0.05, 0.20], ["直播", -0.10, 0.62]], 0.02)[0]


def test_sql_match_topn_sequence() -> None:
    exp = _expected([["雪滢", 100.0, 0.5], ["绯迦", 90.0, 0.45], ["橘屿", 80.0, 0.4]])
    rows = [["雪滢", 100.0, 0.5], ["绯迦", 90.0, 0.45], ["橘屿", 80.0, 0.4]]
    assert R.sql_result_matches("topn", exp, rows, 0.02)[0]
    swapped = [["绯迦", 90.0, 0.45], ["雪滢", 100.0, 0.5], ["橘屿", 80.0, 0.4]]
    assert not R.sql_result_matches("topn", exp, swapped, 0.02)[0]  # 名次错序
    off = [["雪滢", 100.0, 0.5], ["绯迦", 95.0, 0.45], ["橘屿", 80.0, 0.4]]
    assert not R.sql_result_matches("topn", exp, off, 0.02)[0]  # metric 超 2%


def test_judge_execution_replay_and_no_evidence() -> None:
    exp = _expected(123.45)

    def executor(sql: str):
        if sql == "SELECT good":
            return ["gmv"], [[123.45]]
        raise RuntimeError("boom")

    assert R.judge_execution(["SELECT bad", "SELECT good"], "single_metric", exp, executor)[0]
    ok, detail = R.judge_execution(["SELECT bad"], "single_metric", exp, executor)
    assert not ok and "重放执行失败" in detail
    ok, detail = R.judge_execution([], "single_metric", exp, executor)
    assert not ok and "无成功执行" in detail


# ---------- 指标 3：纠错成功率 ----------


def test_first_round_failed_outcomes() -> None:
    assert not R.first_round_failed([])
    assert not R.first_round_failed(_steps("ok"))
    assert not R.first_round_failed([{"outcome": None}])
    for outcome in R.FIRST_FAIL_OUTCOMES:
        assert R.first_round_failed(_steps(outcome)), outcome
    assert R.first_round_failed(_steps("error", "ok"))
    assert R.is_recovered_run(_result(evidence=["SELECT 1"], steps=_steps("error", "ok")), True)
    assert not R.is_recovered_run(
        _result(evidence=["SELECT 1"], steps=_steps("error", "ok")), False)  # 结果错
    assert not R.is_recovered_run(
        _result(status="partial", steps=_steps("error", "ok")), True)  # 未 answered
    assert not R.is_recovered_run(_result(evidence=["SELECT 1"]), True)  # 首轮即 ok


# ---------- 聚合：多数决与一致性 ----------


def test_majority_strict() -> None:
    assert R.majority([True, True, False])
    assert not R.majority([True, False, False])
    assert R.majority([True])
    assert not R.majority([True, False])  # 平票判否


def test_consistency_clustering() -> None:
    assert R.consistent([(0.254, "down"), (0.25, "down"), (0.30, "up")]) is True  # 前 2 成簇
    assert R.consistent([(0.254, "down"), (0.25, "up"), (0.30, "up")]) is False  # 值/方向均散
    assert R.consistent([(None, "none"), (None, "none"), (5.0, "none")]) is True  # None==None
    assert R.consistent([(None, "none"), (5.0, "none")]) is False
    assert R.consistent([(0.25, "down")]) is None  # 单采样不判


def test_select_questions() -> None:
    qs = [{"id": "sm001", "type": "single_metric"}, {"id": "cmp001", "type": "compare"},
          {"id": "dt001", "type": "dirty"}]
    assert [q["id"] for q in R.select_questions(qs)] == ["sm001", "cmp001", "dt001"]
    assert [q["id"] for q in R.select_questions(qs, types=["dirty"])] == ["dt001"]
    assert [q["id"] for q in R.select_questions(qs, ids=["dt001,sm001"])] == ["sm001", "dt001"]
    assert [q["id"] for q in R.select_questions(qs, limit=2)] == ["sm001", "cmp001"]
    with pytest.raises(ValueError, match="未知题目 id"):
        R.select_questions(qs, ids=["nope"])
    with pytest.raises(ValueError, match="子集为空"):
        R.select_questions(qs, types=["topn"])


# ---------- run_eval 端到端（合成数据集 + 假依赖） ----------


def _q(qid: str, qtype: str, question: str, expected, outcome=None) -> dict:
    return {"id": qid, "type": qtype, "question": question,
            "expected": expected, "expected_outcome": outcome}


def test_run_eval_synthetic_dataset(tmp_path: Path) -> None:
    """4 题 × 3 采样：全对 / 数值抖动（2/3 对）/ 脏题纠错（1 次 partial）/ 多数决翻转。"""
    questions = [
        _q("sm001", "single_metric", "上月护肤 GMV", _expected(123.45)),
        _q("cmp001", "compare", "6 月环比", _expected(-0.0871, direction="down")),
        _q("dt001", "dirty", "6 月退款金额", _expected(212643.17), outcome="answered"),
        _q("sm002", "single_metric", "直播订单数", _expected(2167)),
    ]
    scripts = {
        "上月护肤 GMV": [
            _result(conclusion="上月护肤 GMV 为 123.45 元", evidence=["SELECT sm_ok"]),
            _result(conclusion="上月护肤 GMV 约 999 元", evidence=["SELECT sm_bad"]),
            _result(conclusion="上月护肤 GMV 为 123.45 元", evidence=["SELECT sm_ok"]),
        ],
        "6 月环比": [_result(conclusion="6 月环比下滑 8.71%", evidence=["SELECT cmp"])] * 3,
        "6 月退款金额": [
            _result(conclusion="退款金额合计 212,643.17 元", evidence=["SELECT dt_ok"],
                    steps=_steps("error", "ok")),
            _result(status="partial", conclusion="部分完成：查询超预算", evidence=[]),
            _result(conclusion="退款金额合计 212643.17 元", evidence=["SELECT dt_ok"],
                    steps=_steps("error", "ok")),
        ],
        "直播订单数": [
            _result(conclusion="订单数 2167", evidence=["SELECT n_ok"]),
            _result(conclusion="订单数 3000", evidence=["SELECT n_bad"]),
            _result(conclusion="订单数 3000", evidence=["SELECT n_bad"]),
        ],
    }
    table = {"SELECT sm_ok": [[123.45]], "SELECT sm_bad": [[999.0]],
             "SELECT cmp": [[-0.0871, 6650712.33, 7285455.96]],
             "SELECT dt_ok": [[212643.17]], "SELECT n_ok": [[2167]],
             "SELECT n_bad": [[3000]]}

    question_counters: dict[str, int] = {q: 0 for q in scripts}

    def fake_analyze(question: str) -> AnalysisResult:
        i = question_counters[question]  # 逐采样回放脚本（0/1/2 次）
        question_counters[question] += 1
        return scripts[question][i]

    def executor(sql: str):
        return ["x"], table[sql]

    results = R.run_eval(questions, analyze_fn=fake_analyze, executor=executor, samples=3)
    t = results["totals"]
    assert (t["n"], t["runs"]) == (4, 12)
    assert t["exec_ok"] == 3  # sm001/cmp001/dt001 过，sm002 翻转
    assert t["conclusion_ok"] == 3
    assert (t["dirty_n"], t["recovery"]) == (1, 1)
    assert (t["consistent_judged"], t["consistent"]) == (4, 4)  # sm002 稳定地错也算一致
    assert t["usage"] == {"llm_calls": 36, "query_calls": 12, "tokens_in": 360, "tokens_out": 180}

    q_sm, _q_cmp, q_dt, q_fl = results["questions"]
    assert q_sm["exec_ok"] and q_sm["conclusion_ok"] and q_sm["consistent"] is True
    assert q_dt["runs"][0]["first_round_failed"] and q_dt["recovery"]
    assert not q_fl["exec_ok"] and not q_fl["conclusion_ok"] and q_fl["consistent"] is True

    md = build_report(results)
    for needle in ("执行正确率", "≥80%", "单指标", "纠错脏题", "失败题明细",
                   "Token 与调用消耗", "sm002"):
        assert needle in md
    failure_body = md.split("## 失败题明细", 1)[1].split("## Token", 1)[0]
    assert "sm002" in failure_body and "sm001" not in failure_body

    out = save_report(md, tmp_path / "r.md", payload=results)
    assert out.is_file() and (tmp_path / "r.json").is_file()

    with pytest.raises(ValueError, match="samples"):
        R.run_eval(questions, analyze_fn=fake_analyze, executor=executor, samples=0)


# ---------- integration：真实 PG + 真实 LLM，5 题子集出报告（T10 ⑤） ----------


@pytest.mark.integration
def test_integration_runner_real_subset(tmp_path: Path) -> None:
    from chatbi.llm import LLMClient, LLMConfigError
    from chatbi.tools import run_query as real_run_query

    try:
        res = real_run_query("SELECT count(*) AS n FROM orders")
    except Exception:  # noqa: BLE001 —— 环境不可用一律转 skip
        pytest.skip("本机 PG 不可连（先 brew services 起 PG）")
    if not (res.ok and res.rows[0]["n"] > 1000):
        pytest.skip("库中无 mock 数据（先 uv run python -m data.mock_gen）")
    try:
        LLMClient()
    except LLMConfigError:
        pytest.skip("未配置 ZHIPU_API_KEY，跳过真实冒烟")

    rc = R.main(["--limit", "5", "--samples", "1",
                 "--runs-root", str(tmp_path / "runs"),
                 "--report", str(tmp_path / "r.md")])
    assert rc == 0
    md = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "执行正确率" in md and "失败题明细" in md and "Token 与调用消耗" in md
    assert (tmp_path / "r.json").is_file()  # 完整结构落盘（T11 复查用）
