"""超时评分页面。路由都挂在现有 Flask 应用上，沿用同一套登录。"""

from __future__ import annotations

from urllib.parse import quote

from flask import Flask, Response, jsonify, render_template, request

from score_export import stores_to_csv, stores_to_xlsx
from score_llm import llm_configured, review_status, start_review_job
from score_query import build_report, parse_range, store_detail


def _use_llm(root: str) -> bool | None:
    if not llm_configured(root):
        return False
    values = request.args.getlist("llm")
    if not values:
        return None
    return values[-1] == "1"


def register_score(app: Flask, root: str) -> None:
    @app.route("/score")
    def score_page():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = build_report(root, start, end, use_llm=_use_llm(root))
        return render_template("score.html", report=report)

    @app.route("/score/store")
    def score_store_page():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        name = (request.args.get("name") or "").strip()
        detail = store_detail(root, name, start, end, use_llm=_use_llm(root)) if name else None
        return render_template(
            "score_detail.html",
            detail=detail,
            start=start.isoformat(),
            end=end.isoformat(),
            llm_on=bool(detail and detail.get("llm_on")),
        )

    @app.route("/api/score/export.csv")
    def score_export_csv():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = build_report(root, start, end, use_llm=_use_llm(root))
        filename = f"门店超时评分_{report['start']}_{report['end']}.csv"
        return Response(
            stores_to_csv(report["stores"]),
            mimetype="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
        )

    @app.route("/api/score/export.xlsx")
    def score_export_xlsx():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = build_report(root, start, end, use_llm=_use_llm(root))
        filename = f"门店超时评分_{report['start']}_{report['end']}.xlsx"
        return Response(
            stores_to_xlsx(report["stores"]),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
        )

    @app.post("/api/score/llm-review")
    def score_llm_review():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        if not llm_configured(root):
            return jsonify({"status": "off", "error": "未配置 DEEPSEEK_API_KEY"}), 400
        force = request.args.get("force") == "1" or request.form.get("force") == "1"
        status = start_review_job(root, start, end, force=force)
        return jsonify(status), 202

    @app.get("/api/score/llm-status")
    def score_llm_status():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        return jsonify(review_status(root, start, end))
