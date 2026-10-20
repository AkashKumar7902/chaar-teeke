-- The audit log is append-only: the database itself refuses to change or remove a row, so no bug or
-- stray statement can rewrite who did what. Nothing in the app updates or deletes audit rows (the demo
-- reset leaves them in place). A new row is the only write.
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN
  SELECT RAISE(ABORT, 'the audit log is append-only');
END;

CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN
  SELECT RAISE(ABORT, 'the audit log is append-only');
END;

INSERT INTO schema_version VALUES (3);
