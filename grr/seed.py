"""演示数据：3 名检验员 × 5 件样件，读数齐全并生成现行报告。

置环境变量 DEMO_SEED=1 时，启动若发现库为空则载入，
便于在网页直接演示“换件复测”流程。
"""
from . import service
from .db import connect, init_db

DEMO_INSPECTORS = ["张三", "李四", "王五"]
DEMO_PARTS = ["P-101", "P-102", "P-103", "P-104", "P-105"]


def demo_value(position, insp_idx, trial):
    """确定性的演示读数（毫米），含样件主效应、检验员偏移与微小噪声。"""
    base = 10.0 + 0.5 * (position - 1)
    op = 0.03 * insp_idx
    noise = ((position * 7 + insp_idx * 3 + trial * 11) % 5 - 2) * 0.004
    return round(base + op + 0.02 * (trial - 1) + noise, 3)


def seed_demo(db_path):
    """库为空时载入演示试验；返回是否载入。"""
    init_db(db_path)
    conn = connect(db_path)
    try:
        if service.get_study(conn) is not None:
            return False
        sid = service.create_study(
            conn, "演示试验：量具 GRR（换件复测）", DEMO_INSPECTORS, DEMO_PARTS
        )
        parts = service.list_parts(conn, sid, "active")
        inspectors = service.list_inspectors(conn, sid)
        entries = [
            (p["id"], i["id"], t, str(demo_value(p["position"], k, t)))
            for p in parts
            for k, i in enumerate(inspectors)
            for t in service.TRIALS
        ]
        service.submit_readings(conn, sid, entries)
        service.ensure_current_report(conn, sid)
        return True
    finally:
        conn.close()
