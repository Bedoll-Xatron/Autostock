-- ============================================================
-- watch_list 테이블 — 시그널 정보 컬럼 추가
-- Supabase SQL Editor에서 실행
-- ============================================================

ALTER TABLE watch_list
  ADD COLUMN IF NOT EXISTS signal_score    NUMERIC,      -- 전략 원점수 (최고값)
  ADD COLUMN IF NOT EXISTS strategies      TEXT,         -- 트리거 전략+점수 "vcp:82,flow:9"
  ADD COLUMN IF NOT EXISTS strategy_count  INTEGER DEFAULT 1, -- 동시 포착 전략 수
  ADD COLUMN IF NOT EXISTS foreign_5d      BIGINT,       -- 외국인 5일 순매수
  ADD COLUMN IF NOT EXISTS inst_5d         BIGINT,       -- 기관 5일 순매수
  ADD COLUMN IF NOT EXISTS signal_strength TEXT,         -- 수급강도 텍스트 (강력매수 등)
  ADD COLUMN IF NOT EXISTS theme           TEXT,         -- 테마 (narrative)
  ADD COLUMN IF NOT EXISTS rotation_phase  TEXT,         -- 섹터 국면 (sector)
  ADD COLUMN IF NOT EXISTS change_pct      NUMERIC;      -- 최근 등락률 (%)

-- 점수 기반 정렬 인덱스
CREATE INDEX IF NOT EXISTS idx_watch_list_signal_score
  ON watch_list (signal_score DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_watch_list_strategy_count
  ON watch_list (strategy_count DESC NULLS LAST);

COMMENT ON COLUMN watch_list.signal_score    IS '전략별 원점수 중 최고값 (VCP 0-100, flow/sector/contrarian/narrative 0-10)';
COMMENT ON COLUMN watch_list.strategies      IS '트리거된 전략과 점수 목록, 쉼표 구분 (예: vcp:82,flow:9)';
COMMENT ON COLUMN watch_list.strategy_count  IS '동시에 포착한 전략 수 — 높을수록 멀티컨펌 강도 높음';
COMMENT ON COLUMN watch_list.foreign_5d      IS '외국인 5일 누적 순매수 (수량)';
COMMENT ON COLUMN watch_list.inst_5d         IS '기관 5일 누적 순매수 (수량)';
COMMENT ON COLUMN watch_list.signal_strength IS 'flow 전략의 수급강도 텍스트 (강력매수/매수/중립 등)';
COMMENT ON COLUMN watch_list.theme           IS 'narrative 전략의 대표 테마';
COMMENT ON COLUMN watch_list.rotation_phase  IS '섹터 로테이션 국면 (선도/추격/과열/후퇴 등)';
COMMENT ON COLUMN watch_list.change_pct      IS '최근 영업일 등락률 (%)';
