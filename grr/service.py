"""业务逻辑：登记试验、读数录入、换件、报告生成、两地合算。

所有写操作在单个 BEGIN IMMEDIATE 事务内完成：样件、读数、现行报告
始终对应同一已提交状态；任一步失败即回滚，原有进度保留。
"""
import json
import math

from .anova import two_way_anova, two_way_anova_agg

MIN_INSPECTORS, MAX_INSPECTORS = 2, 4
MIN_PARTS, MAX_PARTS = 3, 8
TRIALS = (1, 2)
TOL = 1e-9  # “原值重报”判定容差

# 两地合算：摘要格式标识与版本、参与合算的最少共用样件数
SUMMARY_FORMAT = "grr-summary"
SUMMARY_VERSION = 1
MIN_SHARED_PARTS = 3


class ServiceError(ValueError):
    """可展示给用户的业务错误。"""


# ---------- 查询 ----------

def get_study(conn):
    return conn.execute("SELECT * FROM study ORDER BY id LIMIT 1").fetchone()


def list_inspectors(conn, study_id):
    return conn.execute(
        "SELECT * FROM inspector WHERE study_id=? ORDER BY id", (study_id,)
    ).fetchall()


def list_parts(conn, study_id, status="active"):
    return conn.execute(
        "SELECT * FROM part WHERE study_id=? AND status=? ORDER BY position",
        (study_id, status),
    ).fetchall()


def readings_map(conn, study_id):
    """现行样件的读数：{(part_id, inspector_id, trial): value}"""
    rows = conn.execute(
        """SELECT r.part_id, r.inspector_id, r.trial, r.value
           FROM reading r JOIN part p ON p.id = r.part_id
           WHERE p.study_id=? AND p.status='active'""",
        (study_id,),
    ).fetchall()
    return {(r["part_id"], r["inspector_id"], r["trial"]): r["value"] for r in rows}


def missing_items(conn, study_id):
    """待补项：现行样件 × 检验员 × 测次中缺测的格子。"""
    return conn.execute(
        """SELECT p.position, p.part_no, i.name AS inspector, t.trial
           FROM part p
           CROSS JOIN inspector i
           CROSS JOIN (SELECT 1 AS trial UNION ALL SELECT 2) t
           LEFT JOIN reading r
                  ON r.part_id=p.id AND r.inspector_id=i.id AND r.trial=t.trial
           WHERE p.study_id=? AND p.status='active' AND i.study_id=? AND r.id IS NULL
           ORDER BY p.position, i.id, t.trial""",
        (study_id, study_id),
    ).fetchall()


def replaced_with_readings(conn, study_id):
    """已更换下线的样件及其读数（备查）。"""
    parts = list_parts(conn, study_id, "replaced")
    out = []
    for p in parts:
        rows = conn.execute(
            """SELECT i.name AS inspector, r.trial, r.value
               FROM reading r JOIN inspector i ON i.id = r.inspector_id
               WHERE r.part_id=? ORDER BY i.id, r.trial""",
            (p["id"],),
        ).fetchall()
        out.append({"part": p, "readings": rows})
    return out


def current_report(conn, study_id):
    return conn.execute(
        "SELECT * FROM report WHERE study_id=? AND state='current'", (study_id,)
    ).fetchone()


def get_report(conn, report_id):
    return conn.execute("SELECT * FROM report WHERE id=?", (report_id,)).fetchone()


def list_reports(conn, study_id):
    return conn.execute(
        "SELECT * FROM report WHERE study_id=? ORDER BY id DESC", (study_id,)
    ).fetchall()


def report_parts(conn, report_id):
    return conn.execute(
        """SELECT p.* FROM report_part rp JOIN part p ON p.id=rp.part_id
           WHERE rp.report_id=? ORDER BY p.position""",
        (report_id,),
    ).fetchall()


def report_readings(conn, report_id):
    """报告生成时所用样件的读数（历史报告仍可查到所用读数）。"""
    return conn.execute(
        """SELECT p.position, p.part_no, p.status, i.name AS inspector, r.trial, r.value
           FROM report_part rp
           JOIN part p      ON p.id = rp.part_id
           JOIN reading r   ON r.part_id = p.id
           JOIN inspector i ON i.id = r.inspector_id
           WHERE rp.report_id=?
           ORDER BY p.position, i.id, r.trial""",
        (report_id,),
    ).fetchall()


# ---------- 写操作 ----------

def create_study(conn, name, inspector_names, part_numbers):
    if get_study(conn):
        raise ServiceError("已存在一场试验；本系统一次只保存一场试验。")
    inspectors = [n.strip() for n in inspector_names if n and n.strip()]
    parts = [n.strip() for n in part_numbers if n and n.strip()]
    if not MIN_INSPECTORS <= len(inspectors) <= MAX_INSPECTORS:
        raise ServiceError(f"检验员须 {MIN_INSPECTORS}～{MAX_INSPECTORS} 名。")
    if not MIN_PARTS <= len(parts) <= MAX_PARTS:
        raise ServiceError(f"样件须 {MIN_PARTS}～{MAX_PARTS} 件。")
    if len(set(inspectors)) != len(inspectors):
        raise ServiceError("检验员姓名重复。")
    if len(set(parts)) != len(parts):
        raise ServiceError("样件编号重复。")
    if any(len(n) > 40 for n in inspectors + parts):
        raise ServiceError("姓名/编号过长（≤40 字符）。")
    name = (name or "").strip() or "量具重复性与再现性试验"

    conn.execute("BEGIN IMMEDIATE")
    try:
        sid = conn.execute("INSERT INTO study (name) VALUES (?)", (name,)).lastrowid
        for n in inspectors:
            conn.execute(
                "INSERT INTO inspector (study_id, name) VALUES (?,?)", (sid, n)
            )
        for pos, pno in enumerate(parts, 1):
            conn.execute(
                "INSERT INTO part (study_id, position, part_no) VALUES (?,?,?)",
                (sid, pos, pno),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return sid


def submit_readings(conn, study_id, entries):
    """批量录入读数。entries: [(part_id, inspector_id, trial, raw_value), ...]

    同一测次原值重报只记一次（skipped）；异值退回（errors）；
    已更换样件的迟到读数拒收。合法新读数一次事务提交，
    提交失败整体回滚，原有进度保留。
    返回 (saved, skipped, errors)。
    """
    saved, skipped, errors = 0, 0, []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for part_id, inspector_id, trial, raw in entries:
            part = conn.execute(
                "SELECT * FROM part WHERE id=? AND study_id=?", (part_id, study_id)
            ).fetchone()
            insp = conn.execute(
                "SELECT * FROM inspector WHERE id=? AND study_id=?",
                (inspector_id, study_id),
            ).fetchone()
            label = f"样件#{part_id} / 检验员#{inspector_id} / 第{trial}测次"
            if part is None or insp is None or trial not in TRIALS:
                errors.append(f"{label}：不属于本场试验，已退回。")
                continue
            label = f"位置{part['position']}（{part['part_no']}）/ {insp['name']} / 第{trial}测次"
            if part["status"] != "active":
                errors.append(f"{label}：该样件已更换下线，迟到读数拒收。")
                continue
            try:
                value = float(str(raw).strip())
                if not math.isfinite(value):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{label}：'{raw}' 不是有效毫米读数，已退回。")
                continue
            existing = conn.execute(
                "SELECT value FROM reading WHERE part_id=? AND inspector_id=? AND trial=?",
                (part_id, inspector_id, trial),
            ).fetchone()
            if existing is not None:
                if abs(existing["value"] - value) <= TOL:
                    skipped += 1  # 原值重报，只记一次
                else:
                    errors.append(
                        f"{label}：已存在读数 {existing['value']:g}，异值 {value:g} 退回。"
                    )
                continue
            conn.execute(
                "INSERT INTO reading (part_id, inspector_id, trial, value) VALUES (?,?,?,?)",
                (part_id, inspector_id, trial, value),
            )
            saved += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return saved, skipped, errors


def replace_part(conn, study_id, position, new_no):
    """原位置换入新编号：旧件下线、新件上岗、现行报告转历史，同一事务。"""
    new_no = (new_no or "").strip()
    old = conn.execute(
        "SELECT * FROM part WHERE study_id=? AND position=? AND status='active'",
        (study_id, position),
    ).fetchone()
    if old is None:
        raise ServiceError(f"位置 {position} 没有现行样件。")
    if not new_no:
        raise ServiceError("新编号不能为空。")
    if len(new_no) > 40:
        raise ServiceError("新编号过长（≤40 字符）。")
    if new_no == old["part_no"]:
        raise ServiceError("新编号须与旧编号不同。")

    conn.execute("BEGIN IMMEDIATE")
    try:
        dup = conn.execute(
            "SELECT 1 FROM part WHERE study_id=? AND position=? AND part_no=?",
            (study_id, position, new_no),
        ).fetchone()
        if dup:
            raise ServiceError(f"编号 {new_no} 在位置 {position} 已使用过。")
        conn.execute(
            "UPDATE part SET status='replaced', replaced_at=datetime('now') WHERE id=?",
            (old["id"],),
        )
        conn.execute(
            "INSERT INTO part (study_id, position, part_no) VALUES (?,?,?)",
            (study_id, position, new_no),
        )
        # 旧报告标为历史（读数保留，仍可查）
        conn.execute(
            "UPDATE report SET state='history' WHERE study_id=? AND state='current'",
            (study_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return old["part_no"], new_no


def ensure_current_report(conn, study_id):
    """读数齐全时保证存在与现行样件集合一致的现行报告。

    返回 (report_row, generated)；读数未齐返回 (None, False)。
    生成过程单事务：失败回滚，读数进度不受影响。
    """
    if missing_items(conn, study_id):
        return None, False
    parts = list_parts(conn, study_id, "active")
    inspectors = list_inspectors(conn, study_id)
    active_ids = sorted(p["id"] for p in parts)

    cur = current_report(conn, study_id)
    if cur is not None:
        used = sorted(
            r["part_id"]
            for r in conn.execute(
                "SELECT part_id FROM report_part WHERE report_id=?", (cur["id"],)
            ).fetchall()
        )
        if used == active_ids:
            return cur, False

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE report SET state='history' WHERE study_id=? AND state='current'",
            (study_id,),
        )
        rows = conn.execute(
            """SELECT r.part_id, r.inspector_id, r.trial, r.value
               FROM reading r JOIN part p ON p.id = r.part_id
               WHERE p.study_id=? AND p.status='active'
               ORDER BY r.part_id, r.inspector_id, r.trial""",
            (study_id,),
        ).fetchall()
        pmap = {p["id"]: k for k, p in enumerate(parts)}
        imap = {i["id"]: k for k, i in enumerate(inspectors)}
        cells = {}
        for r in rows:
            cells.setdefault((pmap[r["part_id"]], imap[r["inspector_id"]]), []).append(
                r["value"]
            )
        res = two_way_anova(cells, len(parts), len(inspectors), len(TRIALS))
        rid = conn.execute(
            """INSERT INTO report
               (study_id, state, n_parts, n_inspectors, n_trials,
                ss_part, ss_inspector, ss_interaction, ss_error, ss_total,
                ms_part, ms_inspector, ms_interaction, ms_error,
                var_repeatability, var_inspector, var_interaction, var_part)
               VALUES (?, 'current', ?, ?, ?,
                       ?, ?, ?, ?, ?,
                       ?, ?, ?, ?,
                       ?, ?, ?, ?)""",
            (
                study_id,
                res["n_parts"], res["n_inspectors"], res["n_trials"],
                res["ss_part"], res["ss_inspector"], res["ss_interaction"],
                res["ss_error"], res["ss_total"],
                res["ms_part"], res["ms_inspector"], res["ms_interaction"],
                res["ms_error"],
                res["var_repeatability"], res["var_inspector"],
                res["var_interaction"], res["var_part"],
            ),
        ).lastrowid
        for p in parts:
            conn.execute(
                "INSERT INTO report_part (report_id, part_id) VALUES (?,?)",
                (rid, p["id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_report(conn, rid), True


# ---------- 两地合算 ----------

def build_summary(conn, report_id):
    """由报告生成统计摘要：只含样件位置、编号与每格（样件×检验员槽位）的
    次数/和/平方和；不含检验员姓名、逐人统计与逐次读数。"""
    rep = get_report(conn, report_id)
    if rep is None:
        raise ServiceError("报告不存在。")
    study = conn.execute(
        "SELECT * FROM study WHERE id=?", (rep["study_id"],)
    ).fetchone()
    rows = conn.execute(
        """SELECT p.position, p.part_no, r.inspector_id, r.value
           FROM report_part rp
           JOIN part p    ON p.id = rp.part_id
           JOIN reading r ON r.part_id = p.id
           WHERE rp.report_id=?
           ORDER BY p.position, r.inspector_id, r.trial""",
        (report_id,),
    ).fetchall()
    insp_ids = sorted({r["inspector_id"] for r in rows})
    if len(insp_ids) != rep["n_inspectors"]:
        raise ServiceError("报告读数与检验员人数不符，无法导出摘要。")
    slot = {iid: k + 1 for k, iid in enumerate(insp_ids)}  # 匿名槽位，不含姓名
    parts = {}
    for r in rows:
        cells = parts.setdefault((r["position"], r["part_no"]), {})
        s = slot[r["inspector_id"]]
        n, total, sq = cells.get(s, (0, 0.0, 0.0))
        cells[s] = (n + 1, total + r["value"], sq + r["value"] * r["value"])
    out_parts = [
        {
            "position": position,
            "part_no": part_no,
            "cells": [
                {"slot": s, "n": n, "sum": total, "sumsq": sq}
                for s, (n, total, sq) in sorted(cells.items())
            ],
        }
        for (position, part_no), cells in sorted(parts.items())
    ]
    return {
        "format": SUMMARY_FORMAT,
        "version": SUMMARY_VERSION,
        "study": study["name"] if study else "",
        "report_id": rep["id"],
        "report_created_at": rep["created_at"],
        "n_trials": rep["n_trials"],
        "n_inspectors": rep["n_inspectors"],
        "parts": out_parts,
    }


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_summary(data):
    """校验另一车间上传的摘要并规范化；任何问题都抛 ServiceError 说明原因。"""
    if not isinstance(data, dict):
        raise ServiceError("摘要不是有效的 JSON 对象。")
    if data.get("format") != SUMMARY_FORMAT:
        raise ServiceError(f"摘要格式标识不符（须为 {SUMMARY_FORMAT}）。")
    if data.get("version") != SUMMARY_VERSION:
        raise ServiceError(f"摘要版本不支持（须为 {SUMMARY_VERSION}）。")
    if data.get("n_trials") != len(TRIALS):
        raise ServiceError(f"摘要测次数须为 {len(TRIALS)}。")
    n_insp = data.get("n_inspectors")
    if type(n_insp) is not int or not MIN_INSPECTORS <= n_insp <= MAX_INSPECTORS:
        raise ServiceError(f"摘要检验员人数须为 {MIN_INSPECTORS}～{MAX_INSPECTORS} 的整数。")
    parts = data.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ServiceError("摘要缺少样件数据。")
    out_parts, seen_pos = [], set()
    for p in parts:
        if not isinstance(p, dict):
            raise ServiceError("摘要样件条目无效。")
        pos, pno = p.get("position"), p.get("part_no")
        if type(pos) is not int or pos < 1:
            raise ServiceError("摘要样件位置无效。")
        if not isinstance(pno, str) or not pno.strip() or len(pno.strip()) > 80:
            raise ServiceError("摘要样件编号无效。")
        pno = pno.strip()
        if pos in seen_pos:
            raise ServiceError(f"摘要中位置 {pos} 重复出现。")
        seen_pos.add(pos)
        cells = p.get("cells")
        if not isinstance(cells, list) or len(cells) != n_insp:
            raise ServiceError(f"位置 {pos} 的汇总格数与检验员人数不符。")
        out_cells, seen_slots = [], set()
        for c in cells:
            if not isinstance(c, dict):
                raise ServiceError("摘要汇总格无效。")
            s = c.get("slot")
            if type(s) is not int or not 1 <= s <= n_insp or s in seen_slots:
                raise ServiceError(f"位置 {pos} 的检验员槽位无效或重复。")
            seen_slots.add(s)
            if c.get("n") != len(TRIALS):
                raise ServiceError(f"位置 {pos} 的汇总次数与测次数不符。")
            total, sq = c.get("sum"), c.get("sumsq")
            if not _is_num(total) or not _is_num(sq):
                raise ServiceError(f"位置 {pos} 的汇总量不是有效数值。")
            total, sq = float(total), float(sq)
            if sq < total * total / len(TRIALS) - 1e-6 * max(1.0, total * total):
                raise ServiceError(f"位置 {pos} 的平方和与和不匹配，摘要无效。")
            out_cells.append({"slot": s, "n": len(TRIALS), "sum": total, "sumsq": sq})
        out_parts.append(
            {"position": pos, "part_no": pno,
             "cells": sorted(out_cells, key=lambda c: c["slot"])}
        )
    name = data.get("study")
    label = name.strip()[:80] if isinstance(name, str) and name.strip() else "另一车间"
    if isinstance(data.get("report_id"), int):
        label = f"{label}·报告#{data['report_id']}"
    return {
        "n_trials": len(TRIALS),
        "n_inspectors": n_insp,
        "parts": sorted(out_parts, key=lambda p: p["position"]),
        "label": label,
        "report_id": data.get("report_id"),
        "report_created_at": data.get("report_created_at"),
    }


def combine_report(conn, report_id, remote):
    """本方报告与另一车间摘要合算：只纳入位置与编号都相同的样件，
    两地检验员分别计数。成功时写入 combine 记录（单事务），原报告不动。
    共用样件不足三件时抛 ServiceError，不写入任何数据。返回合算记录 id。"""
    rep = get_report(conn, report_id)
    if rep is None:
        raise ServiceError("报告不存在。")
    local = build_summary(conn, report_id)
    if (remote["n_inspectors"] == local["n_inspectors"]
            and remote["parts"] == local["parts"]):
        raise ServiceError("该摘要与本报告内容完全相同（疑为本报告导出），请上传另一车间的摘要。")
    local_map = {(p["position"], p["part_no"]): p for p in local["parts"]}
    shared = [
        (lp, rp)
        for rp in remote["parts"]
        if (lp := local_map.get((rp["position"], rp["part_no"]))) is not None
    ]
    shared.sort(key=lambda pair: pair[0]["position"])
    if len(shared) < MIN_SHARED_PARTS:
        raise ServiceError(
            f"位置与编号都相同的样件仅 {len(shared)} 件，"
            f"不足 {MIN_SHARED_PARTS} 件，无法合算。"
        )
    b_local, b_peer = local["n_inspectors"], remote["n_inspectors"]
    aggs = {}
    for k, (lp, rp) in enumerate(shared):
        for c in lp["cells"]:
            aggs[(k, c["slot"] - 1)] = (c["n"], c["sum"], c["sumsq"])
        for c in rp["cells"]:
            aggs[(k, b_local + c["slot"] - 1)] = (c["n"], c["sum"], c["sumsq"])
    res = two_way_anova_agg(aggs, len(shared), b_local + b_peer, local["n_trials"])
    parts_json = json.dumps(
        [{"position": lp["position"], "part_no": lp["part_no"]} for lp, _ in shared],
        ensure_ascii=False,
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        cid = conn.execute(
            """INSERT INTO combine
               (report_id, peer_label, n_parts, n_insp_local, n_insp_peer,
                n_trials, result_json, parts_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                rep["id"], remote["label"], len(shared), b_local, b_peer,
                local["n_trials"], json.dumps(res), parts_json,
            ),
        ).lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cid


def get_combine(conn, combine_id):
    return conn.execute(
        """SELECT c.*, r.study_id AS study_id FROM combine c
           JOIN report r ON r.id = c.report_id WHERE c.id=?""",
        (combine_id,),
    ).fetchone()


def list_combines(conn, report_id):
    return conn.execute(
        "SELECT * FROM combine WHERE report_id=? ORDER BY id DESC", (report_id,)
    ).fetchall()
