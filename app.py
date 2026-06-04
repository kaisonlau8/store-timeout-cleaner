import os
import re
import csv
import io
import json
import zipfile
import tempfile
import shutil
from datetime import datetime
from urllib.parse import quote
from flask import Flask, render_template, request, jsonify, redirect, Response
import pandas as pd
from dateutil import parser as date_parser

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
DATA_FILE = 'data.json'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

CUTOFF_DATE = datetime(2026, 5, 1)


def save_data(stats, summary):
    """保存统计结果到 JSON 文件"""
    data = {
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'stats': stats,
        'summary': summary,
    }
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_data():
    """从 JSON 文件加载统计结果"""
    if not os.path.exists(DATA_FILE):
        return None
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def extract_store(dept_str):
    """从部门字符串中提取店名"""
    if not isinstance(dept_str, str) or not dept_str.strip():
        return None
    parts = [p.strip() for p in dept_str.split('/') if p.strip()]
    # 排除最后一项（部门），从倒数第二项往前找，找到第一个以"猛士"开头的作为店名
    for part in reversed(parts[:-1]):
        if part.startswith('猛士'):
            return part
    return None


def parse_create_time(time_val):
    """解析创建时间为 datetime 对象"""
    if pd.isna(time_val):
        return None
    if isinstance(time_val, datetime):
        return time_val
    try:
        return date_parser.parse(str(time_val))
    except Exception:
        return None


def get_excel_path_from_upload(filepath):
    """如果上传的是zip，解压后返回里面的xlsx/xls路径"""
    if filepath.lower().endswith('.zip'):
        extract_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(filepath, 'r') as z:
            z.extractall(extract_dir)
            for name in z.namelist():
                if name.lower().endswith(('.xlsx', '.xls')):
                    return os.path.join(extract_dir, name), extract_dir
        raise ValueError('ZIP文件中未找到Excel文件')
    return filepath, None


def analyze_excel(filepath):
    """分析单个Excel文件，返回群列表和按店统计"""
    excel_path, temp_dir = get_excel_path_from_upload(filepath)
    try:
        df = pd.read_excel(excel_path)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
    rows = []
    for _, row in df.iterrows():
        dept = row.get('群主所在部门（主）', None)
        store = extract_store(dept)
        create_time = parse_create_time(row.get('创建时间', None))
        rows.append({
            'group_id': row.get('群ID', ''),
            'group_name': row.get('群名', ''),
            'owner': row.get('群主', ''),
            'dept': dept,
            'store': store,
            'create_time': create_time.isoformat() if create_time else None,
            'member_count': int(row.get('群人数', 0) or 0),
            'customer_count': int(row.get('群客户数', 0) or 0),
        })

    valid_rows = [r for r in rows if r['store'] is not None]
    invalid_rows = [r for r in rows if r['store'] is None]

    # 按店统计
    store_stats = {}
    for r in valid_rows:
        s = r['store']
        if s not in store_stats:
            store_stats[s] = {'count': 0, 'groups': []}
        store_stats[s]['count'] += 1
        store_stats[s]['groups'].append(r)

    return {
        'total_rows': len(rows),
        'valid_count': len(valid_rows),
        'invalid_count': len(invalid_rows),
        'store_count': len(store_stats),
        'store_stats': store_stats,
        'rows': valid_rows,
    }


def build_monthly_stats(rows):
    """按月统计"""
    months = {}
    for r in rows:
        ct = r.get('create_time')
        if ct is None:
            continue
        if isinstance(ct, str):
            ct = datetime.fromisoformat(ct)
        month_key = ct.strftime('%Y-%m')
        if month_key not in months:
            months[month_key] = {'count': 0, 'stores': {}}
        months[month_key]['count'] += 1
        s = r['store']
        if s not in months[month_key]['stores']:
            months[month_key]['stores'][s] = 0
        months[month_key]['stores'][s] += 1
    return months


def compute_stats(all_rows, filter_rows):
    """计算所有统计结果"""
    all_valid = [r for r in all_rows if r['store'] is not None]
    filter_valid = [r for r in filter_rows if r['store'] is not None]

    all_store_stats = {}
    for r in all_valid:
        s = r['store']
        all_store_stats.setdefault(s, 0)
        all_store_stats[s] += 1

    filter_store_stats = {}
    for r in filter_valid:
        s = r['store']
        filter_store_stats.setdefault(s, 0)
        filter_store_stats[s] += 1

    def parse_ct(r):
        ct = r.get('create_time')
        if ct and isinstance(ct, str):
            return datetime.fromisoformat(ct)
        return ct

    all_after = [r for r in all_valid if parse_ct(r) and parse_ct(r) >= CUTOFF_DATE]
    filter_after = [r for r in filter_valid if parse_ct(r) and parse_ct(r) >= CUTOFF_DATE]

    all_after_stats = {}
    for r in all_after:
        s = r['store']
        all_after_stats.setdefault(s, 0)
        all_after_stats[s] += 1

    filter_after_stats = {}
    for r in filter_after:
        s = r['store']
        filter_after_stats.setdefault(s, 0)
        filter_after_stats[s] += 1

    # 按月统计（对齐月份，方便前端比对）
    all_months_raw = build_monthly_stats(all_valid)
    filter_months_raw = build_monthly_stats(filter_valid)

    all_month_keys = set(all_months_raw.keys())
    filter_month_keys = set(filter_months_raw.keys())
    all_keys = sorted(all_month_keys | filter_month_keys)
    years = sorted(set(m.split('-')[0] for m in all_keys))

    monthly_data = {}
    for m in all_keys:
        a = all_months_raw.get(m, {})
        f = filter_months_raw.get(m, {})
        monthly_data[m] = {
            'all': {
                'group_count': a.get('count', 0),
                'store_count': len(a.get('stores', {})),
                'store_stats': dict(sorted(a.get('stores', {}).items(), key=lambda x: -x[1])),
            },
            'filter': {
                'group_count': f.get('count', 0),
                'store_count': len(f.get('stores', {})),
                'store_stats': dict(sorted(f.get('stores', {}).items(), key=lambda x: -x[1])),
            },
        }

    return {
        'national': {
            'all': {
                'group_count': len(all_valid),
                'store_count': len(all_store_stats),
                'store_stats': dict(sorted(all_store_stats.items(), key=lambda x: -x[1])),
            },
            'filter': {
                'group_count': len(filter_valid),
                'store_count': len(filter_store_stats),
                'store_stats': dict(sorted(filter_store_stats.items(), key=lambda x: -x[1])),
            },
        },
        'after_may1': {
            'all': {
                'group_count': len(all_after),
                'store_count': len(all_after_stats),
                'store_stats': dict(sorted(all_after_stats.items(), key=lambda x: -x[1])),
            },
            'filter': {
                'group_count': len(filter_after),
                'store_count': len(filter_after_stats),
                'store_stats': dict(sorted(filter_after_stats.items(), key=lambda x: -x[1])),
            },
        },
        'monthly': {
            'months': all_keys,
            'years': years,
            'data': monthly_data,
        },
    }


@app.route('/')
def index():
    """展示页：直接显示最新统计结果"""
    data = load_data()
    has_data = data is not None
    return render_template('index.html', has_data=has_data, data=data)


@app.route('/parse')
def parse_page():
    """解析页：上传文件并生成统计"""
    return render_template('parse.html')


@app.route('/api/upload', methods=['POST'])
def upload():
    """解析上传的文件，保存结果，返回统计"""
    all_file = request.files.get('all_file')
    filter_file = request.files.get('filter_file')

    if not all_file or not filter_file:
        return jsonify({'error': '请上传两个文件'}), 400

    all_path = os.path.join(app.config['UPLOAD_FOLDER'], 'all_' + all_file.filename)
    filter_path = os.path.join(app.config['UPLOAD_FOLDER'], 'filter_' + filter_file.filename)

    all_file.save(all_path)
    filter_file.save(filter_path)

    try:
        all_data = analyze_excel(all_path)
        filter_data = analyze_excel(filter_path)

        stats = compute_stats(all_data['rows'], filter_data['rows'])
        summary = {
            'all_total': all_data['total_rows'],
            'all_valid': all_data['valid_count'],
            'all_invalid': all_data['invalid_count'],
            'filter_total': filter_data['total_rows'],
            'filter_valid': filter_data['valid_count'],
            'filter_invalid': filter_data['invalid_count'],
        }

        save_data(stats, summary)

        return jsonify({
            'success': True,
            'stats': stats,
            'summary': summary,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/export')
def export_csv():
    """导出各店群数量为 CSV"""
    scope = request.args.get('scope', 'national')
    dataset = request.args.get('dataset', 'all')

    data = load_data()
    if not data:
        return jsonify({'error': '暂无数据，请先解析'}), 400

    store_stats = data['stats'].get(scope, {}).get(dataset, {}).get('store_stats', {})
    if not store_stats:
        return jsonify({'error': '没有可导出的数据'}), 400

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['排名', '门店名称', '群数量'])
    for idx, (store, count) in enumerate(store_stats.items(), 1):
        writer.writerow([idx, store, count])

    filename_map = {
        ('national', 'all'): '全国全量各店群数量',
        ('national', 'filter'): '全国已拉机器人各店群数量',
        ('after_may1', 'all'): '5月1日后全量各店群数量',
        ('after_may1', 'filter'): '5月1日后已拉机器人各店群数量',
    }
    filename = filename_map.get((scope, dataset), '各店群数量') + '.csv'

    return Response(
        output.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename)}
    )


@app.route('/api/export-all')
def export_all_csv():
    """导出总表：所有门店全量群数与拉机器人群数对比"""
    data = load_data()
    if not data:
        return jsonify({'error': '暂无数据，请先解析'}), 400

    all_stats = data['stats']['national']['all']['store_stats']
    filter_stats = data['stats']['national']['filter']['store_stats']

    all_stores = set(all_stats.keys()) | set(filter_stats.keys())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['序号', '店名', '全量群数', '拉机器人群数'])
    for idx, store in enumerate(sorted(all_stores), 1):
        writer.writerow([idx, store, all_stats.get(store, 0), filter_stats.get(store, 0)])

    return Response(
        output.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote('门店群数量总表.csv')}
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
