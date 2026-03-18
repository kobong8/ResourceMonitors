#!/usr/bin/env python3
"""
Resource Monitor Hub
에이전트로부터 데이터를 수집하고 웹 UI를 통해 현황을 표시하는 서버
"""

import os
import sys
import json
import secrets
import logging
import ipaddress
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import yaml
from flask import (
    Flask, g, request, jsonify,
    render_template, redirect, url_for, abort, session, flash,
)

# ──────────────────────────────────────────────
# 로깅 설정
# ──────────────────────────────────────────────
LOG_PATH = Path(__file__).parent / 'hub.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_PATH), encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 설정 로드
# ──────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / 'config.yaml'


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error(f"설정 파일 없음: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


CONFIG = load_config()

app = Flask(__name__)
app.secret_key = CONFIG.get('server', {}).get('secret_key') or secrets.token_hex(32)

DB_PATH = Path(__file__).parent / CONFIG.get('database', {}).get('path', 'hub_data.db')


# ──────────────────────────────────────────────
# 데이터베이스
# ──────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        g.db = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(str(DB_PATH))
    db.executescript('''
        CREATE TABLE IF NOT EXISTS computers (
            hostname    TEXT PRIMARY KEY,
            ip_address  TEXT,
            last_seen   TEXT,
            last_data   TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS data_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            hostname        TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            cpu_percent     REAL,
            memory_percent  REAL,
            gpu_data        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_history ON data_history(hostname, timestamp);

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    ''')

    # 기본 설정
    defaults = {
        'allowed_ips': json.dumps([]),
        'programs_to_check_running': json.dumps([
            {'name': 'Chrome', 'process': 'chrome'},
            {'name': 'Visual Studio Code', 'process': 'code'},
            {'name': 'Python', 'process': 'python'},
            {'name': 'Jupyter', 'process': 'jupyter'},
        ]),
        'programs_to_check_installed': json.dumps([
            {'name': 'Python', 'check_command': 'python --version'},
            {'name': 'CUDA (nvcc)', 'check_command': 'nvcc --version'},
            {'name': 'Docker', 'check_command': 'docker --version'},
            {'name': 'Git', 'check_command': 'git --version'},
            {'name': 'Node.js', 'check_command': 'node --version'},
        ]),
        'agent_timeout_seconds': '60',
    }
    for key, value in defaults.items():
        db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (key, value))

    db.commit()
    db.close()
    logger.info("데이터베이스 초기화 완료")


def get_setting(key: str, default=None) -> str | None:
    row = get_db().execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key: str, value: str):
    db = get_db()
    db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (key, value))
    db.commit()


# ──────────────────────────────────────────────
# 보안 유틸리티
# ──────────────────────────────────────────────

def get_client_ip() -> str:
    """실제 클라이언트 IP (프록시 고려)"""
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or '0.0.0.0'


def is_ip_allowed() -> bool:
    """현재 요청 IP가 허용 목록에 있는지 확인"""
    allowed_ips: list = json.loads(get_setting('allowed_ips', '[]'))
    if not allowed_ips:          # 빈 목록 = 모두 허용
        return True

    client_ip = get_client_ip()
    try:
        client_obj = ipaddress.ip_address(client_ip)
        for entry in allowed_ips:
            try:
                if '/' in entry:
                    if client_obj in ipaddress.ip_network(entry, strict=False):
                        return True
                else:
                    if client_obj == ipaddress.ip_address(entry):
                        return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False


def require_ip_whitelist(f):
    """웹 UI 라우트용 IP 화이트리스트 데코레이터"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_ip_allowed():
            logger.warning(f"접근 거부: {get_client_ip()} → {request.path}")
            abort(403)
        return f(*args, **kwargs)
    return decorated


def validate_api_key() -> bool:
    """에이전트 API 키 검증 (timing-safe)"""
    expected = CONFIG.get('security', {}).get('api_key', '')
    provided = request.headers.get('X-API-Key', '')
    if not expected:        # 키 미설정 시 개발 편의를 위해 허용 (운영 환경에서는 반드시 설정)
        return True
    return secrets.compare_digest(provided, expected)


def require_api_key(f):
    """에이전트 API 엔드포인트용 키 검증 데코레이터"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not validate_api_key():
            logger.warning(f"잘못된 API 키 요청: {get_client_ip()}")
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


# CSRF 보호 (세션 토큰 방식)
def _csrf_token() -> str:
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(32)
    return session['_csrf']


def validate_csrf() -> bool:
    token = request.form.get('_csrf_token', '')
    sess_token = session.get('_csrf', '')
    if not token or not sess_token:
        return False
    return secrets.compare_digest(token, sess_token)


app.jinja_env.globals['csrf_token'] = _csrf_token


# ──────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────

def is_online(last_seen_str: str | None, timeout: int | None = None) -> bool:
    if not last_seen_str:
        return False
    if timeout is None:
        timeout = int(get_setting('agent_timeout_seconds', '60'))
    try:
        last_seen = datetime.fromisoformat(last_seen_str)
        return (datetime.now() - last_seen).total_seconds() < timeout
    except Exception:
        return False


def get_all_computers() -> list:
    timeout = int(get_setting('agent_timeout_seconds', '60'))
    rows = get_db().execute(
        'SELECT hostname, ip_address, last_seen, last_data FROM computers ORDER BY hostname'
    ).fetchall()
    result = []
    for row in rows:
        data = json.loads(row['last_data']) if row['last_data'] else {}
        result.append({
            'hostname': row['hostname'],
            'ip_address': row['ip_address'],
            'last_seen': row['last_seen'],
            'online': is_online(row['last_seen'], timeout),
            'data': data,
        })
    return result


# ──────────────────────────────────────────────
# Agent API 엔드포인트
# ──────────────────────────────────────────────

@app.route('/api/report', methods=['POST'])
@require_api_key
def api_report():
    """에이전트 데이터 수신"""
    try:
        data = request.get_json(force=True, silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({'error': 'Invalid JSON'}), 400

        hostname = str(data.get('hostname', '')).strip()
        if not hostname or len(hostname) > 255:
            return jsonify({'error': 'Invalid hostname'}), 400

        ip_address = str(data.get('ip_address', ''))[:64]
        now_str = datetime.now().isoformat()

        db = get_db()
        db.execute(
            'INSERT OR REPLACE INTO computers(hostname, ip_address, last_seen, last_data)'
            ' VALUES(?,?,?,?)',
            (hostname, ip_address, now_str, json.dumps(data)),
        )

        cpu_pct = float(data.get('cpu', {}).get('percent', 0))
        mem_pct = float(data.get('memory', {}).get('percent', 0))
        gpu_json = json.dumps(data.get('gpu', []))

        db.execute(
            'INSERT INTO data_history(hostname, timestamp, cpu_percent, memory_percent, gpu_data)'
            ' VALUES(?,?,?,?,?)',
            (hostname, data.get('timestamp', now_str), cpu_pct, mem_pct, gpu_json),
        )

        # 24시간 이상 된 히스토리 자동 삭제
        db.execute(
            "DELETE FROM data_history WHERE hostname=?"
            " AND timestamp < datetime('now','-24 hours')",
            (hostname,),
        )
        db.commit()

        logger.info(f"수신: {hostname} ({ip_address})")
        return jsonify({'status': 'ok', 'timestamp': now_str})

    except Exception as e:
        logger.error(f"데이터 수신 오류: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/config', methods=['GET'])
@require_api_key
def api_get_config():
    """에이전트에 모니터링 프로그램 목록 제공"""
    return jsonify({
        'programs_to_check_running': json.loads(
            get_setting('programs_to_check_running', '[]')
        ),
        'programs_to_check_installed': json.loads(
            get_setting('programs_to_check_installed', '[]')
        ),
    })


@app.route('/api/status', methods=['GET'])
@require_api_key
def api_status():
    """Hub 상태 확인 (헬스체크)"""
    computers = get_all_computers()
    return jsonify({
        'status': 'ok',
        'total': len(computers),
        'online': sum(1 for c in computers if c['online']),
        'timestamp': datetime.now().isoformat(),
    })


# ──────────────────────────────────────────────
# Web UI 라우트
# ──────────────────────────────────────────────

@app.route('/')
@require_ip_whitelist
def index():
    computers = get_all_computers()
    online = sum(1 for c in computers if c['online'])
    return render_template(
        'index.html',
        computers=computers,
        total_count=len(computers),
        online_count=online,
        offline_count=len(computers) - online,
    )


@app.route('/computer/<hostname>')
@require_ip_whitelist
def computer_detail(hostname):
    timeout = int(get_setting('agent_timeout_seconds', '60'))
    db = get_db()

    row = db.execute('SELECT * FROM computers WHERE hostname=?', (hostname,)).fetchone()
    if not row:
        abort(404)

    data = json.loads(row['last_data']) if row['last_data'] else {}
    online = is_online(row['last_seen'], timeout)

    # 최근 1시간 히스토리
    history_rows = db.execute(
        '''SELECT timestamp, cpu_percent, memory_percent, gpu_data
           FROM data_history
           WHERE hostname=? AND timestamp > datetime('now','-1 hour')
           ORDER BY timestamp ASC''',
        (hostname,),
    ).fetchall()

    history = {'timestamps': [], 'cpu': [], 'memory': [], 'gpu': {}}
    for h in history_rows:
        history['timestamps'].append(h['timestamp'])
        history['cpu'].append(h['cpu_percent'])
        history['memory'].append(h['memory_percent'])
        for gpu in json.loads(h['gpu_data'] or '[]'):
            idx = str(gpu.get('index', 0))
            if idx not in history['gpu']:
                history['gpu'][idx] = {
                    'name': gpu.get('name', f'GPU {idx}'),
                    'utilization': [],
                    'memory_percent': [],
                }
            history['gpu'][idx]['utilization'].append(gpu.get('utilization_percent'))
            history['gpu'][idx]['memory_percent'].append(gpu.get('memory_percent'))

    return render_template(
        'detail.html',
        hostname=hostname,
        computer={
            'hostname': row['hostname'],
            'ip_address': row['ip_address'],
            'last_seen': row['last_seen'],
            'online': online,
            'data': data,
        },
        history=history,
    )


@app.route('/settings', methods=['GET', 'POST'])
@require_ip_whitelist
def settings():
    if request.method == 'POST':
        if not validate_csrf():
            flash('보안 토큰이 유효하지 않습니다. 다시 시도해주세요.', 'error')
            return redirect(url_for('settings'))

        action = request.form.get('action', '')

        # ── 허용 IP 저장 ──
        if action == 'save_security':
            raw = request.form.get('allowed_ips', '')
            ips = []
            for line in raw.splitlines():
                ip = line.strip()
                if not ip:
                    continue
                try:
                    if '/' in ip:
                        ipaddress.ip_network(ip, strict=False)
                    else:
                        ipaddress.ip_address(ip)
                    ips.append(ip)
                except ValueError:
                    flash(f'유효하지 않은 IP/CIDR: {ip}', 'error')
                    return redirect(url_for('settings'))
            set_setting('allowed_ips', json.dumps(ips))
            flash('보안 설정이 저장되었습니다.', 'success')

        # ── 프로그램 목록 저장 ──
        elif action == 'save_programs':
            try:
                running_raw = request.form.get('programs_running', '[]')
                installed_raw = request.form.get('programs_installed', '[]')
                running = json.loads(running_raw)
                installed = json.loads(installed_raw)
                # 기본 유효성 검사
                for item in running + installed:
                    if not isinstance(item, dict):
                        raise ValueError("항목은 객체여야 합니다.")
                    if not item.get('name'):
                        raise ValueError("name 필드가 비어 있습니다.")
                set_setting('programs_to_check_running', json.dumps(running))
                set_setting('programs_to_check_installed', json.dumps(installed))
                flash('프로그램 모니터링 설정이 저장되었습니다.', 'success')
            except (json.JSONDecodeError, ValueError) as e:
                flash(f'설정 저장 실패: {e}', 'error')

        # ── 모니터링 타임아웃 저장 ──
        elif action == 'save_monitoring':
            try:
                timeout = int(request.form.get('agent_timeout', '60'))
                if not (10 <= timeout <= 3600):
                    raise ValueError("범위 초과")
                set_setting('agent_timeout_seconds', str(timeout))
                flash('모니터링 설정이 저장되었습니다.', 'success')
            except ValueError:
                flash('타임아웃은 10~3600 사이의 정수여야 합니다.', 'error')

        # ── 컴퓨터 삭제 ──
        elif action == 'delete_computer':
            target = request.form.get('hostname', '').strip()
            if target:
                db = get_db()
                db.execute('DELETE FROM computers WHERE hostname=?', (target,))
                db.execute('DELETE FROM data_history WHERE hostname=?', (target,))
                db.commit()
                flash(f'{target} 이(가) 삭제되었습니다.', 'success')

        return redirect(url_for('settings'))

    # GET
    db = get_db()
    computers = [r['hostname'] for r in db.execute('SELECT hostname FROM computers ORDER BY hostname').fetchall()]
    cur_settings = {
        'allowed_ips': '\n'.join(json.loads(get_setting('allowed_ips', '[]'))),
        'programs_running': json.loads(get_setting('programs_to_check_running', '[]')),
        'programs_installed': json.loads(get_setting('programs_to_check_installed', '[]')),
        'agent_timeout': get_setting('agent_timeout_seconds', '60'),
    }
    return render_template('settings.html', settings=cur_settings, computers=computers)


# ──────────────────────────────────────────────
# 오류 핸들러 & 보안 헤더
# ──────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template('error.html', code=403,
                           message='접근이 거부되었습니다.',
                           detail='귀하의 IP 주소는 접근 허용 목록에 없습니다.'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404,
                           message='페이지를 찾을 수 없습니다.',
                           detail='요청하신 컴퓨터 또는 페이지가 존재하지 않습니다.'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('error.html', code=500,
                           message='서버 내부 오류',
                           detail='잠시 후 다시 시도해주세요.'), 500


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Cache-Control'] = 'no-store'
    return response


# ──────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    srv = CONFIG.get('server', {})
    host = srv.get('host', '0.0.0.0')
    port = int(srv.get('port', 5000))
    debug = bool(srv.get('debug', False))
    logger.info(f"Resource Monitor Hub 시작  →  http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
