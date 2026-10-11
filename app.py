import csv
import fcntl
import io
import json
import logging
import os
import threading
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, render_template, request

from scrm_client import ScrmError
from scrm_sync import sync_from_scrm
from timeout_excel import ExcelImportError, import_excel_file, parse_filename_range, scan_input_dir
from timeout_query import query_chart, query_meta
from timeout_store import connect, replace_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# --- 设计系统（antd-lite.css / sprite.svg，由 design-system/sync.sh 同步到 ds/）---
_DS_DIR = os.path.join(ROOT, "ds")
_ds_cache: dict[str, str] = {}


def _ds(name: str) -> str:
    if name not in _ds_cache:
        with open(os.path.join(_DS_DIR, name), encoding="utf-8") as fh:
            _ds_cache[name] = fh.read()
    return _ds_cache[name]


app.jinja_env.globals["ds_css"] = lambda: _ds("antd-lite.css")
app.jinja_env.globals["ds_sprite"] = lambda: _ds("sprite.svg")
app.jinja_env.globals["ds_cards"] = lambda: _ds("cards.js")

# --- m-hero SSO (Casdoor): inert unless MHERO_AUTH[_CLEANER]=audit|on is set (see docs/SSO_CASDOOR.md) ---
from mhero_auth.flask_adapter import init_app as _mhero_auth_init
_mhero_auth_init(app, "cleaner", default_mode="off")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
DATA_FILE = os.path.join(ROOT, "data.json")
LOCK_FILE = os.path.join(ROOT, ".sync.lock")
SHANGHAI = ZoneInfo("Asia/Shanghai")
CUTOFF_DATE = datetime(2026, 5, 1, tzinfo=SHANGHAI)
_SYNC_LOCK = threading.Lock()


def _acquire_sync_lock():
    if not _SYNC_LOCK.acquire(blocking=False):
        raise ScrmError("正在同步，请稍候")
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        _SYNC_LOCK.release()
        raise ScrmError("正在同步，请稍候")
    return lock_fh


def _release_sync_lock(lock_fh):
    fcntl.flock(lock_fh, fcntl.LOCK_UN)
    lock_fh.close()
    _SYNC_LOCK.release()


def save_data(stats, summary):
    data = {
        "updated_at": datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
        "stats": stats,
        "summary": summary,
    }
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def _save_group_context(result: dict) -> None:
    conn = connect(os.path.join(ROOT, "timeout.db"))
    try:
        replace_context(
            conn,
            robot_chat_ids=result.get("robot_chat_ids") or [],
            employee_ids=result.get("employee_ids") or [],
            group_rows=result.get("all_rows") or [],
        )
        conn.commit()
    finally:
        conn.close()


def _run_group_and_timeout():
    result = sync_from_scrm(ROOT)
    stats = compute_stats(result["all_rows"], result["filter_rows"])
    summary = result["summary"]
    try:
        _save_group_context(result)
    except Exception:
        logging.exception("保存群-门店映射失败，拉群覆盖数据仍会保存")
    save_data(stats, summary)
    return summary


def run_sync():
    lock_fh = _acquire_sync_lock()
    try:
        return _run_group_and_timeout()
    finally:
        _release_sync_lock(lock_fh)


def run_timeout_sync():
    lock_fh = _acquire_sync_lock()
    try:
        return scan_input_dir(ROOT)
    finally:
        _release_sync_lock(lock_fh)


def load_data():
    if not os.path.exists(DATA_FILE):
        return None
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_ct(row):
    ct = row.get("create_time")
    if not ct:
        return None
    if isinstance(ct, datetime):
        if ct.tzinfo is None:
            return ct.replace(tzinfo=SHANGHAI)
        return ct
    try:
        dt = datetime.fromisoformat(str(ct))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI)
    return dt


def build_monthly_stats(rows):
    months = {}
    for r in rows:
        ct = parse_ct(r)
        if ct is None:
            continue
        month_key = ct.strftime("%Y-%m")
        if month_key not in months:
            months[month_key] = {"count": 0, "stores": {}}
        months[month_key]["count"] += 1
        store = r["store"]
        months[month_key]["stores"][store] = months[month_key]["stores"].get(store, 0) + 1
    return months


def compute_stats(all_rows, filter_rows):
    all_valid = [r for r in all_rows if r.get("store")]
    filter_valid = [r for r in filter_rows if r.get("store")]

    def store_counts(rows):
        stats = {}
        for r in rows:
            store = r["store"]
            stats[store] = stats.get(store, 0) + 1
        return dict(sorted(stats.items(), key=lambda x: -x[1]))

    all_store_stats = store_counts(all_valid)
    filter_store_stats = store_counts(filter_valid)
    all_after = [r for r in all_valid if parse_ct(r) and parse_ct(r) >= CUTOFF_DATE]
    filter_after = [r for r in filter_valid if parse_ct(r) and parse_ct(r) >= CUTOFF_DATE]

    all_months_raw = build_monthly_stats(all_valid)
    filter_months_raw = build_monthly_stats(filter_valid)
    all_keys = sorted(set(all_months_raw) | set(filter_months_raw))
    years = sorted({m.split("-")[0] for m in all_keys})

    monthly_data = {}
    for month in all_keys:
        all_m = all_months_raw.get(month, {})
        filter_m = filter_months_raw.get(month, {})
        monthly_data[month] = {
            "all": {
                "group_count": all_m.get("count", 0),
                "store_count": len(all_m.get("stores", {})),
                "store_stats": dict(sorted(all_m.get("stores", {}).items(), key=lambda x: -x[1])),
            },
            "filter": {
                "group_count": filter_m.get("count", 0),
                "store_count": len(filter_m.get("stores", {})),
                "store_stats": dict(sorted(filter_m.get("stores", {}).items(), key=lambda x: -x[1])),
            },
        }

    return {
        "national": {
            "all": {
                "group_count": len(all_valid),
                "store_count": len(all_store_stats),
                "store_stats": all_store_stats,
            },
            "filter": {
                "group_count": len(filter_valid),
                "store_count": len(filter_store_stats),
                "store_stats": filter_store_stats,
            },
        },
        "after_may1": {
            "all": {
                "group_count": len(all_after),
                "store_count": len(store_counts(all_after)),
                "store_stats": store_counts(all_after),
            },
            "filter": {
                "group_count": len(filter_after),
                "store_count": len(store_counts(filter_after)),
                "store_stats": store_counts(filter_after),
            },
        },
        "monthly": {
            "months": all_keys,
            "years": years,
            "data": monthly_data,
        },
    }


@app.route("/")
def index():
    data = load_data()
    return render_template("index.html", has_data=data is not None, data=data)


@app.route("/timeout")
def timeout_page():
    data = load_data()
    return render_template("timeout.html", has_data=data is not None, data=data)


@app.route("/parse")
def parse_gone():
    return redirect("/", code=301)


@app.route("/api/export")
def export_csv():
    scope = request.args.get("scope", "national")
    dataset = request.args.get("dataset", "all")
    data = load_data()
    if not data:
        return jsonify({"error": "暂无数据，请先从 SCRM 同步"}), 400

    store_stats = data["stats"].get(scope, {}).get(dataset, {}).get("store_stats", {})
    if not store_stats:
        return jsonify({"error": "没有可导出的数据"}), 400

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["排名", "门店名称", "群数量"])
    for idx, (store, count) in enumerate(store_stats.items(), 1):
        writer.writerow([idx, store, count])

    filename_map = {
        ("national", "all"): "全国全量各店群数量",
        ("national", "filter"): "全国已拉机器人各店群数量",
        ("after_may1", "all"): "5月1日后全量各店群数量",
        ("after_may1", "filter"): "5月1日后已拉机器人各店群数量",
    }
    filename = filename_map.get((scope, dataset), "各店群数量") + ".csv"
    return Response(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
    )


@app.route("/api/export-all")
def export_all_csv():
    data = load_data()
    if not data:
        return jsonify({"error": "暂无数据，请先从 SCRM 同步"}), 400

    all_stats = data["stats"]["national"]["all"]["store_stats"]
    filter_stats = data["stats"]["national"]["filter"]["store_stats"]
    all_stores = set(all_stats) | set(filter_stats)

    rows = []
    for store in all_stores:
        all_count = all_stats.get(store, 0)
        filter_count = filter_stats.get(store, 0)
        remaining = all_count - filter_count
        rate = f"{filter_count / all_count * 100:.1f}%" if all_count else "-"
        rows.append((store, all_count, filter_count, rate, remaining))
    rows.sort(key=lambda x: x[4], reverse=True)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["序号", "店名", "全量群数", "拉机器人群数", "拉群率", "剩余待拉群数"])
    for idx, (store, all_count, filter_count, rate, remaining) in enumerate(rows, 1):
        writer.writerow([idx, store, all_count, filter_count, rate, remaining])

    return Response(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote("门店群数量总表.csv")},
    )


@app.route("/api/timeout/meta")
def timeout_meta_api():
    if request.args.get("scan") == "1":
        try:
            scan_input_dir(ROOT)
        except Exception:
            logging.exception("扫描 input_file 失败")
    return jsonify(query_meta(ROOT))


@app.route("/api/timeout/chart")
def timeout_chart_api():
    payload = query_chart(
        ROOT,
        scope=request.args.get("scope") or "national",
        ident=request.args.get("id") or "",
        grain=request.args.get("grain") or "month",
        start=request.args.get("start"),
        end=request.args.get("end"),
    )
    return jsonify(payload)


@app.route("/api/timeout/import", methods=["POST"])
def timeout_import_api():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "请选择管家导出的 Excel"}), 400
    filename = os.path.basename(uploaded.filename)
    if not filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"error": "只支持 .xlsx"}), 400
    try:
        parse_filename_range(filename)
    except ExcelImportError as exc:
        return jsonify({"error": str(exc)}), 400
    dest_dir = os.path.join(ROOT, "input_file")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)
    uploaded.save(dest)
    try:
        result = import_excel_file(ROOT, dest, force=True)
    except ExcelImportError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/timeout/import-scan", methods=["POST"])
def timeout_import_scan_api():
    return jsonify(scan_input_dir(ROOT, force=bool(request.args.get("force"))))


@app.route("/api/timeout/export")
def timeout_export_csv():
    payload = query_chart(
        ROOT,
        scope=request.args.get("scope") or "national",
        ident=request.args.get("id") or "",
        grain=request.args.get("grain") or "month",
        start=request.args.get("start"),
        end=request.args.get("end"),
    )
    rules = payload.get("totals", {}).get("rules") or []
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["排名", "门店", "战区", "计入消息数", "触发群数", "规则触发次数"]
    for rule in rules:
        header.append((rule.get("rule_name") or rule.get("rule_id")) + "次数")
    writer.writerow(header)
    for idx, row in enumerate(payload.get("stores") or [], 1):
        line = [
            idx,
            row.get("store") or "",
            row.get("theater") or "",
            row.get("messages") or 0,
            row.get("rooms") or 0,
            row.get("triggers") or 0,
        ]
        by_rule = row.get("by_rule") or {}
        for rule in rules:
            cell = by_rule.get(rule.get("rule_id")) or {}
            line.append(cell.get("triggers") or 0)
        writer.writerow(line)
    filename = "超时提醒门店统计.csv"
    return Response(
        output.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
