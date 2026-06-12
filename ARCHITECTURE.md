# bedoll AutoStock — 시스템 아키텍처 (최종 업데이트: 2026-05-08)

## 개요

한국투자증권(KIS) API 기반 자동매매 시스템.  
LangGraph 멀티에이전트가 종목을 분석하고, Telegram HITL(Human-In-The-Loop)로 사람이 최종 승인합니다.  
**단일 프로세스** (FastAPI + Telegram Bot + APScheduler + 트레일링 손절 감시) 구성.

---

## 전체 흐름

```
[데몬 워치독 — --daemon 모드]
    서버 비정상 종료 시 5초 후 자동 재시작 (STOP_SENTINEL 파일로 정지)
    │
    ▼
APScheduler (평일 + 공휴일 제외)
    ├─ 08:30 KST — 오전 매매 파이프라인
    ├─ 13:00 KST — 오후 급부각 스캔
    ├─ 16:00 KST — MarketFlow 전략 스캔 + watchlist 갱신
    └─ 17:00 KST — 성과 추적 (d5/d10 수익률)
    │
    ▼
run_daily_pipeline()
    │
    ├─ [공휴일 가드] is_trading_day() → False면 즉시 return
    ├─ [DANGER 가드] 거시지표 DANGER → 관망
    ├─ [BEAR 가드] KODEX200 추세 약세 → 관망
    │
    ▼
DB — Supabase watch_list + market_daily 조회
    │
    ▼
LangGraph 파이프라인
    │
    ├─ supervisor_node → screening_agent (상위 N종목 선택)
    │        └─ Send API 병렬: technical / fundamental / sentiment 에이전트
    │                └─ reflection_node (품질 검토)
    │                        ├─ 통과 → bull_node ↔ bear_node (디베이트)
    │                        └─ 반려 → retry_research
    │                                   └─ supervisor_node (최종 결정)
    │
    ▼
HITL — human_review_node (interrupt)
    ├─ Telegram: 매매결정 카드 + BUY수량 입력 + 승인/거절 버튼
    └─ 모의투자(KIS_SIMULATED_MODE=true): HITL 없이 AI 결정 자동 실행
    │
    ▼
FastAPI /hitl-response → resume_graph()
    │
    ├─ approved → KIS API 주문 실행 → Supabase 저장 → add_positions() 큐 주입
    └─ rejected → Supabase 저장 + Telegram 알림
    │
    ▼
[싱글턴 트레일링 손절 감시 — watch_trailing_stops()]
    - 서버 시작 시 1회 기동, 절대 종료하지 않음
    - add_positions()로 신규 포지션 즉시 주입 (서버 재시작 불필요)
    - 5분마다 Supabase DB 재동기화 (외부 매수 자동 감지)
    - 장 마감(15:20) 후 held_positions 저장, 다음 날 재개
    - 주말/공휴일: _is_trading_day_kst() → False → 폴링 일시 중지
```

---

## 디렉토리 구조

```
autostock/
├── .env                            # 실제 인증정보 (절대 공개 금지)
├── .gitignore
├── requirements.txt
├── ARCHITECTURE.md                 # 이 파일
├── main.py                         # 진입점 + 서버 관리 (start/stop/restart/status/daemon)
├── manual_buy.py                   # 수동 매수 스크립트 (test/debug용)
├── start_server.bat                # 서버 시작 배치 (--restart)
│
├── autostock/
│   ├── config.py                   # 환경변수 로드 및 설정 상수
│   ├── models.py                   # Pydantic 스키마 + TradingState TypedDict
│   ├── logger.py                   # 로거 설정
│   │
│   ├── market/
│   │   ├── fetcher.py              # OHLCV, 기초데이터 수집 (FinanceDataReader, pykrx)
│   │   ├── analyzer.py             # 기술지표 계산 (RSI, MACD, 손절가)
│   │   ├── market_regime.py        # KODEX200 MA50/MA200 시장 국면 감지
│   │   ├── us_market.py            # 미국 시장 지표 수집 (VIX, Fear&Greed)
│   │   └── kr_holidays.py          # 한국 공휴일 조회 (data.go.kr API + 정적 fallback)
│   │
│   ├── db/
│   │   └── supabase.py             # Supabase 클라이언트
│   │
│   ├── research/
│   │   ├── state.py                # TradingState 초기화
│   │   ├── tools.py                # LangChain @tool
│   │   ├── agents.py               # screening/technical/fundamental/sentiment 에이전트
│   │   ├── reflection.py           # reflection_node
│   │   ├── debate.py               # bull_node ↔ bear_node
│   │   ├── supervisor.py           # supervisor_node
│   │   ├── hitl_node.py            # human_review_node (interrupt)
│   │   ├── graph.py                # LangGraph StateGraph 조립
│   │   └── runner.py               # 그래프 실행 + interrupt 처리
│   │
│   ├── hitl/
│   │   ├── telegram_bot.py         # Telegram Bot — HITL 버튼, 워치리스트 관리
│   │   └── hitl_state.py           # asyncio Event — Bot ↔ FastAPI 브릿지
│   │
│   ├── api/
│   │   ├── app.py                  # FastAPI 앱 인스턴스
│   │   ├── routes.py               # /hitl-response, /health
│   │   └── schemas.py              # 요청/응답 Pydantic 스키마
│   │
│   ├── trading/
│   │   ├── kis_client.py           # KIS REST API (토큰, 잔고, 시세, 주문)
│   │   ├── executor.py             # 매매 주문 실행 + 수량 계산
│   │   ├── trailing_stop.py        # 트레일링 손절 감시 싱글턴
│   │   ├── limit_order.py          # 지정가 주문 헬퍼
│   │   └── performance_tracker.py  # d5/d10 수익률 추적
│   │
│   └── scheduler/
│       └── jobs.py                 # APScheduler 일일 작업 정의
│
├── marketflow/
│   ├── daily_update.py             # MarketFlow 전략 일일 실행 진입점
│   ├── data/                       # 전략별 JSON 결과 저장소
│   └── engine/
│       ├── config.py               # MarketFlow 설정 (임계값 등)
│       ├── collectors.py           # pykrx 데이터 수집
│       ├── scorer.py               # 종목 스코어링
│       ├── generator.py            # 워치리스트 생성
│       ├── models.py               # 전략 데이터 모델
│       ├── notifier.py             # Telegram 알림
│       ├── vcp_detector.py         # VCP(변동성 수축) 탐지
│       ├── flow_momentum.py        # 수급 모멘텀 전략
│       ├── sector_rotation.py      # 섹터 로테이션 전략
│       ├── narrative_momentum.py   # 내러티브 모멘텀 전략
│       ├── contrarian_reversal.py  # 역추세 반전 전략
│       └── run_engine.py           # 전략 통합 실행
│
├── sql/                            # Supabase 테이블 DDL
└── scratch/                        # 개발/테스트 스크립트
```

---

## 핵심 컴포넌트

### 1. 서버 프로세스 구조 (main.py)

```
python main.py              # 데몬 백그라운드 시작
python main.py --restart    # 기존 종료 + 데몬 재시작
python main.py --stop       # 완전 정지 (sentinel 파일 생성)
python main.py --status     # 상태 확인
```

**데몬 워치독 패턴:**
- `--daemon` 모드: 서버(`--serve`) 프로세스를 감시, 비정상 종료 시 5초 후 자동 재시작
- `logs/daemon.pid` — 데몬 PID 추적
- `logs/server.pid` — 서버 PID 추적
- `logs/.stop` — 이 파일이 있으면 데몬이 재시작하지 않음 (`--stop` 시 생성)
- `logs/server.log` — 서버 stdout/stderr 전체 기록

### 2. 트레일링 손절 (trailing_stop.py)

3단계 손절 전략:
| 단계 | 조건 | 손절가 |
|------|------|--------|
| Phase 1 (stop)  | 수익률 < +2%      | 진입가 × 0.97 (고정 -3%) |
| Phase 2 (even)  | 수익률 ≥ +2%      | 진입가 (본전 보호) |
| Phase 3 (trail) | 수익률 ≥ +5%      | peak × 0.975 (trailing -2.5%) |

**싱글턴 감시 루프:**
- 서버 시작 시 1회만 `asyncio.create_task(watch_trailing_stops())` 호출
- 포지션 없어도 루프 유지 — 큐/DB 대기
- 신규 포지션: `add_positions()` → `asyncio.Queue` 즉시 주입 (서버 재시작 불필요)
- 5분마다 Supabase 재동기화 (manual_buy.py 등 외부 매수 자동 감지)
- Time Stop: 5일 경과 + 수익률 < 1% → 자동 손절

### 3. 한국 공휴일 (kr_holidays.py)

- **API**: data.go.kr 한국천문연구원 특일정보 (`HOLIDAY_API_KEY`)
- **임시공휴일·선거일** 포함 (공식 정부 데이터)
- **캐시**: 메모리 → `logs/holidays_{year}.json` → API → 정적 fallback 순
- 적용 위치:
  - `trailing_stop._is_trading_day_kst()` — 장 시간 폴링 제어
  - `jobs.is_trading_day()` — 모든 스케줄 작업 진입 시 가드

### 4. LangGraph 파이프라인 노드

| 노드 | 역할 |
|------|------|
| `supervisor_node` | CEO — 스크리닝/반려재지시/최종결정 3가지 케이스 처리 |
| `screening_agent` | 워치리스트 중 분석 대상 상위 MAX_ANALYSIS_STOCKS개 선택 |
| `technical_agent` | RSI, MACD, 추세, 손절가 기술 분석 |
| `fundamental_agent` | PER, PBR, ROE, 실적 기본 분석 |
| `sentiment_agent` | 뉴스 감성, 공시, 외국인/기관 수급 |
| `reflection_node` | 리서치 품질 검토 — REVIEW_COUNT 초과 시 강제 통과 |
| `bull_node` | 매수 논거 + Bear 반박 |
| `bear_node` | 매도 논거 + Bull 반박 |
| `human_review_node` | `interrupt()` — Telegram HITL 대기 |

### 5. HITL 흐름

1. `human_review_node` → `interrupt()` 그래프 정지
2. Telegram 결정 카드 전송 (AI 추천 수량 포함)
3. 사람이 **종목별 BUY 수량 입력** → 최종 승인 / 전체 거절
4. FastAPI `/hitl-response` → `resume_graph()` 재개
5. KIS 주문 → Supabase 저장 → `add_positions()` → Telegram 완료
6. **모의투자 모드** (`KIS_SIMULATED_MODE=true`): HITL 없이 AI 결정 즉시 자동 실행

### 6. Telegram Bot 명령어

| 명령어 | 설명 |
|--------|------|
| `/add 코드 이름 섹터 전일종가` | 워치리스트 추가 |
| `/remove 코드` | 워치리스트 제거 |
| `/watchlist` | 현재 목록 조회 |
| `/kis_mode` | 모의 ↔ 실거래 토글 |
| `/status` | 현재 설정 조회 |

### 7. 매수 수량 결정 로직

| 신뢰도 | 포지션 비율 | 시장 국면 CAUTION 시 |
|--------|-------------|---------------------|
| 9.0 이상 | 잔고의 15% | × 0.7 = 10.5% |
| 7.0 이상 | 잔고의 10% | × 0.7 = 7%    |
| 5.0 이상 | 잔고의 5%  | × 0.7 = 3.5%  |
| 미만     | 매매 보류   | —                  |

HITL 승인 시 AI 제안 수량 또는 직접 입력 수량 선택 가능.

---

## 환경변수 (.env)

| 변수 | 설명 |
|------|------|
| `AI_PROVIDER` | AI 제공자 (`gemini`) |
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `GEMINI_BASIC_MODEL` | 기본 모델 (gemini-2.5-flash) |
| `GEMINI_BOSS_MODEL` | 상위 모델 (gemini-2.5-pro) |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot 토큰 |
| `TELEGRAM_CHAT_ID` | 알림 채널 Chat ID |
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_KEY` | Supabase service role key |
| `KIS_APP_KEY` | 한투 앱키 |
| `KIS_APP_SECRET` | 한투 앱시크릿 |
| `KIS_CANO` | 계좌번호 (8자리) |
| `KIS_ACNT_PRDT_CD` | 계좌상품코드 (보통 01) |
| `KIS_SIMULATED_MODE` | `true`=모의투자, `false`=실거래 |
| `krx_API_KEY` | KRX 데이터 API 키 |
| `TAVILY_API_KEY` | Tavily 뉴스 검색 API 키 |
| `API_HOST` | FastAPI 바인딩 주소 (0.0.0.0) |
| `API_PORT` | FastAPI 포트 (8000) |
| `PIPELINE_SCHEDULES` | 파이프라인 실행 시각 (08:30) |
| `AFTERNOON_SCANNER_HOUR` | 오후 스캔 시 (13) |
| `AFTERNOON_SCANNER_MINUTE` | 오후 스캔 분 (0) |
| `MARKETFLOW_HOUR` | MarketFlow 스캔 시 (16) |
| `MARKETFLOW_MINUTE` | MarketFlow 스캔 분 (0) |
| `REVIEW_COUNT` | AI 검토 횟수 (기본 3) |
| `HITL_TIMEOUT_MINUTES` | HITL 타임아웃 분 (기본 20) |
| `MAX_ANALYSIS_STOCKS` | 최대 분석 종목 수 (기본 5) |
| `HOLIDAY_API_KEY` | data.go.kr 한국천문연구원 특일정보 API 키 |

---

## Supabase 테이블

### watch_list
```sql
CREATE TABLE watch_list (
    id         BIGSERIAL PRIMARY KEY,
    ticker     TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    sector     TEXT,
    prev_close NUMERIC,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### market_daily
```sql
CREATE TABLE market_daily (
    id                BIGSERIAL PRIMARY KEY,
    date              DATE NOT NULL,
    fear_greed_score  NUMERIC,
    fear_greed_rating TEXT,
    vix               NUMERIC,
    vix_movement      TEXT,
    kospi             NUMERIC,
    kospi_movement    TEXT,
    condition         TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
```

### trading_decisions
```sql
CREATE TABLE trading_decisions (
    id              BIGSERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,
    price_reference NUMERIC,
    stop_loss_price NUMERIC,
    confidence      NUMERIC,
    order_qty       INTEGER,
    hitl_result     TEXT,
    final_reason    TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### held_positions (트레일링 손절 감시용)
```sql
CREATE TABLE held_positions (
    ticker      TEXT PRIMARY KEY,
    name        TEXT,
    qty         INTEGER,
    avg_price   NUMERIC,
    entry_price NUMERIC,
    stop_price  NUMERIC,
    peak_price  NUMERIC,
    phase       TEXT DEFAULT 'stop',
    entry_date  DATE,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
```

SQL 파일 위치: `sql/` 디렉토리

---

## 신규 PC 이식 절차

### 1. Python 환경

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. .env 설정

`.env` 파일이 압축에 포함되어 있음.  
이식 전 확인 필요 항목:
- `KIS_SIMULATED_MODE=true` (처음엔 모의투자로 시작 권장)
- `KIS_CANO`, `KIS_APP_KEY`, `KIS_APP_SECRET` — 계좌 변경 시 수정

### 3. Supabase

기존 Supabase 프로젝트 재사용 가능 (URL/KEY 동일).  
새 프로젝트면 `sql/` 디렉토리의 DDL 파일 순서대로 실행:
1. `held_positions.sql`
2. `held_positions_add_entry_phase.sql`
3. `marketflow_tables.sql`
4. `trading_decisions_enrich.sql`
5. `watch_list_signal_columns.sql`

### 4. 서버 시작

```bash
python main.py          # 데몬 백그라운드 시작 (자동 재시작 포함)
# 또는
start_server.bat        # 더블클릭으로 시작
```

### 5. 공휴일 캐시 초기화

서버 첫 실행 시 `HOLIDAY_API_KEY`로 자동 조회.  
수동 갱신이 필요하면:
```python
from autostock.market.kr_holidays import refresh_cache
refresh_cache(2026)
```
