-- ============================================================
-- MarketFlow 전략 테이블 — Supabase에서 실행
-- ============================================================

-- 1. 종가베팅 V2 (jongga)
CREATE TABLE IF NOT EXISTS mf_jongga (
    id                      BIGSERIAL PRIMARY KEY,
    date                    DATE        NOT NULL,
    stock_code              TEXT        NOT NULL,
    stock_name              TEXT        NOT NULL,
    market                  TEXT,
    grade                   TEXT,
    -- 점수
    score_total             INTEGER,
    score_news              INTEGER,
    score_volume            INTEGER,
    score_chart             INTEGER,
    score_candle            INTEGER,
    score_consolidation     INTEGER,
    score_supply            INTEGER,
    score_retracement       INTEGER,
    score_pullback_support  INTEGER,
    llm_reason              TEXT,
    -- 가격/포지션
    current_price           NUMERIC,
    entry_price             NUMERIC,
    stop_price              NUMERIC,
    target_price            NUMERIC,
    quantity                INTEGER,
    position_size           NUMERIC,
    r_value                 NUMERIC,
    r_multiplier            NUMERIC,
    trading_value           NUMERIC,
    change_pct              NUMERIC,
    -- 수급
    foreign_5d              BIGINT,
    inst_5d                 BIGINT,
    quality                 NUMERIC,
    -- JSON 배열
    themes                  JSONB       DEFAULT '[]',
    news_items              JSONB       DEFAULT '[]',
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, stock_code)
);

-- 2. VCP 패턴
CREATE TABLE IF NOT EXISTS mf_vcp (
    id          BIGSERIAL PRIMARY KEY,
    date        DATE    NOT NULL,
    stock_code  TEXT    NOT NULL,
    stock_name  TEXT    NOT NULL,
    market      TEXT,
    grade       TEXT,
    score       NUMERIC,
    c1          NUMERIC,
    c2          NUMERIC,
    c3          NUMERIC,
    r12         NUMERIC,
    r23         NUMERIC,
    pivot_high  NUMERIC,
    foreign_5d  BIGINT,
    inst_5d     BIGINT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, stock_code)
);

-- 3. 수급 모멘텀 (flow momentum)
CREATE TABLE IF NOT EXISTS mf_flow (
    id                  BIGSERIAL PRIMARY KEY,
    date                DATE    NOT NULL,
    ticker              TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    market              TEXT,
    score               NUMERIC,
    flow_score          NUMERIC,
    trend_score         NUMERIC,
    vol_score           NUMERIC,
    foreign_flow        NUMERIC,
    institution_flow    NUMERIC,
    volume_ratio        NUMERIC,
    signal_strength     TEXT,
    price               NUMERIC,
    change_pct          NUMERIC,
    ma20                NUMERIC,
    ma60                NUMERIC,
    trend               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, ticker)
);

-- 4. 섹터 로테이션
CREATE TABLE IF NOT EXISTS mf_sector (
    id                  BIGSERIAL PRIMARY KEY,
    date                DATE    NOT NULL,
    ticker              TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    market              TEXT,
    score               NUMERIC,
    sector              TEXT,
    rotation_phase      TEXT,
    relative_strength   NUMERIC,
    rs_raw              NUMERIC,
    price               NUMERIC,
    ma20                NUMERIC,
    ma60                NUMERIC,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, ticker)
);

-- 5. 역발상 반전 (contrarian)
CREATE TABLE IF NOT EXISTS mf_contrarian (
    id                      BIGSERIAL PRIMARY KEY,
    date                    DATE    NOT NULL,
    ticker                  TEXT    NOT NULL,
    name                    TEXT    NOT NULL,
    market                  TEXT,
    score                   NUMERIC,
    oversold_score          NUMERIC,
    reversal_probability    NUMERIC,
    support_level           NUMERIC,
    rsi                     NUMERIC,
    price                   NUMERIC,
    change_pct              NUMERIC,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, ticker)
);

-- 6. 테마 모멘텀 (narrative)
CREATE TABLE IF NOT EXISTS mf_narrative (
    id                  BIGSERIAL PRIMARY KEY,
    date                DATE    NOT NULL,
    ticker              TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    market              TEXT,
    score               NUMERIC,
    theme               TEXT,
    news_sentiment      NUMERIC,
    sns_momentum        NUMERIC,
    narrative_score     NUMERIC,
    news_pts            NUMERIC,
    theme_pts           NUMERIC,
    vol_pts             NUMERIC,
    llm_source          TEXT,
    news_reason         TEXT,
    price               NUMERIC,
    change_pct          NUMERIC,
    all_themes          JSONB   DEFAULT '[]',
    theme_peers         JSONB   DEFAULT '[]',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, ticker)
);

-- ============================================================
-- 인덱스 (날짜별 조회 최적화)
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_mf_jongga_date    ON mf_jongga    (date DESC);
CREATE INDEX IF NOT EXISTS idx_mf_vcp_date       ON mf_vcp       (date DESC);
CREATE INDEX IF NOT EXISTS idx_mf_flow_date      ON mf_flow      (date DESC);
CREATE INDEX IF NOT EXISTS idx_mf_sector_date    ON mf_sector    (date DESC);
CREATE INDEX IF NOT EXISTS idx_mf_contrarian_date ON mf_contrarian (date DESC);
CREATE INDEX IF NOT EXISTS idx_mf_narrative_date ON mf_narrative  (date DESC);
