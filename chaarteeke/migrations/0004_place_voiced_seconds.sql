-- The voiced seconds the app measured in the place recording (20 ms frames above policy
-- calls.speech_frame_rms_min), kept for review. Asterisk's own talking time is whole seconds and is not stored.
ALTER TABLE place_captures ADD COLUMN voiced_s REAL;
INSERT INTO schema_version VALUES (4);
