#!/usr/bin/env python3
"""
Resource Monitor Agent
각 컴퓨터에 설치되어 시스템 정보를 수집하고 Hub에 전송하는 에이전트
"""

import os
import sys
import json
import time
import socket
import logging
import platform
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import psutil
import requests
import yaml

# GPU 지원 라이브러리 (선택적)
try:
    import pynvml
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except Exception:
    NVML_AVAILABLE = False

# 로깅 설정
LOG_PATH = Path(__file__).parent / 'agent.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_PATH), encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / 'config.yaml'


# ──────────────────────────────────────────────
# 설정 로드
# ──────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error(f"설정 파일을 찾을 수 없습니다: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────
# 시스템 정보 수집
# ──────────────────────────────────────────────

def get_ip_address() -> str:
    """현재 컴퓨터의 주 IP 주소 반환"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


def get_cpu_info() -> dict:
    """CPU 사용량 및 정보"""
    freq = psutil.cpu_freq()
    return {
        'percent': psutil.cpu_percent(interval=1),
        'count_logical': psutil.cpu_count(logical=True),
        'count_physical': psutil.cpu_count(logical=False),
        'freq_mhz': round(freq.current, 0) if freq else None,
    }


def get_memory_info() -> dict:
    """메모리 사용량 정보"""
    mem = psutil.virtual_memory()
    return {
        'total_gb': round(mem.total / (1024 ** 3), 2),
        'available_gb': round(mem.available / (1024 ** 3), 2),
        'used_gb': round(mem.used / (1024 ** 3), 2),
        'percent': mem.percent,
    }


def get_gpu_info() -> list:
    """NVIDIA GPU 정보 수집 (다중 GPU 지원)"""
    gpus = []

    # 1순위: pynvml 사용
    if NVML_AVAILABLE:
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode('utf-8')

                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)

                try:
                    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    temp = None

                total_mb = round(mem_info.total / (1024 ** 2))
                used_mb = round(mem_info.used / (1024 ** 2))
                gpus.append({
                    'index': i,
                    'name': name,
                    'memory_total_mb': total_mb,
                    'memory_used_mb': used_mb,
                    'memory_free_mb': round(mem_info.free / (1024 ** 2)),
                    'memory_percent': round(used_mb / total_mb * 100, 1) if total_mb > 0 else 0,
                    'utilization_percent': util.gpu,
                    'memory_utilization_percent': util.memory,
                    'temperature_c': temp,
                })
            return gpus
        except Exception as e:
            logger.debug(f"pynvml GPU 정보 수집 실패: {e}")

    # 2순위: nvidia-smi subprocess 사용
    try:
        result = subprocess.run(
            [
                'nvidia-smi',
                '--query-gpu=index,name,memory.total,memory.used,memory.free,'
                'utilization.gpu,utilization.memory,temperature.gpu',
                '--format=csv,noheader,nounits',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(',')]
                if len(parts) < 8:
                    continue
                total_mb = int(parts[2]) if parts[2].isdigit() else 0
                used_mb = int(parts[3]) if parts[3].isdigit() else 0

                def _parse_int(val):
                    try:
                        return int(val)
                    except (ValueError, TypeError):
                        return None

                gpus.append({
                    'index': _parse_int(parts[0]),
                    'name': parts[1],
                    'memory_total_mb': total_mb,
                    'memory_used_mb': used_mb,
                    'memory_free_mb': _parse_int(parts[4]),
                    'memory_percent': round(used_mb / total_mb * 100, 1) if total_mb > 0 else 0,
                    'utilization_percent': _parse_int(parts[5]),
                    'memory_utilization_percent': _parse_int(parts[6]),
                    'temperature_c': _parse_int(parts[7]),
                })
    except Exception as e:
        logger.debug(f"nvidia-smi GPU 정보 수집 실패: {e}")

    return gpus


def get_remote_sessions() -> list:
    """원격 접속 세션 정보 수집 (RDP / SSH / VNC)"""
    sessions = []
    os_type = platform.system()

    # ── Windows: query session ──
    if os_type == 'Windows':
        try:
            result = subprocess.run(
                ['query', 'session'],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n')[1:]:
                    if 'Active' in line:
                        parts = line.split()
                        sessions.append({
                            'type': 'RDP/Console',
                            'user': parts[0].lstrip('>') if parts else '',
                            'state': 'Active',
                            'remote_ip': '',
                        })
        except Exception as e:
            logger.debug(f"Windows 세션 조회 실패: {e}")

    # ── Linux: who ──
    elif os_type == 'Linux':
        try:
            result = subprocess.run(['who'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        remote_ip = ''
                        if len(parts) >= 5 and '(' in parts[-1]:
                            remote_ip = parts[-1].strip('()')
                        sessions.append({
                            'type': 'SSH/Terminal',
                            'user': parts[0],
                            'terminal': parts[1],
                            'remote_ip': remote_ip,
                            'state': 'Active',
                        })
        except Exception as e:
            logger.debug(f"Linux who 조회 실패: {e}")

    # ── 공통: 네트워크 연결 확인 (RDP:3389 / SSH:22 / VNC:5900) ──
    port_map = {3389: 'RDP', 22: 'SSH', 5900: 'VNC'}
    try:
        existing_ips = {s.get('remote_ip') for s in sessions}
        for conn in psutil.net_connections(kind='tcp'):
            if conn.status != 'ESTABLISHED':
                continue
            if conn.laddr.port not in port_map:
                continue
            remote_ip = conn.raddr.ip if conn.raddr else ''
            conn_type = port_map[conn.laddr.port]
            if remote_ip and remote_ip not in existing_ips:
                sessions.append({
                    'type': conn_type,
                    'user': '',
                    'remote_ip': remote_ip,
                    'state': 'Established',
                })
                existing_ips.add(remote_ip)
    except Exception as e:
        logger.debug(f"네트워크 연결 조회 실패: {e}")

    return sessions


def check_running_programs(programs: list) -> dict:
    """실행 중인 프로그램 확인"""
    if not programs:
        return {}

    # 현재 실행 중인 프로세스 이름 집합
    running = set()
    for proc in psutil.process_iter(['name']):
        try:
            name = (proc.info['name'] or '').lower()
            running.add(name)
            running.add(name.replace('.exe', ''))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    result = {}
    for prog in programs:
        proc_name = prog.get('process', '').lower().strip()
        if not proc_name:
            continue
        is_running = (
            proc_name in running
            or proc_name + '.exe' in running
            or any(proc_name in p for p in running)
        )
        result[prog['name']] = is_running

    return result


def check_installed_programs(programs: list) -> dict:
    """설치된 프로그램 확인 (check_command 실행 결과로 판단)"""
    if not programs:
        return {}

    # 허용된 명령어 prefix (보안 화이트리스트)
    ALLOWED_COMMANDS = {
        'python', 'python3', 'pip', 'pip3',
        'nvcc', 'nvidia-smi', 'docker', 'git',
        'node', 'npm', 'java', 'javac',
        'gcc', 'g++', 'cmake', 'make',
        'ffmpeg', 'ffprobe', 'matlab',
        'conda', 'code', 'blender',
        'powershell', 'pwsh',
    }

    result = {}
    for prog in programs:
        check_cmd = prog.get('check_command', '').strip()
        if not check_cmd:
            result[prog['name']] = False
            continue

        cmd_parts = check_cmd.split()
        base_cmd = os.path.basename(cmd_parts[0]).lower().replace('.exe', '')

        if base_cmd not in ALLOWED_COMMANDS:
            logger.warning(f"허용되지 않은 명령어: {cmd_parts[0]}")
            result[prog['name']] = False
            continue

        try:
            proc = subprocess.run(
                cmd_parts,
                capture_output=True,
                text=True,
                timeout=10,
            )
            result[prog['name']] = proc.returncode == 0
        except FileNotFoundError:
            result[prog['name']] = False
        except subprocess.TimeoutExpired:
            result[prog['name']] = False
        except Exception as e:
            logger.debug(f"설치 확인 실패 ({prog['name']}): {e}")
            result[prog['name']] = False

    return result


# ──────────────────────────────────────────────
# Hub 통신
# ──────────────────────────────────────────────

def collect_system_info(config: dict) -> dict:
    """전체 시스템 정보 수집"""
    hostname = config.get('agent', {}).get('hostname') or socket.gethostname()
    monitoring = config.get('monitoring', {})

    remote_sessions = get_remote_sessions()

    return {
        'hostname': hostname,
        'ip_address': get_ip_address(),
        'os': f"{platform.system()} {platform.release()} {platform.machine()}",
        'timestamp': datetime.now().isoformat(),
        'cpu': get_cpu_info(),
        'memory': get_memory_info(),
        'gpu': get_gpu_info(),
        'remote_sessions': remote_sessions,
        'has_remote_connection': len(remote_sessions) > 0,
        'running_programs': check_running_programs(
            monitoring.get('programs_to_check_running', [])
        ),
        'installed_programs': check_installed_programs(
            monitoring.get('programs_to_check_installed', [])
        ),
    }


def report_to_hub(config: dict, data: dict) -> None:
    """Hub 서버에 데이터 전송"""
    hub_cfg = config.get('hub', {})
    hub_url = hub_cfg.get('url', 'http://localhost:5000').rstrip('/')
    api_key = hub_cfg.get('api_key', '')

    try:
        resp = requests.post(
            f"{hub_url}/api/report",
            json=data,
            headers={'X-API-Key': api_key},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"데이터 전송 성공 → {hub_url}  [{data['hostname']}]")
        else:
            logger.warning(f"Hub 응답 오류: HTTP {resp.status_code}")
    except requests.exceptions.ConnectionError:
        logger.warning(f"Hub 연결 실패: {hub_url}")
    except Exception as e:
        logger.error(f"데이터 전송 오류: {e}")


def fetch_hub_monitoring_config(config: dict) -> dict | None:
    """Hub에서 모니터링 설정(프로그램 목록 등) 가져오기"""
    hub_cfg = config.get('hub', {})
    hub_url = hub_cfg.get('url', 'http://localhost:5000').rstrip('/')
    api_key = hub_cfg.get('api_key', '')

    try:
        resp = requests.get(
            f"{hub_url}/api/config",
            headers={'X-API-Key': api_key},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.debug(f"Hub 설정 가져오기 실패: {e}")
    return None


# ──────────────────────────────────────────────
# 메인 루프
# ──────────────────────────────────────────────

def main():
    logger.info("=" * 50)
    logger.info("Resource Monitor Agent 시작")

    config = load_config()
    interval = int(config.get('agent', {}).get('report_interval_seconds', 30))
    config_refresh_interval = 300  # 5분마다 Hub에서 설정 갱신

    # 최초 Hub 설정 가져오기
    hub_monitoring = fetch_hub_monitoring_config(config)
    if hub_monitoring:
        config['monitoring'] = hub_monitoring
        logger.info("Hub에서 모니터링 설정 동기화 완료")

    last_config_fetch = time.time()

    while True:
        try:
            # 주기적 Hub 설정 갱신
            if time.time() - last_config_fetch > config_refresh_interval:
                hub_monitoring = fetch_hub_monitoring_config(config)
                if hub_monitoring:
                    config['monitoring'] = hub_monitoring
                last_config_fetch = time.time()

            data = collect_system_info(config)
            report_to_hub(config, data)
            time.sleep(interval)

        except KeyboardInterrupt:
            logger.info("Agent 종료")
            break
        except Exception as e:
            logger.error(f"예기치 않은 오류: {e}", exc_info=True)
            time.sleep(interval)


if __name__ == '__main__':
    main()
