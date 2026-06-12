-- held_positions: 장 마감 후에도 보유 중인 종목 (트레일링 손절 재개용)
-- 서버 재시작 시 이 테이블에서 포지션을 복구해 트레일링 감시를 재개합니다.

CREATE TABLE IF NOT EXISTS held_positions (
    ticker          TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    qty             INTEGER NOT NULL,
    avg_price       NUMERIC(12, 2) NOT NULL,
    stop_price      NUMERIC(12, 2) NOT NULL,   -- 현재 적용 손절가
    peak_price      NUMERIC(12, 2) NOT NULL,   -- 감시 중 최고가
    trail_pct       NUMERIC(5, 2) NOT NULL DEFAULT 3.0,  -- 트레일링 비율(%)
    entry_date      DATE NOT NULL,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- updated_at 자동 갱신 트리거 (선택)
CREATE OR REPLACE FUNCTION update_held_positions_ts()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_held_positions_ts ON held_positions;
CREATE TRIGGER trg_held_positions_ts
    BEFORE UPDATE ON held_positions
    FOR EACH ROW EXECUTE FUNCTION update_held_positions_ts();
