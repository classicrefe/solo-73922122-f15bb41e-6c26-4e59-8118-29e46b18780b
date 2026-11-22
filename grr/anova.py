"""带交互项的双因素方差分析（平衡设计：a 样件 × b 检验员 × n 次重复）。

方差分量按随机效应模型估计，负分量按零计：
  重复测量(重复性)  var_rep  = MS_error
  交互(样件×检验员) var_int  = max(0, (MS_interaction - MS_error) / n)
  检验员            var_insp = max(0, (MS_inspector - MS_interaction) / (a*n))
  样件              var_part = max(0, (MS_part - MS_interaction) / (b*n))
"""


def two_way_anova(cells, n_parts, n_inspectors, n_trials=2):
    """cells: {(part_idx, inspector_idx): [读数, ...]}，必须齐全（每格 n_trials 个）。"""
    a, b, n = n_parts, n_inspectors, n_trials
    if a < 2 or b < 2 or n < 2:
        raise ValueError("样件、检验员、测次均须不少于 2")
    for i in range(a):
        for j in range(b):
            vals = cells.get((i, j))
            if not vals or len(vals) != n:
                raise ValueError("读数未齐全，不能做方差分析")

    cell_mean = {(i, j): sum(cells[(i, j)]) / n for i in range(a) for j in range(b)}
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
    ss_err = sum(
        (v - cell_mean[(i, j)]) ** 2
        for (i, j), vals in cells.items()
        for v in vals
    )
    ss_tot = sum((v - grand) ** 2 for vals in cells.values() for v in vals)

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
