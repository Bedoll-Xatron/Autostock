-- Migration: add entry_price and phase columns to held_positions
-- Run once on Supabase SQL editor

ALTER TABLE held_positions
    ADD COLUMN IF NOT EXISTS entry_price NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS phase       TEXT NOT NULL DEFAULT 'stop';

-- Backfill entry_price from avg_price for existing rows
UPDATE held_positions
SET entry_price = avg_price
WHERE entry_price IS NULL;
