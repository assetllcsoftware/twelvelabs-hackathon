-- Track the S3 prefix Bedrock writes async output under. start_clip_embed
-- stamps this with the value it passed to s3OutputDataConfig.s3Uri so the
-- finalize Lambda can look up the parent videos row from an output.json
-- ObjectCreated event without parsing invocationArns.

ALTER TABLE videos
    ADD COLUMN IF NOT EXISTS output_prefix text;

CREATE INDEX IF NOT EXISTS videos_output_prefix_idx
    ON videos (output_prefix);
