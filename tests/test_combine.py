"""两地合算：摘要导出/校验、共用样件匹配、合算数值与两地明细合并一致。"""
import io
import json
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

from grr import service
from grr.anova import two_way_anova
from grr.app import create_app

# 两个车间各自的确定性读数（毫米）
def val_a(pos, k, t):
    return round(10.0 + 0.5 * (pos - 1) + 0.03 * k + 0.02 * (t - 1)
                 + ((pos * 7 + k * 3 + t * 11) % 5 - 2) * 0.004, 3)


def val_b(pos, k, t):
    return round(10.1 + 0.5 * (pos - 1) + 0.05 * k + 0.015 * (t - 1)
                 + ((pos * 5 + k * 7 + t * 13) % 5 - 2) * 0.004, 3)


A_INSPECTORS = ("张三", "李四", "王五")
A_PARTS = ("P-101", "P-102", "P-103", "P-104", "P-105")
B_INSPECTORS = ("赵六", "钱七")
# 对方车间位置 3 换过样件（P-103B ≠ P-103），其余编号一致
B_PARTS = ("P-101", "P-102", "P-103B", "P-104", "P-105")
SHARED_POS = (1, 2, 4, 5)


def db_rows(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def make_client(tmp_path, name):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / name)})
    return app.test_client()


def make_study(client, inspectors, parts, name="合算试验"):
    data = MultiDict(
        [("name", name)]
        + [("inspector", n) for n in inspectors]
        + [("part", n) for n in parts]
    )
    return client.post("/study", data=data, follow_redirects=True)


def ids(client):
    db_path = client.application.config["DATABASE"]
    parts = {
        r["position"]: r["id"]
        for r in db_rows(db_path, "SELECT id, position FROM part WHERE status='active'")
    }
    insps = {
        r["name"]: r["id"]
        for r in db_rows(db_path, "SELECT id, name FROM inspector")
    }
    return parts, insps


def fill(client, entries):
    """entries: {(position, inspector_name, trial): value}"""
    parts, insps = ids(client)
    data = {}
    for (pos, iname, trial), value in entries.items():
        data[f"r_{parts[pos]}_{insps[iname]}_{trial}"] = str(value)
    return client.post("/readings", data=data, follow_redirects=True)


def setup_workshop(client, inspectors, parts, valfn, name):
    make_study(client, inspectors, parts, name)
    fill(client, {
        (pos, iname, t): valfn(pos, k, t)
        for pos in range(1, len(parts) + 1)
        for k, iname in enumerate(inspectors)
        for t in (1, 2)
    })
    r = client.get("/report")
    assert "带交互项的双因素方差分析" in r.get_data(as_text=True)


@pytest.fixture()
def workshops(tmp_path):
    """两个车间各自完成试验并生成现行报告；返回 (A客户端, B客户端, B摘要)。"""
    ca = make_client(tmp_path, "a.db")
    cb = make_client(tmp_path, "b.db")
    setup_workshop(ca, A_INSPECTORS, A_PARTS, val_a, "车间A试验")
    setup_workshop(cb, B_INSPECTORS, B_PARTS, val_b, "车间B试验")
    r = cb.get("/report/1/summary.json")
    assert r.status_code == 200
    return ca, cb, json.loads(r.get_data(as_text=True))


def post_combine(client, report_id, payload, filename="peer.json"):
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload).encode("utf-8")
    return client.post(
        f"/report/{report_id}/combine",
        data={"summary": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def combine_rows(client):
    return db_rows(client.application.config["DATABASE"], "SELECT * FROM combine")


def expected_pooled():
    """把两地对应明细放在一起做现有方差分析（仅共用样件，检验员分别计数）。"""
    cells = {}
    for k, pos in enumerate(SHARED_POS):
        for j in range(len(A_INSPECTORS)):
            cells[(k, j)] = [val_a(pos, j, 1), val_a(pos, j, 2)]
        for j in range(len(B_INSPECTORS)):
            cells[(k, len(A_INSPECTORS) + j)] = [val_b(pos, j, 1), val_b(pos, j, 2)]
    return two_way_anova(cells, len(SHARED_POS),
                         len(A_INSPECTORS) + len(B_INSPECTORS), 2)


# ---------- 摘要导出 ----------

def test_summary_export_structure_and_privacy(workshops):
    ca, cb, summary = workshops
    assert summary["format"] == service.SUMMARY_FORMAT
    assert summary["version"] == service.SUMMARY_VERSION
    assert summary["n_trials"] == 2 and summary["n_inspectors"] == 2
    assert [p["position"] for p in summary["parts"]] == [1, 2, 3, 4, 5]
    assert [p["part_no"] for p in summary["parts"]] == list(B_PARTS)
    for p in summary["parts"]:
        assert [c["slot"] for c in p["cells"]] == [1, 2]
        for c in p["cells"]:
            assert c["n"] == 2
            assert set(c) == {"slot", "n", "sum", "sumsq"}
    raw = json.dumps(summary, ensure_ascii=False)
    # 不含检验员姓名、逐人统计与逐次读数
    for name in B_INSPECTORS:
        assert name not in raw
    for leaked in ('"inspector"', '"name"', '"trial"', '"value"'):
        assert leaked not in raw


def test_summary_download_404_for_unknown_report(workshops):
    ca, _, _ = workshops
    assert ca.get("/report/99/summary.json").status_code == 404


# ---------- 合算数值 ----------

def test_combine_matches_pooled_anova(workshops):
    ca, _, summary = workshops
    r = post_combine(ca, 1, summary)
    body = r.get_data(as_text=True)
    assert "合算完成" in body
    assert "两地合算结果" in body
    # 参与合算的样件：位置与编号都相同的 4 件（换过的 P-103/P-103B 不纳入）
    for pno in ("P-101", "P-102", "P-104", "P-105"):
        assert pno in body
    assert "P-103" not in body.replace("P-103B", "")
    assert "4 件共用样件" in body and "3+2 名检验员" in body
    # 数值与两地明细合并做现有方差分析一致
    exp = expected_pooled()
    rows = combine_rows(ca)
    assert len(rows) == 1
    res = json.loads(rows[0]["result_json"])
    for key in ("ss_part", "ss_inspector", "ss_interaction", "ss_error", "ss_total",
                "ms_part", "ms_inspector", "ms_interaction", "ms_error",
                "var_repeatability", "var_inspector", "var_interaction", "var_part"):
        assert res[key] == pytest.approx(exp[key], abs=1e-9), key
    assert res["n_parts"] == 4 and res["n_inspectors"] == 5 and res["n_trials"] == 2
    # 参与样件清单与四个方差分量都在页面上
    parts = json.loads(rows[0]["parts_json"])
    assert [(p["position"], p["part_no"]) for p in parts] == [
        (1, "P-101"), (2, "P-102"), (4, "P-104"), (5, "P-105")]
    for label in ("重复测量（重复性）", "检验员（再现性）", "样件×检验员交互", "样件"):
        assert label in body


def test_combine_keeps_original_report(workshops):
    ca, _, summary = workshops
    db_path = ca.application.config["DATABASE"]
    before = db_rows(db_path, "SELECT * FROM report WHERE id=1")[0]
    post_combine(ca, 1, summary)
    after = db_rows(db_path, "SELECT * FROM report WHERE id=1")[0]
    assert dict(before) == dict(after)  # 原报告保留不变
    rows = combine_rows(ca)
    assert rows[0]["report_id"] == 1
    assert rows[0]["n_insp_local"] == 3 and rows[0]["n_insp_peer"] == 2
    # 报告详情页列出合算记录
    body = ca.get("/report/1").get_data(as_text=True)
    assert "本报告的合算记录" in body and "车间B试验" in body


def test_combine_on_history_report(workshops):
    ca, _, summary = workshops
    # 本车间也换件：位置 3 换 P-103X，重测后报告 #1 转历史
    ca.post("/parts/replace", data={"position": "3", "part_no": "P-103X"},
            follow_redirects=True)
    fill(ca, {(3, iname, t): val_a(3, k, t)
              for k, iname in enumerate(A_INSPECTORS) for t in (1, 2)})
    ca.get("/report")
    db_path = ca.application.config["DATABASE"]
    states = [r["state"] for r in db_rows(db_path, "SELECT state FROM report ORDER BY id")]
    assert states == ["history", "current"]
    # 历史报告 #1 仍可合算（与 B 共用 4 件）
    r = post_combine(ca, 1, summary)
    assert "合算完成" in r.get_data(as_text=True)
    rows = combine_rows(ca)
    assert len(rows) == 1 and rows[0]["report_id"] == 1
    res = json.loads(rows[0]["result_json"])
    exp = expected_pooled()
    assert res["var_repeatability"] == pytest.approx(exp["var_repeatability"], abs=1e-9)


# ---------- 失败情形：提示原因并保留原报告 ----------

def test_combine_rejects_self_summary(workshops):
    ca, _, _ = workshops
    own = json.loads(ca.get("/report/1/summary.json").get_data(as_text=True))
    r = post_combine(ca, 1, own)
    assert "请上传另一车间的摘要" in r.get_data(as_text=True)
    assert combine_rows(ca) == []


def test_combine_rejects_fewer_than_three_shared(workshops, tmp_path):
    ca, _, _ = workshops
    # 第三个车间只有 2 件样件编号一致
    cc = make_client(tmp_path, "c.db")
    setup_workshop(cc, ("甲", "乙"), ("P-101", "P-102", "Q-3", "Q-4", "Q-5"),
                   val_b, "车间C试验")
    summary = json.loads(cc.get("/report/1/summary.json").get_data(as_text=True))
    r = post_combine(ca, 1, summary)
    body = r.get_data(as_text=True)
    assert "仅 2 件" in body and "不足 3 件" in body
    assert combine_rows(ca) == []
    # 原报告保留
    db_path = ca.application.config["DATABASE"]
    assert len(db_rows(db_path, "SELECT id FROM report")) == 1


def test_combine_rejects_garbage_and_empty(workshops):
    ca, _, _ = workshops
    r = post_combine(ca, 1, b"{not json", "bad.json")
    assert "不是有效的 JSON" in r.get_data(as_text=True)
    r = ca.post("/report/1/combine", data={}, follow_redirects=True)
    assert "请选择另一车间的摘要文件" in r.get_data(as_text=True)
    assert combine_rows(ca) == []


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda d: d.update(format="other"), "格式标识不符"),
        (lambda d: d.update(version=99), "版本不支持"),
        (lambda d: d.update(n_trials=3), "测次数须为 2"),
        (lambda d: d.update(n_inspectors=1), "检验员人数"),
        (lambda d: d["parts"][0]["cells"].pop(), "汇总格数与检验员人数不符"),
        (lambda d: d["parts"][0]["cells"][1].update(slot=1), "槽位无效或重复"),
        (lambda d: d["parts"][0]["cells"][0].update(n=1), "汇总次数与测次数不符"),
        (lambda d: d["parts"][0]["cells"][0].update(sumsq=0.0), "平方和与和不匹配"),
        (lambda d: d["parts"][0]["cells"][0].update(sum=float("nan")), "不是有效数值"),
        (lambda d: d["parts"].append(d["parts"][0]), "重复出现"),
    ],
)
def test_combine_rejects_invalid_summaries(workshops, mutate, msg):
    ca, _, summary = workshops
    mutate(summary)
    r = post_combine(ca, 1, summary)
    body = r.get_data(as_text=True)
    assert "合算未进行" in body and msg in body
    assert combine_rows(ca) == []
    # 原报告数值未被触动
    db_path = ca.application.config["DATABASE"]
    rep = db_rows(db_path, "SELECT * FROM report WHERE id=1")[0]
    assert rep["state"] == "current" and rep["n_parts"] == 5


def test_combine_detail_404_for_unknown(workshops):
    ca, _, _ = workshops
    assert ca.get("/combine/99").status_code == 404
