"""SQLite 连接与表结构。所有写路径在 service.py 中以显式事务提交。"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS study (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS inspector (
    id       INTEGER PRIMARY KEY,
    study_id INTEGER NOT NULL REFERENCES study(id),
    name     TEXT NOT NULL,
    UNIQUE (study_id, name)
);

-- 同一位置(position)可多次换件，旧件 status='replaced' 保留读数备查
CREATE TABLE IF NOT EXISTS part (
    id          INTEGER PRIMARY KEY,
    study_id    INTEGER NOT NULL REFERENCES study(id),
    position    INTEGER NOT NULL,
    part_no     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'replaced')),
    replaced_at TEXT,
    UNIQUE (study_id, position, part_no)
);

-- 同一(样件, 检验员, 测次)只记一次，由唯一约束兜底
CREATE TABLE IF NOT EXISTS reading (
    id           INTEGER PRIMARY KEY,
    part_id      INTEGER NOT NULL REFERENCES part(id),
    inspector_id INTEGER NOT NULL REFERENCES inspector(id),
    trial        INTEGER NOT NULL CHECK (trial IN (1, 2)),
    value        REAL NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (part_id, inspector_id, trial)
);

CREATE TABLE IF NOT EXISTS report (
    id              INTEGER PRIMARY KEY,
    study_id        INTEGER NOT NULL REFERENCES study(id),
    state           TEXT NOT NULL DEFAULT 'current' CHECK (state IN ('current', 'history')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    n_parts         INTEGER NOT NULL,
    n_inspectors    INTEGER NOT NULL,
    n_trials        INTEGER NOT NULL,
    ss_part         REAL NOT NULL,
    ss_inspector    REAL NOT NULL,
    ss_interaction  REAL NOT NULL,
    ss_error        REAL NOT NULL,
    ss_total        REAL NOT NULL,
    ms_part         REAL NOT NULL,
    ms_inspector    REAL NOT NULL,
    ms_interaction  REAL NOT NULL,
    ms_error        REAL NOT NULL,
    var_repeatability REAL NOT NULL,
    var_inspector     REAL NOT NULL,
    var_interaction   REAL NOT NULL,
    var_part          REAL NOT NULL
);

-- 报告与当时所用样件集合的对应关系，保证报告/读数/样件同属一次提交状态
CREATE TABLE IF NOT EXISTS report_part (
    report_id INTEGER NOT NULL REFERENCES report(id),
    part_id   INTEGER NOT NULL REFERENCES part(id),
    PRIMARY KEY (report_id, part_id)
);
"""


def connect(path):
    """autocommit 连接；写事务由调用方用 BEGIN IMMEDIATE/COMMIT 显式控制。"""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
