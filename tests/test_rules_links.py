"""B4 规则管理、超时规则筛选、日期错误页与一级页导航。"""

import html as html_lib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import timeout_algo

ROOT = Path(__file__).resolve().parent.parent
RULE_30 = "2055182790919555459"
RULE_10 = "2035988283787413634"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _seed_rules(tmp_path: Path) -> str:
    db = str(tmp_path / "timeout.db")
    conn = timeout_algo.open_db(str(tmp_path))
    try:
        conn.executemany(
            "INSERT INTO timeout_event("
            "msg_id, rule_id, rule_name, room_id, store, theater, created_at, room_name, content, exclude_reason"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("m30", RULE_30, "30 分钟未回复预警", "r1", "甲店", "华东战区", "2026-07-10 10:00:00", "甲店群", "在吗", ""),
                ("m10", RULE_10, "10 分钟未回复预警", "r1", "甲店", "华东战区", "2026-07-10 11:00:00", "甲店群", "有人吗", ""),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    (tmp_path / "data.json").write_text(
        json.dumps({"updated_at": "2026-10-15 00:00:00", "summary": {}, "stats": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return db


@pytest.fixture
def rules_env(tmp_path, monkeypatch):
    db = _seed_rules(tmp_path)
    monkeypatch.setenv("CLEANER_DB_PATH", db)
    monkeypatch.setenv("CLEANER_DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setenv("MHERO_AUTH", "off")
    monkeypatch.setenv("MHERO_AUTH_CLEANER", "off")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    import app as app_module

    return app_module.app.test_client(), db


def test_rule_rows_include_timeout_trigger_link():
    source = _read("static/js/admin-rules.js")
    assert "查看触发记录" in source
    assert "function ruleLogHref(ruleId)" in source
    assert "'/timeout?'" in source
    assert "params.set('rule_id'" in source
    assert "params.set('start', monthStart())" in source
    assert "params.set('end', today())" in source
    assert "data-rule-log" in source


def test_timeout_page_and_api_filter_by_rule_id(rules_env):
    client, _db = rules_env
    page = client.get(
        "/timeout", query_string={"rule_id": RULE_30, "start": "2026-07-01", "end": "2026-07-31"}
    )
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "规则：" in html
    assert "30 分钟未回复预警" in html
    assert "清除规则筛选" in html
    clear_href = html_lib.unescape(
        re.search(r'id="rule-filter-clear" href="([^"]*)"', html).group(1)
    )
    clear_parts = urlsplit(clear_href)
    clear_qs = parse_qs(clear_parts.query)
    assert "rule_id" not in clear_qs
    assert clear_qs["start"] == ["2026-07-01"]
    assert clear_qs["end"] == ["2026-07-31"]

    chart = client.get(
        "/api/timeout/chart",
        query_string={"rule_id": RULE_30, "start": "2026-07-01", "end": "2026-07-31"},
    ).get_json()
    assert chart["totals"]["messages"] == 1
    assert [rule["rule_id"] for rule in chart["totals"]["rules"]] == [RULE_30]
    assert chart["rule_id"] == RULE_30

    events = client.get(
        "/api/timeout/events",
        query_string={
            "rule_id": RULE_30,
            "start": "2026-07-01",
            "end": "2026-07-31",
            "status": "all",
        },
    ).get_json()
    assert [event["msg_id"] for event in events["events"]] == ["m30"]

    timeout_js = _read("static/js/timeout.js")
    assert "rule_id: RULE_ID" in timeout_js
    assert "'rule_id'" in timeout_js


def test_kpi_confirmation_modal_and_rule_detection():
    template = _read("templates/admin_rules.html")
    source = _read("static/js/admin-rules.js")
    assert 'class="ant-modal ant-modal-confirm' in template
    assert "KPI 只统计 30 分钟规则，勾选后此规则将计入评分" in template
    assert "THIRTY_MINUTE_RULE_ID = '2055182790919555459'" in source
    assert "function isThirtyMinuteRule" in source
    assert "form.counts_for_kpi.checked = false" in source
    assert "data-rule-log" in source


def test_date_error_page_has_nav_and_retry_buttons(rules_env):
    client, _db = rules_env
    response = client.get(
        "/score/store",
        query_string={
            "name": "甲店",
            "start": "2026-07-31",
            "end": "2026-07-01",
            "theater": "华东战区",
        },
    )
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "日期范围错误" in html
    assert 'aria-label="主导航"' in html
    assert "用默认日期重新打开" in html
    assert "返回拉群覆盖" in html
    assert 'data-ds-back="' not in html

    retry = html_lib.unescape(
        re.search(r'href="([^"]*)">用默认日期重新打开', html).group(1)
    )
    retry_parts = urlsplit(retry)
    assert retry_parts.path == "/score/store"
    retry_qs = parse_qs(retry_parts.query)
    assert retry_qs["name"] == ["甲店"]
    assert retry_qs["theater"] == ["华东战区"]
    assert "start" not in retry_qs and "end" not in retry_qs


def test_primary_pages_do_not_show_back_header():
    for template in ("templates/timeout.html", "templates/groups.html", "templates/score.html"):
        source = _read(template)
        assert "data-ds-back" not in source
        assert "data-ds-back-label" not in source
        assert "<h1>" in source
        assert 'class="ant-page-header"' in source


def test_new_rule_changed_to_ten_minutes_requires_confirmation():
    source = _read("static/js/admin-rules.js")
    submit_block = source.split("form.addEventListener('submit'", 1)[1].split(
        "document.querySelector('#rules-table')", 1
    )[0]
    assert "await confirmKpiIfNeeded()" in submit_block
    assert "form.counts_for_kpi.checked = false" in submit_block
    assert submit_block.index("confirmKpiIfNeeded") < submit_block.index("const body =")
    assert "kpiConfirmedKey" in source
    assert "Number(form.minutes.value)" in source


def test_timeout_kpi_rule_cards_have_scoring_note():
    source = _read("static/js/timeout.js")
    assert "规则触发统计（同一消息先命中 10 分钟规则时记为重复触发），评分口径见超时评分" in source
    assert "rule-card-info" in source
    assert "displayOnly ? '' :" in source


def test_admin_rule_submit_confirm_race_guards():
    source = _read("static/js/admin-rules.js")
    assert "let modalSession = 0;" in source
    assert "let submitInFlight = false;" in source
    assert source.count("modalSession += 1;") >= 2
    assert "if (!$('kpi-confirm').hidden) finishKpiConfirm(false);" in source
    assert "form.inert = true;" in source
    assert "form.inert = false;" in source
    assert "form.removeAttribute('inert');" in source

    submit_block = source.split("form.addEventListener('submit'", 1)[1].split(
        "document.querySelector('#rules-table')", 1
    )[0]
    assert "if (submitInFlight) return;" in submit_block
    assert "const session = modalSession;" in submit_block
    assert submit_block.index("if (submitInFlight) return;") < submit_block.index(
        "await confirmKpiIfNeeded()"
    )
    assert submit_block.count("if (session !== modalSession) return;") >= 2
    assert submit_block.index("await confirmKpiIfNeeded()") < submit_block.index(
        "const body ="
    )
