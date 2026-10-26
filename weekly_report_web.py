"""周报列表/下载，共用 m-hero SSO；关闭/audit 模式下也不泄露报表。"""

from pathlib import Path

from flask import abort, g, render_template, send_from_directory

from weekly_report import REPORT_DIR, REPORT_FILENAME_RE


def register_weekly_reports(app):
    app.config.setdefault("WEEKLY_REPORT_DIR", str(REPORT_DIR))

    def directory():
        if getattr(g, "mhero_session", None) is None:
            abort(401)
        return Path(app.config["WEEKLY_REPORT_DIR"]).resolve()

    @app.get("/weekly-reports")
    def weekly_reports():
        root = directory()
        files = sorted((p.name for p in root.glob("*_weekly.xlsx")
                        if REPORT_FILENAME_RE.fullmatch(p.name) and p.is_file()
                        and not p.is_symlink()), reverse=True)
        return render_template("weekly_reports.html", files=files), {"Cache-Control": "private, no-store"}

    @app.get("/weekly-reports/<path:filename>")
    def weekly_report_download(filename):
        root = directory()
        if not REPORT_FILENAME_RE.fullmatch(filename):
            abort(404)
        path = root / filename
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            abort(404)
        response = send_from_directory(root, filename, as_attachment=True)
        response.headers["Cache-Control"] = "private, no-store"
        return response
