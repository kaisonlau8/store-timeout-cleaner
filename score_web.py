"""超时评分页面。路由都挂在现有 Flask 应用上，沿用同一套登录。"""

from __future__ import annotations

from urllib.parse import quote

from flask import Flask, Response, render_template, request

from score_export import stores_to_csv, stores_to_xlsx
from score_query import build_report, parse_range, store_detail


def register_score(app: Flask, root: str) -> None:
    @app.route("/score")
    def score_page():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = build_report(root, start, end)
        return render_template("score.html", report=report)

    @app.route("/score/store")
    def score_store_page():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        name = (request.args.get("name") or "").strip()
        detail = store_detail(root, name, start, end) if name else None
        return render_template("score_detail.html", detail=detail, start=start.isoformat(), end=end.isoformat())

    @app.route("/api/score/export.csv")
    def score_export_csv():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = build_report(root, start, end)
        filename = f"门店超时评分_{report['start']}_{report['end']}.csv"
        return Response(
            stores_to_csv(report["stores"]),
            mimetype="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
        )

    @app.route("/api/score/export.xlsx")
    def score_export_xlsx():
        start, end = parse_range(request.args.get("start"), request.args.get("end"))
        report = build_report(root, start, end)
        filename = f"门店超时评分_{report['start']}_{report['end']}.xlsx"
        return Response(
            stores_to_xlsx(report["stores"]),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
        )
