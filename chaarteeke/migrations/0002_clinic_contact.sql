-- Each clinic has its own visit time (spoken in reminders) and a phone number (spoken when it is out
-- of vaccine). Both are required for calls that use them; a clinic without one cannot be called for.
ALTER TABLE clinics ADD COLUMN phone TEXT;
ALTER TABLE clinics ADD COLUMN visit_time TEXT;
INSERT INTO schema_version VALUES (2);
