"""关键流程自测：登记 → 录入 → 方差分析 → 换件复测 → 历史报告 → 失败回滚。"""
import sqlite3

import pytest
from werkzeug.datastructures import MultiDict

from grr import service
from grr.anova import two_way_anova
from grr.app import create_app
from grr.seed import seed_demo

# 手工核算过的数据集：3 件样件 × 2 名检验员 × 2 测次
# P1: 甲 10.0/10.2  乙 10.1/10.3
# P2: 甲 11.0/11.2  乙 10.9/11.1
# P3: 甲 12.0/12.2  乙 11.9/12.1
DATASET = {
    (1, "甲"): (10.0, 10.2), (1, "乙"): (10.1, 10.3),
    (2, "甲"): (11.0, 11.2), (2, "乙"): (10.9, 11.1),
    (3, "甲"): (12.0, 12.2), (3, "乙"): (11.9, 12.1),
}
# 期望：SS_part=7.226667, SS_insp=0.003333, SS_int=0.026667, SS_err=0.12
# MS_err=0.02, MS_int=0.013333, MS_insp=0.003333, MS_part=3.613333
# var_rep=0.02；var_int、var_insp 为负→0；var_part=0.9


@pytest.fixture()
def client(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db")})
    return app.test_client()


def db_rows(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def make_study(client, inspectors=("甲", "乙"), parts=("P1", "P2", "P3")):
    data = MultiDict(
        [("name", "测试试验")]
        + [("inspector", n) for n in inspectors]
        + [("part", n) for n in parts]
    )
    return client.post("/study", data=data, follow_redirects=True)


def ids(client):
    """返回 (parts: {position: id}, inspectors: {name: id})，应用的数据库路径。"""
    db_path = client.application.config["DATABASE"]
    parts = {
        r["position"]: r["id"]
        for r in db_rows(db_path, "SELECT id, position FROM part WHERE status='active'")
    }
    insps = {r["name"]: r["id"] for r in db_rows(db_path, "SELECT id, name FROM inspector")}
    return parts, insps


def fill(client, entries):
    """entries: {(position, inspector_name, trial): value}，按当前现行样件提交。"""
    parts, insps = ids(client)
    data = {}
    for (pos, iname, trial), value in entries.items():
        data[f"r_{parts[pos]}_{insps[iname]}_{trial}"] = str(value)
    return client.post("/readings", data=data, follow_redirects=True)


def fill_all(client):
    return fill(client, {(p, i, t): v[t - 1] for (p, i), v in DATASET.items() for t in (1, 2)})


# ---------- 方差分析单元测试 ----------

def test_anova_hand_computed():
    cells = {}
    for (p, _iname), (v1, v2) in DATASET.items():
        j = 0 if _iname == "甲" else 1
        cells[(p - 1, j)] = [v1, v2]
    res = two_way_anova(cells, 3, 2, 2)
    assert res["ss_part"] == pytest.approx(7.226667, abs=1e-5)
    assert res["ss_inspector"] == pytest.approx(0.003333, abs=1e-5)
    assert res["ss_interaction"] == pytest.approx(0.026667, abs=1e-5)
    assert res["ss_error"] == pytest.approx(0.12, abs=1e-9)
    assert res["ms_error"] == pytest.approx(0.02, abs=1e-9)
    assert res["var_repeatability"] == pytest.approx(0.02, abs=1e-9)
    assert res["var_interaction"] == 0.0  # 负分量按零计
    assert res["var_inspector"] == 0.0    # 负分量按零计
    assert res["var_part"] == pytest.approx(0.9, abs=1e-9)


def test_anova_rejects_incomplete():
    with pytest.raises(ValueError):
        two_way_anova({(0, 0): [1.0, 1.1]}, 2, 2, 2)


# ---------- 登记 ----------

def test_create_study_and_single_study(client):
    r = make_study(client)
    assert "试验登记成功" in r.get_data(as_text=True)
    # 第二场试验被拒绝（一次只保存一场）
    r = make_study(client)
    assert "已存在一场试验" in r.get_data(as_text=True)
    db_path = client.application.config["DATABASE"]
    assert len(db_rows(db_path, "SELECT id FROM study")) == 1
    assert len(db_rows(db_path, "SELECT id FROM inspector")) == 2
    assert len(db_rows(db_path, "SELECT id FROM part")) == 3


@pytest.mark.parametrize(
    "inspectors,parts,msg",
    [
        (("甲",), ("P1", "P2", "P3"), "检验员须 2～4 名"),
        (("甲", "乙", "丙", "丁", "戊"), ("P1", "P2", "P3"), "检验员须 2～4 名"),
        (("甲", "乙"), ("P1", "P2"), "样件须 3～8 件"),
        (("甲", "乙"), tuple(f"P{i}" for i in range(9)), "样件须 3～8 件"),
        (("甲", "甲"), ("P1", "P2", "P3"), "检验员姓名重复"),
        (("甲", "乙"), ("P1", "P1", "P2"), "样件编号重复"),
    ],
)
def test_create_study_validation(client, inspectors, parts, msg):
    r = make_study(client, inspectors, parts)
    assert msg in r.get_data(as_text=True)
    db_path = client.application.config["DATABASE"]
    assert len(db_rows(db_path, "SELECT id FROM study")) == 0


# ---------- 读数录入 ----------

def test_reading_idempotent_and_conflict(client):
    make_study(client)
    fill(client, {(1, "甲", 1): 10.0})
    db_path = client.application.config["DATABASE"]
    # 原值重报：只记一次
    r = fill(client, {(1, "甲", 1): 10.0})
    assert "只记一次" in r.get_data(as_text=True)
    rows = db_rows(db_path, "SELECT value FROM reading")
    assert len(rows) == 1 and rows[0]["value"] == 10.0
    # 异值退回：原值不动
    r = fill(client, {(1, "甲", 1): 99.9})
    body = r.get_data(as_text=True)
    assert "异值" in body and "退回" in body
    rows = db_rows(db_path, "SELECT value FROM reading")
    assert len(rows) == 1 and rows[0]["value"] == 10.0


def test_reading_rejects_bad_value(client):
    make_study(client)
    r = fill(client, {(1, "甲", 1): "abc"})
    assert "不是有效毫米读数" in r.get_data(as_text=True)
    db_path = client.application.config["DATABASE"]
    assert len(db_rows(db_path, "SELECT id FROM reading")) == 0


def test_pending_listed_until_complete(client):
    make_study(client)
    # 缺 (3, 乙, 2)
    entries = {(p, i, t): v[t - 1] for (p, i), v in DATASET.items() for t in (1, 2)}
    del entries[(3, "乙", 2)]
    fill(client, entries)
    r = client.get("/report")
    body = r.get_data(as_text=True)
    assert "读数未齐全" in body
    assert "待补项（1 项）" in body
    assert "位置 3（P3） · 乙 · 第 2 测次" in body
    # 补齐后报告生成
    fill(client, {(3, "乙", 2): DATASET[(3, "乙")][1]})
    r = client.get("/report")
    body = r.get_data(as_text=True)
    assert "带交互项的双因素方差分析" in body


# ---------- 报告数值 ----------

def test_report_values_match_hand_calc(client):
    make_study(client)
    fill_all(client)
    r = client.get("/report")
    assert r.status_code == 200
    db_path = client.application.config["DATABASE"]
    rep = db_rows(db_path, "SELECT * FROM report WHERE state='current'")[0]
    assert rep["var_repeatability"] == pytest.approx(0.02, abs=1e-9)
    assert rep["var_interaction"] == 0.0
    assert rep["var_inspector"] == 0.0
    assert rep["var_part"] == pytest.approx(0.9, abs=1e-9)
    assert rep["ss_error"] == pytest.approx(0.12, abs=1e-9)
    # 报告与现行样件集合对应
    used = db_rows(db_path, "SELECT part_id FROM report_part WHERE report_id=?", (rep["id"],))
    active = db_rows(db_path, "SELECT id FROM part WHERE status='active'")
    assert sorted(r["part_id"] for r in used) == sorted(r["id"] for r in active)


# ---------- 换件复测 ----------

def test_replace_and_retest_flow(client):
    make_study(client)
    fill_all(client)
    client.get("/report")  # 生成现行报告
    db_path = client.application.config["DATABASE"]
    old_report = db_rows(db_path, "SELECT id FROM report WHERE state='current'")[0]["id"]
    old_parts, _ = ids(client)
    old_p2 = old_parts[2]

    # 位置 2 磕伤，换入新编号
    r = client.post("/parts/replace", data={"position": "2", "part_no": "P2-新"},
                    follow_redirects=True)
    assert "P2 已更换为 P2-新" in r.get_data(as_text=True)

    # 旧报告转历史，且仍能查到所用读数
    rep = db_rows(db_path, "SELECT state FROM report WHERE id=?", (old_report,))[0]
    assert rep["state"] == "history"
    r = client.get(f"/report/{old_report}")
    body = r.get_data(as_text=True)
    assert "历史" in body and "P2" in body and "11.2" in body

    # 拒收旧件迟到读数
    r = client.post("/readings", data={f"r_{old_p2}_1_1": "11.0"},
                    follow_redirects=True)
    # 上面 inspector id=1 未必存在？存在（甲）。应提示拒收。
    assert "迟到读数拒收" in r.get_data(as_text=True)

    # 其他样件已打开的表单仍可提交（原值重报只记一次）
    parts, insps = ids(client)
    r = client.post(
        "/readings",
        data={f"r_{parts[1]}_{insps['甲']}_1": "10.0"},
        follow_redirects=True,
    )
    assert "只记一次" in r.get_data(as_text=True)

    # 只需重测新件：待补项恰为位置 2 的 2 人 × 2 测次
    r = client.get("/report")
    body = r.get_data(as_text=True)
    assert "待补项（4 项）" in body
    assert "P2-新" in body

    # 重测新件后生成新现行报告
    fill(client, {(2, "甲", 1): 11.05, (2, "甲", 2): 11.15,
                  (2, "乙", 1): 10.95, (2, "乙", 2): 11.05})
    r = client.get("/report")
    assert "带交互项的双因素方差分析" in r.get_data(as_text=True)
    rows = db_rows(db_path, "SELECT state FROM report ORDER BY id")
    assert [r["state"] for r in rows] == ["history", "current"]
    # 新报告对应新样件集合
    new_report = db_rows(db_path, "SELECT id FROM report WHERE state='current'")[0]["id"]
    used = db_rows(db_path, "SELECT part_id FROM report_part WHERE report_id=?", (new_report,))
    active = db_rows(db_path, "SELECT id FROM part WHERE status='active'")
    assert sorted(r["part_id"] for r in used) == sorted(r["id"] for r in active)
    # 历史列表两报告可见
    r = client.get("/history")
    body = r.get_data(as_text=True)
    assert body.count("查看") == 2


def test_replace_while_incomplete_keeps_other_forms_open(client):
    make_study(client)
    # 缺 (3, 乙, 2)，此时换位置 2
    entries = {(p, i, t): v[t - 1] for (p, i), v in DATASET.items() for t in (1, 2)}
    del entries[(3, "乙", 2)]
    fill(client, entries)
    r = client.post("/parts/replace", data={"position": "2", "part_no": "P2-新"},
                    follow_redirects=True)
    assert "已更换" in r.get_data(as_text=True)
    # 其他样件未提交的格子仍可提交
    r = fill(client, {(3, "乙", 2): DATASET[(3, "乙")][1]})
    assert "已记录 1 个读数" in r.get_data(as_text=True)
    # 待补项只剩新件的 4 格
    r = client.get("/report")
    assert "待补项（4 项）" in r.get_data(as_text=True)


def test_replace_validation(client):
    make_study(client)
    # 新旧同号
    r = client.post("/parts/replace", data={"position": "1", "part_no": "P1"},
                    follow_redirects=True)
    assert "新编号须与旧编号不同" in r.get_data(as_text=True)
    # 空编号
    r = client.post("/parts/replace", data={"position": "1", "part_no": "  "},
                    follow_redirects=True)
    assert "新编号不能为空" in r.get_data(as_text=True)
    # 换成功后同位置同编号不可复用
    client.post("/parts/replace", data={"position": "1", "part_no": "P1B"})
    r = client.post("/parts/replace", data={"position": "1", "part_no": "P1"},
                    follow_redirects=True)
    assert "已使用过" in r.get_data(as_text=True)
    # 状态保持一致：仍只有 3 件现行样件，仅第一次换件成功下线 1 件
    db_path = client.application.config["DATABASE"]
    assert len(db_rows(db_path, "SELECT id FROM part WHERE status='active'")) == 3
    assert len(db_rows(db_path, "SELECT id FROM part WHERE status='replaced'")) == 1


# ---------- 事务一致性：提交失败保留原进度 ----------

def test_report_failure_preserves_progress(client, monkeypatch):
    make_study(client)
    fill_all(client)
    db_path = client.application.config["DATABASE"]

    def boom(*a, **k):
        raise RuntimeError("模拟计算故障")

    monkeypatch.setattr(service, "two_way_anova", boom)
    r = client.get("/report", follow_redirects=True)
    assert "报告生成失败" in r.get_data(as_text=True)
    # 读数原样保留，无半成品报告
    assert len(db_rows(db_path, "SELECT id FROM reading")) == 12
    assert len(db_rows(db_path, "SELECT id FROM report")) == 0
    # 故障消除后可正常生成
    monkeypatch.undo()
    r = client.get("/report")
    assert "带交互项的双因素方差分析" in r.get_data(as_text=True)


# ---------- 演示数据 ----------

def test_seed_demo(tmp_path):
    db = str(tmp_path / "demo.db")
    assert seed_demo(db) is True
    assert seed_demo(db) is False  # 幂等
    reps = db_rows(db, "SELECT * FROM report WHERE state='current'")
    assert len(reps) == 1
    assert reps[0]["n_parts"] == 5 and reps[0]["n_inspectors"] == 3
    assert len(db_rows(db, "SELECT id FROM reading")) == 5 * 3 * 2
