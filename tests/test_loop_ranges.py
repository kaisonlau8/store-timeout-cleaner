"""Date guard, offline auth redirects, and real JS state-machine regressions."""
from datetime import date
import logging
from pathlib import Path
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit, unquote
from unittest.mock import Mock

import pytest

from date_ranges import DateRangeError, parse_range
from tests.test_store_page import store_client, STORE


@pytest.mark.parametrize('a,b', [
    ('bad','2026-09-30'), ('2026-02-30','2026-09-30'),
    ('20260701','2026-09-30'), ('2026-07-01suffix','2026-09-30'),
    ('2026-09-30','2026-07-01'), ('2025-12-31','2026-09-30'),
    ('2026-07-01','2099-01-01'),
])
@pytest.mark.parametrize('path', ['/timeout','/score','/score/store','/api/score/llm-status','/api/score/export.csv','/api/score/export.xlsx','/api/timeout/chart','/api/timeout/events','/api/timeout/export','/store/'+STORE])
def test_invalid_range_400_before_query_and_warning(store_client, monkeypatch, caplog, a, b, path):
    client, mod, _ = store_client
    heavy = Mock(side_effect=AssertionError('must reject before heavy query'))
    monkeypatch.setattr(mod, 'query_chart', heavy)
    monkeypatch.setattr(mod, 'store_coverage_detail', heavy)
    monkeypatch.setattr('score_web.build_report', heavy)
    with caplog.at_level(logging.WARNING):
        response = client.get(path, query_string={'start':a,'end':b}, headers={
            'X-Forwarded-For':'192.0.2.10, 127.0.0.1', 'User-Agent':'STC-range-test',
            'Cookie':'private-cookie-not-for-logs', 'Authorization':'private-token-not-for-logs',
        })
    assert response.status_code == 400
    heavy.assert_not_called()
    assert 'suspicious date range' in caplog.text
    assert path in caplog.text and a in caplog.text and '192.0.2.10' in caplog.text and 'STC-range-test' in caplog.text
    assert 'private-cookie' not in caplog.text and 'private-token' not in caplog.text
    if path.startswith('/api/'):
        assert response.json['code'] == 'invalid_date_range'
    else:
        assert 'ant-alert-error' in response.text and '日期范围错误' in response.text
        assert response.headers.get('Location') is None


def test_span_boundary_and_shanghai_defaults():
    assert parse_range(today=date(2026,10,17)) == (date(2026,10,1),date(2026,10,17))
    assert parse_range('2026-01-01','2027-01-01',today=date(2027,1,2)) == (date(2026,1,1),date(2027,1,1))
    with pytest.raises(DateRangeError, match='366'):
        parse_range('2026-01-01','2027-01-02',today=date(2027,1,2))


def test_timeout_store_defaults_and_old_links(store_client):
    client, _, _ = store_client
    a,b = parse_range()
    for path in ('/timeout','/store/'+STORE):
        response = client.get(path)
        assert response.status_code == 200
        assert f'value="{a}"' in response.text and f'value="{b}"' in response.text
    response = client.get('/timeout?start=2026-07-01&end=2026-09-30&theater=华东战区&q=甲')
    assert response.status_code == 200 and 'value="2026-07-01"' in response.text
    response = client.get('/store/'+STORE+'?date_from=2026-07-01&date_to=2026-07-31')
    assert response.status_code == 200 and 'msg-start' in response.text


def test_timeout_apis_use_current_month_before_query(store_client, monkeypatch):
    client, mod, _ = store_client
    chart = Mock(return_value={})
    events = Mock(return_value={})
    monkeypatch.setattr(mod, 'query_chart', chart)
    monkeypatch.setattr(mod.stc_live, 'query_events', events)
    a,b = parse_range()
    assert client.get('/api/timeout/chart').status_code == 200
    assert client.get('/api/timeout/events').status_code == 200
    for call in (chart.call_args, events.call_args):
        assert call.kwargs['start'] == str(a) and call.kwargs['end'] == str(b)


@pytest.mark.parametrize('params', [
    {'start':'2025-12-31'}, {'start':'2099-01-01'}, {'start':'bad'},
    {'start':'2026-09-30','end':'2026-07-01'},
])
def test_admin_recompute_rejects_explicit_suspicious_dates(store_client, monkeypatch, caplog, params):
    client, mod, _ = store_client
    heavy = Mock(side_effect=AssertionError('must reject before recompute'))
    monkeypatch.setattr(mod.timeout_algo, 'start_background', heavy)
    with caplog.at_level(logging.WARNING):
        response = client.post('/api/admin/recompute', json=params)
    assert response.status_code == 400 and response.json['code'] == 'invalid_date_range'
    assert 'suspicious date range' in caplog.text and params['start'] in caplog.text
    heavy.assert_not_called()


@pytest.mark.parametrize('params', [
    {'date_from':'2026-09-30','date_to':'2026-07-01'},
    {'date_from':'2025-12-31'}, {'date_to':'2099-01-01'},
    {'group_from':'bad'}, {'group_from':'2026-09-30','group_to':'2026-07-01'},
])
def test_store_native_date_validation(store_client, params, caplog):
    client, _, _ = store_client
    with caplog.at_level(logging.WARNING):
        response = client.get('/store/'+STORE, query_string=params)
    assert response.status_code == 400 and 'suspicious date range' in caplog.text


@pytest.mark.parametrize('path', ['/','/timeout','/score','/score/store?name=甲','/store/'+STORE,'/groups','/admin/rules'])
def test_expired_auth_redirect_preserves_next_without_api_redirect(store_client, monkeypatch, path):
    client, mod, _ = store_client
    auth = mod.app.extensions['mhero_auth']
    cfg = auth.cfg
    for key,value in {'mode':'on','off_file':'','issuer':'http://127.0.0.1:19999','base_url':'http://localhost','client_id':'offline-client','client_secret':'offline-client-fixture','session_secret':'offline-fixture-session-signing-key-32','trust_loopback':False}.items():
        monkeypatch.setattr(cfg,key,value)
    client.set_cookie(cfg.cookie_name, 'invalid-expired-session')
    response = client.get(path)
    assert response.status_code == 302
    target = urlsplit(response.location)
    assert target.path == '/_auth/login'
    assert unquote(parse_qs(target.query)['next'][0]) == path
    # Follow only the local login endpoint; never contact the identity provider.
    login = client.get(response.location)
    assert login.status_code == 302
    assert login.location.startswith(cfg.issuer+'/login/oauth/authorize?')
    api = client.get('/api/score/llm-status?start=2026-07-01&end=2026-09-30')
    assert api.status_code == 401 and api.is_json and 'Location' not in api.headers


def test_js_poll_state_machine_and_range_validation():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    script = Path(__file__).with_name('loop_ranges_js.cjs')
    result = subprocess.run(['node',str(script)],capture_output=True,text=True,check=True)
    assert '9 polling cases' in result.stdout
