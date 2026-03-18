# Resource Monitor

사내 공용 컴퓨터들의 리소스 현황을 실시간으로 모니터링하는 시스템입니다.

```
ResourceMonitors/
├── agent/                  ← 각 컴퓨터에 설치하는 에이전트
│   ├── agent.py
│   ├── config.yaml
│   └── requirements.txt
└── hub/                    ← 중앙 허브 서버 (웹 UI 포함)
    ├── hub.py
    ├── config.yaml
    ├── requirements.txt
    └── templates/
        ├── index.html      (대시보드)
        ├── detail.html     (컴퓨터 상세)
        ├── settings.html   (설정)
        └── error.html
```

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| 온라인/오프라인 상태 | 에이전트 응답 여부로 자동 판단 |
| 원격 접속 감지 | RDP / SSH / VNC 세션 실시간 탐지 |
| CPU 사용률 | 코어 수, 주파수 포함 |
| Memory 사용률 | 전체/사용/여유 GB |
| GPU 사용률 | NVIDIA 다중 GPU 지원, VRAM·온도 포함 |
| 프로그램 실행 여부 | 프로세스 이름으로 확인 |
| 프로그램 설치 여부 | 명령어 실행 결과로 확인 |
| 접속 IP 제한 | 웹 UI에 단일 IP 또는 CIDR 화이트리스트 |
| 1시간 히스토리 차트 | CPU·Memory·GPU 추이 (캔버스 기반) |

---

## 설치 및 실행

### 1. Hub 서버 설정

```bash
cd hub
pip install -r requirements.txt
```

**`hub/config.yaml` 필수 수정:**

```yaml
server:
  secret_key: "<랜덤 문자열>"   # python -c "import secrets; print(secrets.token_hex(32))"

security:
  api_key: "<강력한 API 키>"    # 에이전트와 동일하게 설정
```

Hub 서버 실행:

```bash
python hub.py
# 브라우저에서 http://<허브IP>:5000 접속
```

---

### 2. Agent 설치 (각 모니터링 대상 컴퓨터)

```bash
cd agent
pip install -r requirements.txt
```

**`agent/config.yaml` 필수 수정:**

```yaml
hub:
  url: "http://<허브IP>:5000"
  api_key: "<Hub와 동일한 API 키>"
```

Agent 실행:

```bash
python agent.py
```

#### Windows 서비스로 등록 (선택)

```bash
pip install pywin32
# 관리자 권한으로 실행
python -m win32serviceutil install ResourceMonitorAgent
python -m win32serviceutil start  ResourceMonitorAgent
```

#### Linux systemd 서비스로 등록 (선택)

`/etc/systemd/system/resource-monitor-agent.service`:

```ini
[Unit]
Description=Resource Monitor Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/agent/agent.py
WorkingDirectory=/path/to/agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now resource-monitor-agent
```

---

## 보안 구성

| 항목 | 방법 |
|------|------|
| 웹 UI 접근 제어 | 설정 페이지에서 허용 IP/CIDR 목록 관리 |
| 에이전트 인증 | `X-API-Key` 헤더 (timing-safe 비교) |
| CSRF 방지 | 세션 토큰 (모든 POST 요청) |
| XSS 방지 | Jinja2 자동 이스케이프 |
| 보안 헤더 | X-Content-Type-Options, X-Frame-Options 등 |
| SQL 인젝션 방지 | 파라미터화된 쿼리 |
| 명령어 인젝션 방지 | check_command 화이트리스트 검증 |

> 사내망이라도 API 키와 Secret Key는 반드시 강력한 값으로 변경하세요.

---

## 웹 UI 화면

- **대시보드** (`/`) – 전체 컴퓨터 카드 그리드, 30초 자동 갱신
- **상세 페이지** (`/computer/<hostname>`) – 게이지·차트·세션·프로그램 상태
- **설정** (`/settings`) – IP 화이트리스트, 모니터링 프로그램 목록 관리

---

## GPU 지원

NVIDIA GPU는 `pynvml` (nvidia-ml-py) 또는 `nvidia-smi`로 자동 감지됩니다.
GPU가 없는 컴퓨터에서는 GPU 섹션이 숨겨집니다.

```bash
# NVIDIA GPU가 있는 경우 추가 설치
pip install nvidia-ml-py
```
