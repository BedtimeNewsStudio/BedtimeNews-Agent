BEGIN;
CREATE TABLE IF NOT EXISTS rag.transcripts (
    doc_id VARCHAR(500) PRIMARY KEY,
    canonical_title VARCHAR(255) NOT NULL,
    source_title TEXT NOT NULL,
    channel VARCHAR(100) NOT NULL,
    publication_date VARCHAR(100),
    body_html TEXT NOT NULL,
    source_hash VARCHAR(64) NOT NULL,
    projection_version INTEGER NOT NULL DEFAULT 2,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE rag.transcripts
    ADD COLUMN IF NOT EXISTS projection_version INTEGER;
UPDATE rag.transcripts
SET projection_version = 0
WHERE projection_version IS NULL;
ALTER TABLE rag.transcripts
    ALTER COLUMN projection_version SET DEFAULT 2,
    ALTER COLUMN projection_version SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_transcripts_channel ON rag.transcripts(channel);
CREATE INDEX IF NOT EXISTS idx_transcripts_source_hash ON rag.transcripts(source_hash);
COMMIT;
