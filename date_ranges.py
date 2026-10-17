"""Shared date contract for pages and APIs (Shanghai calendar days)."""
from datetime import date, datetime
import logging
import re
from zoneinfo import ZoneInfo

from flask import g, jsonify, render_template, request

MIN_DATE = date(2026, 1, 1)
SHANGHAI = ZoneInfo('Asia/Shanghai')


class DateRangeError(ValueError):
    pass


def today_shanghai():
    return datetime.now(SHANGHAI).date()


def parse_range(start=None, end=None, today=None):
    today = today or today_shanghai()
    def one(raw, fallback):
        if raw is None or raw == '':
            return fallback
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(raw)):
            raise DateRangeError('日期格式应为 YYYY-MM-DD')
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise DateRangeError('日期格式应为 YYYY-MM-DD') from None
    a, b = one(start, today.replace(day=1)), one(end, today)
    if a > b:
        raise DateRangeError('开始日期不能晚于结束日期')
    if a < MIN_DATE or b > today:
        raise DateRangeError(f'日期范围应在 {MIN_DATE} 至 {today} 之间')
    if (b - a).days + 1 > 366:
        raise DateRangeError('日期跨度不能超过 366 天')
    return a, b


def install_date_ranges(app):
    if app.extensions.get('stc_date_ranges'):
        return
    app.extensions['stc_date_ranges'] = True

    @app.context_processor
    def range_context():
        a, b = parse_range()
        return {'range_today': b.isoformat(), 'range_month_start': a.isoformat()}

    @app.before_request
    def validate_ranges():
        path = request.path
        if path in ('/score', '/score/store', '/timeout') or path.startswith('/api/score/') or path in ('/api/timeout/chart', '/api/timeout/export', '/api/timeout/events'):
            g.date_range = parse_range(request.args.get('start'), request.args.get('end'))
        elif path.startswith('/store/'):
            g.date_range = parse_range(request.args.get('date_from', request.args.get('start')), request.args.get('date_to', request.args.get('end')))
            # Group creation dates are optional; validate supplied endpoints as a pair.
            if request.args.get('group_from') or request.args.get('group_to'):
                parse_range(request.args.get('group_from') or MIN_DATE.isoformat(), request.args.get('group_to'))
        elif path == '/api/admin/recompute':
            body = request.get_json(silent=True)
            body = body if isinstance(body, dict) else {}
            start = body.get('start', request.args.get('start'))
            end = body.get('end', request.args.get('end'))
            # No supplied dates retains the existing intentional full recompute.
            if start or end:
                parse_range(start, end)

    @app.errorhandler(DateRangeError)
    def invalid_range(exc):
        params = {k: request.args.getlist(k) for k in ('start', 'end', 'date_from', 'date_to', 'group_from', 'group_to') if k in request.args}
        if request.path == '/api/admin/recompute':
            body = request.get_json(silent=True)
            if isinstance(body, dict):
                params.update({k: body[k] for k in ('start', 'end') if k in body})
        logging.getLogger(__name__).warning(
            'suspicious date range path=%s params=%r client_ip=%s UA=%r',
            request.path, params, (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip(),
            request.headers.get('User-Agent', ''),
        )
        if request.path.startswith('/api/'):
            return jsonify({'error': str(exc), 'code': 'invalid_date_range'}), 400
        return render_template('date_range_error.html', error=str(exc)), 400
