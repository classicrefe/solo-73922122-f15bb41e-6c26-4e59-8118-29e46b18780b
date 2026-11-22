"""Flask 路由：试验台、读数录入、换件、报告与历史。"""
import os
import sqlite3

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, url_for,
)

from . import service
from .db import close_db, get_db, init_db


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "grr-dev-key"),
        DATABASE=os.environ.get(
            "DATABASE", os.path.join(app.instance_path, "grr.db")
        ),
    )
    if config:
        app.config.update(config)
    os.makedirs(os.path.dirname(app.config["DATABASE"]) or ".", exist_ok=True)
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    @app.route("/")
    def index():
        db = get_db()
        study = service.get_study(db)
        if study is None:
            return render_template(
                "index.html", study=None,
                min_i=service.MIN_INSPECTORS, max_i=service.MAX_INSPECTORS,
                min_p=service.MIN_PARTS, max_p=service.MAX_PARTS,
            )
        sid = study["id"]
        return render_template(
            "index.html",
            study=study,
            inspectors=service.list_inspectors(db, sid),
            parts=service.list_parts(db, sid, "active"),
            replaced=service.replaced_with_readings(db, sid),
            rmap=service.readings_map(db, sid),
            missing=service.missing_items(db, sid),
            current=service.current_report(db, sid),
            trials=service.TRIALS,
        )

    @app.route("/study", methods=["POST"])
    def create_study():
        db = get_db()
        try:
            service.create_study(
                db,
                request.form.get("name", ""),
                request.form.getlist("inspector"),
                request.form.getlist("part"),
            )
            flash("试验登记成功，请录入读数。", "ok")
        except service.ServiceError as e:
            flash(str(e), "error")
        except sqlite3.Error:
            flash("登记失败，未写入任何数据。", "error")
        return redirect(url_for("index"))

    @app.route("/readings", methods=["POST"])
    def submit_readings():
        db = get_db()
        study = service.get_study(db)
        if study is None:
            flash("请先登记试验。", "error")
            return redirect(url_for("index"))
        entries = []
        for key, raw in request.form.items():
            if not key.startswith("r_"):
                continue
            try:
                _, pid, iid, trial = key.split("_")
                entries.append((int(pid), int(iid), int(trial), raw))
            except ValueError:
                continue
        try:
            saved, skipped, errors = service.submit_readings(db, study["id"], entries)
            if saved:
                flash(f"已记录 {saved} 个读数。", "ok")
            if skipped:
                flash(f"{skipped} 个原值重报，只记一次，未重复写入。", "ok")
            for e in errors:
                flash(e, "error")
            if not saved and not skipped and not errors:
                flash("没有可提交的读数。", "error")
        except (service.ServiceError, sqlite3.Error):
            flash("提交失败，已保留原有进度。", "error")
        return redirect(url_for("index"))

    @app.route("/parts/replace", methods=["POST"])
    def replace_part():
        db = get_db()
        study = service.get_study(db)
        if study is None:
            flash("请先登记试验。", "error")
            return redirect(url_for("index"))
        try:
            position = int(request.form.get("position", "0"))
        except ValueError:
            position = 0
        try:
            old_no, new_no = service.replace_part(
                db, study["id"], position, request.form.get("part_no", "")
            )
            flash(
                f"位置 {position}：{old_no} 已更换为 {new_no}；"
                f"旧件读数保留备查，只需重测新件。",
                "ok",
            )
        except service.ServiceError as e:
            flash(str(e), "error")
        except sqlite3.Error:
            flash("换件失败，已保留原进度。", "error")
        return redirect(url_for("index"))

    @app.route("/report")
    def report():
        db = get_db()
        study = service.get_study(db)
        if study is None:
            flash("请先登记试验。", "error")
            return redirect(url_for("index"))
        missing = service.missing_items(db, study["id"])
        if missing:
            return render_template(
                "report.html", study=study, pending=True, missing=missing
            )
        try:
            rep, generated = service.ensure_current_report(db, study["id"])
        except Exception:
            flash("报告生成失败，读数进度已保留，可重试。", "error")
            return redirect(url_for("index"))
        if generated:
            flash("读数齐全，已生成现行报告。", "ok")
        return render_template(
            "report.html",
            study=study,
            pending=False,
            report=rep,
            parts=service.report_parts(db, rep["id"]),
            readings=service.report_readings(db, rep["id"]),
            components=_components(rep),
        )

    @app.route("/report/<int:report_id>")
    def report_detail(report_id):
        db = get_db()
        study = service.get_study(db)
        rep = service.get_report(db, report_id)
        if rep is None or study is None or rep["study_id"] != study["id"]:
            abort(404)
        return render_template(
            "report_detail.html",
            study=study,
            report=rep,
            parts=service.report_parts(db, rep["id"]),
            readings=service.report_readings(db, rep["id"]),
            components=_components(rep),
        )

    @app.route("/history")
    def history():
        db = get_db()
        study = service.get_study(db)
        if study is None:
            flash("请先登记试验。", "error")
            return redirect(url_for("index"))
        return render_template(
            "history.html", study=study, reports=service.list_reports(db, study["id"])
        )

    return app


def _components(rep):
    rows = [
        ("重复测量（重复性）", rep["var_repeatability"]),
        ("检验员（再现性）", rep["var_inspector"]),
        ("样件×检验员交互", rep["var_interaction"]),
        ("样件", rep["var_part"]),
    ]
    total = sum(v for _, v in rows)
    return [
        {
            "name": name,
            "var": v,
            "std": v ** 0.5,
            "pct": (100.0 * v / total) if total > 0 else 0.0,
        }
        for name, v in rows
    ]
