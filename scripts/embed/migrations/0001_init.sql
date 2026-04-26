-- Energy Infrastructure Health Platform — initial schema for video search.
--
-- Single `embeddings` table holds rows for both clip-level and frame-level
-- vectors. The `kind` column distinguishes them; the unified table keeps the
-- HNSW index simple and lets us join clip + frame ranking in one query.
--
-- Notes on portability:
--   * Designed to run under Postgres 16 (which is what RDS provisions).
--   * Idempotent — every CREATE uses IF NOT EXISTS so re-running on a fresh
--     or already-migrated DB is safe.
--   * Vectors are stored L2-normalized so the `<=>` cosine-distance operator
--     and the in-memory `matrix @ q` ranking agree to floating point.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS videos (
    s3_key          text        PRIMARY KEY,
    bucket          text        NOT NULL,
    bytes           bigint,
    invocation_arn  text,
    model_id        text        NOT NULL,
    -- pending: nothing embedded yet
    -- clips_ready: clip output.json processed
    -- frames_ready: frame extractor finished
    -- ready: both pipelines done
    status          text        NOT NULL DEFAULT 'pending',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS embeddings (
    id                bigserial   PRIMARY KEY,
    s3_key            text        NOT NULL REFERENCES videos(s3_key) ON DELETE CASCADE,
    kind              text        NOT NULL CHECK (kind IN ('clip', 'frame')),
    embedding_option  text        NOT NULL,                 -- visual | audio | transcription | frame
    segment_index     integer,                              -- non-null for kind='clip'
    frame_index       integer,                              -- non-null for kind='frame'
    start_sec         double precision NOT NULL,
    end_sec           double precision NOT NULL,
    timestamp_sec     double precision NOT NULL,
    thumb_s3_key      text,                                 -- non-null for kind='frame'
    embedding         vector(512) NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- Idempotent upsert key. COALESCE keeps the index well-defined when one of
-- (segment_index, frame_index) is NULL, which always happens by construction.
CREATE UNIQUE INDEX IF NOT EXISTS embeddings_natural_key_idx
    ON embeddings (
        s3_key,
        kind,
        embedding_option,
        COALESCE(segment_index, -1),
        COALESCE(frame_index, -1)
    );

CREATE INDEX IF NOT EXISTS embeddings_s3_key_idx
    ON embeddings (s3_key);

CREATE INDEX IF NOT EXISTS embeddings_kind_idx
    ON embeddings (kind);

-- HNSW for cosine ANN. m / ef_construction tuned for our small corpus; the
-- pgvector defaults are fine for >100k rows but the parameter group already
-- raises maintenance_work_mem so building this is cheap on RDS.
CREATE INDEX IF NOT EXISTS embeddings_hnsw_cosine_idx
    ON embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Touch updated_at on every UPDATE to videos so we can see pipeline progress.
CREATE OR REPLACE FUNCTION touch_videos_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS videos_touch_updated_at ON videos;
CREATE TRIGGER videos_touch_updated_at
    BEFORE UPDATE ON videos
    FOR EACH ROW EXECUTE FUNCTION touch_videos_updated_at();
