ALTER TABLE paper_accounts ADD COLUMN config TEXT NOT NULL DEFAULT '{}';
ALTER TABLE paper_positions ADD COLUMN last_checked_t INTEGER;
ALTER TABLE paper_positions ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
