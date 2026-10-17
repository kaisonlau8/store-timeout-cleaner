"""超时评分页面。路由都挂在现有 Flask 应用上，沿用同一套登录。"""

from __future__ import annotations

from urllib.parse import quote

from flask import Flask, Response, jsonify, render_template, request

from list_filters import row_visible
from score_export import stores_to_csv, stores_to_xlsx
from score_llm import llm_configured, maybe_autostart_review, review_status, start_review_job
from score_query import build_report, parse_range, store_detail
from date_ranges import install_date_ranges


def _view_report(report: dict, theater: str | None, query: str | None) -> dict:
    """只筛列表，不重算分档。战区选项来自评分行上的 theater（chat_store 多数票）。"""
    theater = (theater or "").strip()
    query = (query or "").strip()
    stores = list(report.get("stores") or [])
    theaters = sorted({(row.get("theater") or "").strip() for row in stores if (row.get("theater") or "").strip()})
    if theater and theater not in theaters:
        theaters = sorted(set(theaters) | {theater})
    shown = [
        row
        for row in stores
        if row_visible(
            {"name": row.get("name") or "", "code": row.get("code") or "", "theater": row.get("theater") or ""},
            theater,
            query,
        )
    ]
    view = dict(report)
    view["stores"] = shown
    view["theaters"] = theaters
    view["theater"] = theater
    view["q"] = query
    view["filtered"] = bool(theater or query)
    view["matched"] = len(shown)
    return view


def _kick_review(root: str, start, end, meta: dict) -> dict:
    return maybe_autostart_review(
        root,
        start,
        end,
        pending=meta.get("llm_pending"),
        cached=meta.get("llm_cached"),
        source="page",
    )


def _with_auto_review(root: str, start, end, build):
    payload = build()
    gap = payload.get("llm_available") and payload.get("llm_cached", 0) < payload.get("llm_pending", 0)
    status = _kick_review(root, start, end, payload)
    if gap and status.get("status") in {"done", "error"}:
        payload = build()
    payload["llm_job"] = status
    return payload


def register_score(app: Flask, root: str) -> None:
    install_date_ranges(app)
    @app.route("/score")
    def score_page():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = _with_auto_review(root, start, end, lambda: build_report(root, start, end))
        report = _view_report(report, request.args.get("theater"), request.args.get("q"))
        return render_template("score.html", report=report)

    @app.route("/score/store")
    def score_store_page():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        name = (request.args.get("name") or "").strip()
        detail = (
            _with_auto_review(root, start, end, lambda: store_detail(root, name, start, end))
            if name
            else None
        )
        return render_template(
            "score_detail.html",
            detail=detail,
            start=start.isoformat(),
            end=end.isoformat(),
        )

    @app.route("/api/score/export.csv")
    def score_export_csv():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = _view_report(
            build_report(root, start, end),
            request.args.get("theater"),
            request.args.get("q"),
        )
        filename = f"门店超时评分_{report['start']}_{report['end']}.csv"
        return Response(
            stores_to_csv(report["stores"]),
            mimetype="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
        )

    @app.route("/api/score/export.xlsx")
    def score_export_xlsx():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = _view_report(
            build_report(root, start, end),
            request.args.get("theater"),
            request.args.get("q"),
        )
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
