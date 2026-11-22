"""业务逻辑：登记试验、读数录入、换件、报告生成。

所有写操作在单个 BEGIN IMMEDIATE 事务内完成：样件、读数、现行报告
始终对应同一已提交状态；任一步失败即回滚，原有进度保留。
"""
import math

from .anova import two_way_anova

MIN_INSPECTORS, MAX_INSPECTORS = 2, 4
MIN_PARTS, MAX_PARTS = 3, 8
TRIALS = (1, 2)
TOL = 1e-9  # “原值重报”判定容差


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
