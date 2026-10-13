# Resource Monitor

각 원격 컴퓨터의 에이전트가 CPU·메모리·NVIDIA GPU와 프로그램 상태를 수집하고 중앙 Hub로 전송하는 모니터링 시스템입니다. Hub가 원격 PC에 직접 접속하는 방식이 아니므로 **모니터링할 모든 PC에서 에이전트를 실행해야 합니다.**

## 구성 및 지원 범위

- `agent/agent.py`: 리소스 수집, 인증된 HTTP 전송, 5분 간격 Hub 프로그램 설정 동기화
- `agent/build.ps1`: Windows standalone EXE 빌드
- `hub/hub.py`: Flask 서버, SQLite 저장, 대시보드·상세·설정 화면
- `tests/test_monitoring.py`: 수집·전송·저장 관련 회귀 테스트

| 항목 | 동작 / 제한 |
|---|---|
| CPU / RAM | psutil로 실제 사용률 수집 |
| GPU | NVIDIA NVML 우선, nvidia-smi 대체 수집. NVIDIA 드라이버 필요. AMD/Intel GPU 미지원 |
| 온라인 상태 | Hub의 마지막 수신 시각 기준, 기본 60초 초과 시 오프라인 |
| 히스토리 | 상세 화면 최근 1시간, 보고 수신 시 해당 PC의 24시간 초과 데이터 정리 |
| 원격 접속 | Windows RDP 세션 및 TCP 3389/22/5900 연결 감지. Linux who 보조 사용 |
| 프로그램 실행 | 프로세스 이름 정확히 비교 (.exe 생략 가능). 부분 문자열 검색 아님 |
| 프로그램 설치 | 허용된 도구의 버전 명령 성공 여부. PATH에 없는 프로그램은 감지되지 않을 수 있음 |

원격 접속 감지는 보조 지표입니다. 사용자 권한, Windows 표시 언어, 변경된 포트, SSH 비대화형 세션 등에 따라 누락될 수 있습니다. TCP 연결 자체가 사용자 로그인 성공을 보장하지는 않습니다. 디스크·네트워크 사용량 수집은 현재 구현되어 있지 않습니다.

## 1. Hub 실행

Hub 컴퓨터에는 Python 3.10 이상이 필요합니다. 저장소 루트에서:

```powershell
python -m venv hub/.venv
.\hub\.venv\Scripts\python.exe -X utf8 -m pip install -r hub/requirements.txt
```

`hub/config.yaml`을 수정합니다.

```yaml
server:
  host: "0.0.0.0"
  port: 5000
  secret_key: "서로-다른-충분히-긴-랜덤-시크릿"
  debug: false
security:
  api_key: "에이전트와-공유할-충분히-긴-랜덤-키"
  initial_allowed_ips: ["127.0.0.1", "192.168.1.0/24"]
database:
  path: "hub_data.db"
```

키는 `python -c "import secrets; print(secrets.token_hex(32))"`로 생성할 수 있습니다. 예시 IP 대역은 실제 관리자 PC의 IP/CIDR로 변경하세요. `initial_allowed_ips`는 최초 DB 초기화에만 반영되며 이후에는 웹 설정의 DB 값이 우선합니다. 빈 허용 목록은 모든 웹 접속을 허용합니다.

```powershell
.\hub\.venv\Scripts\python.exe hub/hub.py
```

브라우저에서 `http://<Hub-IP>:5000`에 접속합니다. Hub 방화벽에서 필요한 사내 PC의 TCP 5000 접근을 허용해야 합니다. 에이전트에는 수신 포트를 열 필요가 없습니다.

Hub 실행 예시는 Flask 내장 서버입니다. 상시 운영에는 적절한 WSGI 서버 및 HTTPS 구성이 필요하며 WSGI로 실행할 때도 `init_db()`를 먼저 호출해야 합니다. 웹 UI에는 별도 사용자 로그인이 없으므로 접근 허용 목록을 설정하세요. 임의의 `X-Forwarded-For`는 신뢰하지 않습니다. 리버스 프록시를 쓰는 경우 원래 클라이언트 IP 제한은 프록시에서도 적용해야 합니다.

## 2. Windows Agent EXE 빌드

**빌드 PC에만 Python이 필요합니다. 배포 대상 PC에는 Python/pip 설치가 필요하지 않습니다.** 내부적으로 Python 런타임과 라이브러리를 묶는 PyInstaller 방식입니다. Windows EXE는 Windows에서 빌드하세요. 권장 빌드 환경은 Windows x64 + Python 3.12 x64입니다.

저장소 루트의 PowerShell에서:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\agent\build.ps1
```

스크립트는 `agent/.venv-build`에 의존성을 설치하고 다음 결과를 생성합니다.

```text
agent/dist/
  ResourceMonitorAgent.exe
  config.yaml
```

EXE 한 파일에 실행 런타임을 포함하며, 변경 가능한 설정은 별도의 `config.yaml`로 배포합니다. 기존 `dist/config.yaml`은 재빌드 시 덮어쓰지 않습니다. 실제 API 키가 포함된 설정 파일은 공개 배포하거나 Git에 올리지 마세요.

PyInstaller의 [onefile 빌드](https://pyinstaller.org/en/stable/usage.html) 및 [실행 경로 규칙](https://pyinstaller.org/en/stable/runtime-information.html)에 따라 설정은 임시 압축 해제 폴더가 아닌 EXE 옆에서 읽습니다.

## 3. 원격 PC에서 실행 (Python 불필요)

위 두 파일을 대상 PC의 예: `C:\ResourceMonitorAgent`에 복사하고 `config.yaml`을 수정합니다.

```yaml
hub:
  url: "http://192.168.1.100:5000"
  api_key: "Hub의 security.api_key와 같은 값"
agent:
  hostname: ""  # 자동 컴퓨터 이름. 이름이 중복되면 PC마다 고유하게 지정
  report_interval_seconds: 30
```

Hub 주소에는 **에이전트에서 접근 가능한 Hub IP**를 사용하세요. 원격 PC에서 localhost를 지정하면 그 PC 자신에게 전송됩니다.

먼저 한 번 전송하여 점검합니다.

```powershell
cd C:\ResourceMonitorAgent
.\ResourceMonitorAgent.exe --once
$LASTEXITCODE  # 0: 전송 성공, 1: 설정/수집/전송 실패
```

계속 실행하려면:

```powershell
.\ResourceMonitorAgent.exe
# 종료: Ctrl+C
```

더블클릭도 가능하며 콘솔 창을 닫으면 에이전트도 종료됩니다. 다른 경로의 설정이나 로그 폴더를 지정할 수 있습니다.

```powershell
.\ResourceMonitorAgent.exe --config "D:\Monitor\config.yaml" --log-dir "D:\Monitor\logs"
```

- 기본 설정: EXE와 같은 폴더의 `config.yaml` (현재 작업 디렉터리와 무관)
- 기본 로그: `%LOCALAPPDATA%\ResourceMonitorAgent\agent.log`, 5 MiB씩 최대 백업 3개
- 설정 파일 변경 후 에이전트를 재시작합니다. 프로그램 목록은 Hub에서 5분마다 갱신됩니다.
- 전송 실패 시 다음 주기에 재시도하며, 전송하지 못한 과거 샘플을 재전송하는 큐는 없습니다.
- 기본 주기는 수집/설치 확인/통신 시간에 더해 30초 대기합니다. 작업이 느리면 Hub 오프라인 타임아웃을 120~180초 등으로 늘리세요.
- NVIDIA GPU 측정에는 대상 PC의 NVIDIA 드라이버가 필요합니다. Python 설치와는 무관합니다.

### 로그인 시 자동 실행

Windows 작업 스케줄러에서 '작업 만들기'를 사용합니다.

1. 트리거: 해당 사용자가 로그온할 때
2. 동작: 프로그램 시작, `C:\ResourceMonitorAgent\ResourceMonitorAgent.exe`
3. 인수: `--config "C:\ResourceMonitorAgent\config.yaml"`
4. 시작 위치: `C:\ResourceMonitorAgent`
5. 설정: 실패 시 다시 시작, 이미 실행 중이면 새 인스턴스를 시작하지 않음. 장시간 실행 중지 제한 해제.

일반 사용자로 먼저 실행하세요. 필요 권한 및 로그 경로는 실행 계정에 따라 달라집니다. 이 EXE는 Windows 서비스 프로토콜을 구현한 서비스가 아닙니다. 이전의 `python -m win32serviceutil install ResourceMonitorAgent` 명령은 사용할 수 없습니다.

## 소스 실행 / Linux

Windows에서도 개발 시 소스로 실행할 수 있습니다.

```powershell
python -X utf8 -m pip install -r agent/requirements.txt
python agent/agent.py --config agent/config.yaml
```

Linux에서는 Python 3.10 이상과 같은 의존성을 설치하고 소스로 실행합니다. Windows EXE는 Linux에서 실행되지 않습니다.

```ini
# /etc/systemd/system/resource-monitor-agent.service
[Unit]
Description=Resource Monitor Agent
After=network-online.target
Wants=network-online.target

[Service]
User=monitor
ExecStart=/opt/resource-monitor/.venv/bin/python /opt/resource-monitor/agent/agent.py --config /opt/resource-monitor/agent/config.yaml --log-dir /var/lib/resource-monitor
Restart=always
RestartSec=10
StateDirectory=resource-monitor

[Install]
WantedBy=multi-user.target
```

실행 계정 `monitor`, 설치 경로, 가상환경은 미리 준비한 값으로 바꾸고 `sudo systemctl enable --now resource-monitor-agent`로 등록합니다.

## 보안 및 문제 해결

- Hub와 Agent의 API 키가 다르거나 기본 예제 키이면 인증에 실패합니다. 빈 키도 허용하지 않습니다.
- 사내망 밖으로 통신한다면 HTTPS를 사용하세요. 기본 HTTP 예시는 암호화되지 않습니다.
- 설치 확인 명령은 허용된 실행 파일과 `--version` / `-V` / `-version` 인수 하나만 허용합니다. 임의 스크립트/셸 명령은 지원하지 않습니다.
- EXE 실행 직후 종료: PowerShell에서 실행해 오류를 확인합니다. 설정 파일 누락, 키 미변경, 잘못된 YAML을 우선 확인하세요.
- 연결 실패: Hub 실행 여부, IP/포트, 방화벽을 확인합니다.
- PC가 덮어써짐: 동일 hostname이 같은 PC로 저장됩니다. `agent.hostname`을 고유하게 지정하세요.
- GPU가 안 보임: NVIDIA 드라이버와 `nvidia-smi` 실행 가능 여부를 확인합니다.

## 개발 검증

저장소 루트에서 (EXE 빌드 가상환경을 재사용):

```powershell
.\agent\.venv-build\Scripts\python.exe -X utf8 -m pip install -r hub/requirements.txt
.\agent\.venv-build\Scripts\python.exe -m unittest discover -s tests -v
```

실제 사내망 검증은 서로 다른 PC 두 대 이상에서 EXE를 실행하여 대시보드 갱신을 확인하고, 한 에이전트를 종료한 뒤 타임아웃 이후 오프라인으로 바뀌는지 확인하세요. 실제 GPU 및 원격 로그인 감지는 해당 하드웨어·운영체제·권한에서 별도로 확인해야 합니다.

### 이번 점검 결과

2026-10-13 Windows / Python 3.12 환경에서 테스트 10개 통과, PyInstaller EXE 빌드 및 실행 확인.
EXE 통합 테스트는 Python을 PATH에서 제외하고 다른 작업 디렉터리에서 실행하여, 로컬 Hub에 두 hostname으로 실제 CPU/RAM 수집 데이터를 전송하고 DB 저장·상세 페이지 응답을 검증합니다. 물리적으로 다른 PC 두 대를 사용한 테스트는 아닙니다. EXE가 없거나 Windows가 아니면 해당 통합 테스트는 건너뜁니다.

수정한 주요 문제:
- EXE 실행 시 외부 설정 경로 및 로그 저장 경로
- ISO 시각과 SQLite 시각 문자열 혼용으로 발생하던 1시간 조회·24시간 정리 오류 (신규 기록은 Hub 수신 시각 기준)
- GPU 수집 누락 시 히스토리 배열의 시점 불일치
- 로컬 콘솔의 원격 접속 오탐 및 프로세스 부분 일치 오탐
- 초기 IP 허용 목록 미반영, 전달 헤더 위조에 의한 접근 제한 우회, 빈/예제 API 키 허용
- 설치 확인 명령의 임의 인수 허용
- 구현되지 않은 Windows 서비스 등록 안내
