-- Phase D.5 — per-clip Pegasus video-text descriptions.
--
-- One row per (s3_key, start_sec, end_sec, prompt_id). Frames inherit
-- the description of the clip whose [start_sec, end_sec] contains them
-- (resolved at search time, not stored).
--
-- prompt_id is intentionally tiny ('inspector', 'summary', ...) so the
-- portal can ask for "the inspector caption" without serializing the
-- full prompt text. The actual prompt is also stored for audit.

CREATE TABLE IF NOT EXISTS clip_descriptions (
    id            bigserial    PRIMARY KEY,
    s3_key        text         NOT NULL REFERENCES videos(s3_key) ON DELETE CASCADE,
    start_sec     double precision NOT NULL,
    end_sec       double precision NOT NULL,
    clip_s3_key   text,
    prompt_id     text         NOT NULL DEFAULT 'inspector',
    prompt        text         NOT NULL,
    message       text         NOT NULL,
    model_id      text         NOT NULL,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS clip_descriptions_natural_key_idx
    ON clip_descriptions (s3_key, start_sec, end_sec, prompt_id);

CREATE INDEX IF NOT EXISTS clip_descriptions_s3_key_idx
    ON clip_descriptions (s3_key);

CREATE OR REPLACE FUNCTION touch_clip_descriptions_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS clip_descriptions_touch_updated_at ON clip_descriptions;
CREATE TRIGGER clip_descriptions_touch_updated_at
    BEFORE UPDATE ON clip_descriptions
    FOR EACH ROW EXECUTE FUNCTION touch_clip_descriptions_updated_at();
