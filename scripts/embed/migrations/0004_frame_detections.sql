-- Phase D.6 — per-frame YOLO instance-segmentation detections.
--
-- Mirrors what the local pld-yolo project produces (Ultralytics yolo*-seg):
-- one row per detected instance per (s3_key, frame_index, model_name).
--
-- Geometry is stored normalized to [0, 1] against the source frame's pixel
-- size, so the portal can render polygons over thumbnails of any display
-- size with one ``<svg viewBox="0 0 1 1">``.
--
-- We deliberately keep this in its own table (vs. squeezing into
-- ``embeddings``) — detections are not searchable vectors and have a much
-- larger payload (polygons of up to ~50 vertices each).

CREATE TABLE IF NOT EXISTS frame_detections (
    id            bigserial    PRIMARY KEY,
    s3_key        text         NOT NULL REFERENCES videos(s3_key) ON DELETE CASCADE,
    frame_index   integer      NOT NULL,
    timestamp_sec double precision NOT NULL,
    thumb_s3_key  text         NOT NULL,                 -- the JPEG that ran through YOLO
    model_name    text         NOT NULL,                 -- 'pldm-power-line', 'airpelago-insulator-pole', ...
    model_version text         NOT NULL DEFAULT 'v1',
    class_id      integer      NOT NULL,
    class_name    text         NOT NULL,                 -- 'power_line', 'insulator', 'pole', ...
    confidence    real         NOT NULL,
    bbox_xyxy     real[]       NOT NULL,                 -- [x1, y1, x2, y2] in [0, 1]
    polygon_xy    real[]       NOT NULL,                 -- flat [x0,y0,x1,y1,...] in [0, 1]
    created_at    timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS frame_detections_lookup_idx
    ON frame_detections (s3_key, frame_index);

CREATE INDEX IF NOT EXISTS frame_detections_class_idx
    ON frame_detections (s3_key, class_name);

CREATE INDEX IF NOT EXISTS frame_detections_model_idx
    ON frame_detections (s3_key, model_name);

-- Idempotency: re-running the worker for the same (video, frame, model)
-- replaces all old detections for that triple. We do this in two steps:
--   DELETE FROM frame_detections WHERE (s3_key, frame_index, model_name) = (...);
--   INSERT ... one row per detection;
-- so we don't need a UNIQUE index here.
