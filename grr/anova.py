"""带交互项的双因素方差分析（平衡设计：a 样件 × b 检验员 × n 次重复）。

方差分量按随机效应模型估计，负分量按零计：
  重复测量(重复性)  var_rep  = MS_error
  交互(样件×检验员) var_int  = max(0, (MS_interaction - MS_error) / n)
  检验员            var_insp = max(0, (MS_inspector - MS_interaction) / (a*n))
  样件              var_part = max(0, (MS_part - MS_interaction) / (b*n))

two_way_anova 接收逐次读数；two_way_anova_agg 接收每格汇总量（次数、和、平方和）。
两条路径共用同一套计算公式，因此“把明细放在一起算”与“用统计摘要合算”结果一致。
"""


def two_way_anova(cells, n_parts, n_inspectors, n_trials=2):
    """cells: {(part_idx, inspector_idx): [读数, ...]}，必须齐全（每格 n_trials 个）。"""
    aggs = {}
    for key, vals in cells.items():
        if not vals or len(vals) != n_trials:
            raise ValueError("读数未齐全，不能做方差分析")
        aggs[key] = (len(vals), sum(vals), sum(v * v for v in vals))
    return two_way_anova_agg(aggs, n_parts, n_inspectors, n_trials)


def two_way_anova_agg(aggs, n_parts, n_inspectors, n_trials=2):
    """aggs: {(part_idx, inspector_idx): (次数, 和, 平方和)}，每格次数须等于 n_trials。"""
    a, b, n = n_parts, n_inspectors, n_trials
    if a < 2 or b < 2 or n < 2:
        raise ValueError("样件、检验员、测次均须不少于 2")
    for i in range(a):
        for j in range(b):
            agg = aggs.get((i, j))
            if agg is None or agg[0] != n:
                raise ValueError("读数未齐全，不能做方差分析")

    cell_mean = {(i, j): aggs[(i, j)][1] / n for i in range(a) for j in range(b)}
    part_mean = [sum(cell_mean[(i, j)] for j in range(b)) / b for i in range(a)]
    insp_mean = [sum(cell_mean[(i, j)] for i in range(a)) / a for j in range(b)]
    grand = sum(part_mean) / a

    ss_part = b * n * sum((m - grand) ** 2 for m in part_mean)
    ss_insp = a * n * sum((m - grand) ** 2 for m in insp_mean)
    ss_cells = n * sum(
        (cell_mean[(i, j)] - grand) ** 2 for i in range(a) for j in range(b)
    )
    ss_int = ss_cells - ss_part - ss_insp
    if ss_int < 0 and ss_int > -1e-9:  # 浮点尾差
        ss_int = 0.0
    # 每格离差平方和 = 平方和 − 和²/次数
    ss_err = sum(agg[2] - agg[1] * agg[1] / n for agg in aggs.values())
    ss_tot = sum(agg[2] for agg in aggs.values()) - a * b * n * grand * grand

    df_part, df_insp = a - 1, b - 1
    df_int = (a - 1) * (b - 1)
    df_err = a * b * (n - 1)

    ms_part = ss_part / df_part
    ms_insp = ss_insp / df_insp
    ms_int = ss_int / df_int
    ms_err = ss_err / df_err

    return {
        "n_parts": a,
        "n_inspectors": b,
        "n_trials": n,
        "ss_part": ss_part,
        "ss_inspector": ss_insp,
        "ss_interaction": ss_int,
        "ss_error": ss_err,
        "ss_total": ss_tot,
        "df_part": df_part,
        "df_inspector": df_insp,
        "df_interaction": df_int,
        "df_error": df_err,
        "df_total": a * b * n - 1,
        "ms_part": ms_part,
        "ms_inspector": ms_insp,
        "ms_interaction": ms_int,
        "ms_error": ms_err,
        # 方差分量，负分量按零计
        "var_repeatability": ms_err,
        "var_interaction": max(0.0, (ms_int - ms_err) / n),
        "var_inspector": max(0.0, (ms_insp - ms_int) / (a * n)),
        "var_part": max(0.0, (ms_part - ms_int) / (b * n)),
    }
