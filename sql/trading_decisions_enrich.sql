-- trading_decisions 테이블 — 분석 정보 컬럼 추가
-- Supabase SQL Editor에서 실행

ALTER TABLE trading_decisions
  -- FinalDecision (현재 누락)
  ADD COLUMN IF NOT EXISTS bull_summary    TEXT,
  ADD COLUMN IF NOT EXISTS bear_summary    TEXT,
  -- Bull / Bear 점수
  ADD COLUMN IF NOT EXISTS bull_score      NUMERIC,
  ADD COLUMN IF NOT EXISTS bear_score      NUMERIC,
  -- 기술적 분석
  ADD COLUMN IF NOT EXISTS rsi             NUMERIC,
  ADD COLUMN IF NOT EXISTS trend           TEXT,
  ADD COLUMN IF NOT EXISTS macd_signal     TEXT,
  -- 기본적 분석
  ADD COLUMN IF NOT EXISTS per             NUMERIC,
  ADD COLUMN IF NOT EXISTS pbr             NUMERIC,
  ADD COLUMN IF NOT EXISTS roe             NUMERIC,
  ADD COLUMN IF NOT EXISTS valuation       TEXT,
  -- 감성 분석
  ADD COLUMN IF NOT EXISTS news_score      NUMERIC,
  ADD COLUMN IF NOT EXISTS foreign_net     TEXT,
  ADD COLUMN IF NOT EXISTS inst_net        TEXT,
  -- MarketFlow 시그널
  ADD COLUMN IF NOT EXISTS signal_score    NUMERIC,
  ADD COLUMN IF NOT EXISTS strategies      TEXT,
  ADD COLUMN IF NOT EXISTS strategy_count  INTEGER,
  ADD COLUMN IF NOT EXISTS signal_strength TEXT,
  ADD COLUMN IF NOT EXISTS theme           TEXT;

COMMENT ON COLUMN trading_decisions.bull_score     IS 'Bull 에이전트 매수 강도 점수 (0~10)';
COMMENT ON COLUMN trading_decisions.bear_score     IS 'Bear 에이전트 매도 강도 점수 (0~10)';
COMMENT ON COLUMN trading_decisions.rsi            IS 'RSI 값 (0~100)';
COMMENT ON COLUMN trading_decisions.trend          IS '상승/하락/횡보';
COMMENT ON COLUMN trading_decisions.macd_signal    IS 'MACD 시그널 설명';
COMMENT ON COLUMN trading_decisions.valuation      IS '저평가/고평가/적정';
COMMENT ON COLUMN trading_decisions.news_score     IS '뉴스 감성 점수 (0~10)';
COMMENT ON COLUMN trading_decisions.signal_score   IS 'MarketFlow 전략 원점수 (최고값)';
COMMENT ON COLUMN trading_decisions.strategies     IS '트리거 전략 목록 (예: vcp:82,flow:9)';
COMMENT ON COLUMN trading_decisions.strategy_count IS '동시 포착 전략 수';
