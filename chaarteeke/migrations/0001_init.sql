-- Chaar Teeke schema v1. Times: UTC ISO text '...Z'. Local dates: 'YYYY-MM-DD' in CT_LOCAL_TZ.
PRAGMA foreign_keys = ON;

CREATE TABLE clinics (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  short_name TEXT NOT NULL,
  ward_no INTEGER,
  default_regimen TEXT NOT NULL,
  default_lang TEXT NOT NULL CHECK (default_lang IN ('hi','doi','ur')),
  arv_status TEXT NOT NULL CHECK (arv_status IN ('confirmed','unverified')),
  vaccine_out INTEGER NOT NULL DEFAULT 0,
  vaccine_out_since TEXT,
  is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE staff (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('clinic','abc','vet','oversight','reviewer','admin')),
  clinic_id TEXT REFERENCES clinics(id),
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  staff_id TEXT REFERENCES staff(id),
  role TEXT NOT NULL,
  visitor_label TEXT,
  csrf TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE courses (
  id TEXT PRIMARY KEY,
  clinic_id TEXT NOT NULL REFERENCES clinics(id),
  register_no TEXT,
  patient_type TEXT NOT NULL DEFAULT 'new' CHECK (patient_type IN ('new','old')),
  regimen_id TEXT NOT NULL,
  day0 TEXT NOT NULL,
  lang TEXT NOT NULL CHECK (lang IN ('hi','doi','ur')),
  status TEXT NOT NULL CHECK (status IN ('awaiting_call','active','completed','stopped')),
  eligibility TEXT NOT NULL CHECK (eligibility IN ('adult','guardian_consented','unconfirmed')),
  consent_reminders_at TEXT,
  consent_questions TEXT CHECK (consent_questions IN ('yes','no')),
  notice_version TEXT,
  stopped_reason TEXT,
  source TEXT NOT NULL CHECK (source IN ('clinic','simulator','demo')),
  created_at TEXT NOT NULL,
  completed_at TEXT,
  is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX courses_clinic ON courses(clinic_id, status);

-- The phone table: separate, encrypted, access-logged, purged (DOMAIN R11, R13).
CREATE TABLE contacts (
  course_id TEXT PRIMARY KEY REFERENCES courses(id) ON DELETE CASCADE,
  phone_cipher TEXT NOT NULL,
  phone_last4 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  purge_after TEXT
);

CREATE TABLE purge_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id TEXT,
  what TEXT NOT NULL CHECK (what IN ('phone','clip','missed_call')),
  reason TEXT NOT NULL,
  purged_at TEXT NOT NULL
);

CREATE TABLE missed_calls (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  phone_cipher TEXT,
  phone_last4 TEXT,
  transport TEXT NOT NULL CHECK (transport IN ('sim','ari')),
  status TEXT NOT NULL CHECK (status IN ('unlinked','linked','duplicate','withdrawn','expired','withheld')),
  course_id TEXT REFERENCES courses(id),
  clinic_hint TEXT REFERENCES clinics(id),
  is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX missed_calls_status ON missed_calls(status, at);

CREATE TABLE doses (
  course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  dose_no INTEGER NOT NULL,
  offset_days INTEGER NOT NULL,
  due_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('due','given','late_minor','late_review','not_needed')),
  given_on TEXT,
  marked_by TEXT CHECK (marked_by IN ('clinic','patient')),
  PRIMARY KEY (course_id, dose_no)
);
CREATE INDEX doses_due ON doses(due_date, status);

CREATE TABLE calls (
  id TEXT PRIMARY KEY,
  course_id TEXT REFERENCES courses(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('first','reminder','missed_dose','duplicate_notice','withdrawn_notice')),
  job_key TEXT NOT NULL UNIQUE,
  dose_no INTEGER,
  scheduled_for TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('scheduled','dialing','ringing','answered','completed','no_answer','failed','cancelled')),
  transport TEXT,
  started_at TEXT,
  answered_at TEXT,
  ended_at TEXT,
  hangup_cause TEXT,
  seconds_listened REAL,
  error TEXT,
  is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX calls_due ON calls(status, scheduled_for);
CREATE INDEX calls_course ON calls(course_id);

CREATE TABLE answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  question TEXT NOT NULL CHECK (question IN ('lang','consent_reminders','consent_questions','dose_today','place','place_type','animal','notch','signs','dose_taken','withdraw')),
  value TEXT NOT NULL,
  at TEXT NOT NULL
);

CREATE TABLE bite_reports (
  id TEXT PRIMARY KEY,
  course_id TEXT UNIQUE REFERENCES courses(id) ON DELETE SET NULL,
  clinic_id TEXT NOT NULL REFERENCES clinics(id),
  bite_week TEXT NOT NULL,
  place_type TEXT NOT NULL DEFAULT 'no_answer' CHECK (place_type IN ('home_street','school','hospital','bus_market','other','no_answer')),
  animal TEXT NOT NULL DEFAULT 'no_answer' CHECK (animal IN ('notched','unnotched','unsure','pet','other','animal_unknown','no_answer')),
  signs TEXT NOT NULL DEFAULT 'no_answer' CHECK (signs IN ('yes','no','dont_know','no_answer')),
  ward_no INTEGER,
  outside_tehsil TEXT,
  location_status TEXT NOT NULL CHECK (location_status IN ('pending','matched','needs_review','decided','outside_jmc','cant_tell','no_place_heard','skipped')),
  cluster_id TEXT,
  source TEXT NOT NULL CHECK (source IN ('clinic','simulator','demo')),
  created_at TEXT NOT NULL,
  is_demo INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX bite_reports_week ON bite_reports(bite_week);
CREATE INDEX bite_reports_cluster ON bite_reports(cluster_id, bite_week);

CREATE TABLE place_captures (
  report_id TEXT PRIMARY KEY REFERENCES bite_reports(id) ON DELETE CASCADE,
  recording_name TEXT,
  audio_path TEXT,
  transcript TEXT,
  asr_model TEXT,
  asr_status TEXT NOT NULL CHECK (asr_status IN ('pending','done','off','failed','no_audio')),
  asr_error TEXT,
  candidates_json TEXT,
  confidence TEXT CHECK (confidence IN ('likely','possible','none')),
  decided_by TEXT,
  decided_at TEXT,
  clip_deleted_at TEXT,
  expires_at TEXT NOT NULL
);

CREATE TABLE aliases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text_norm TEXT NOT NULL,
  ward_no INTEGER NOT NULL,
  added_by TEXT NOT NULL,
  added_at TEXT NOT NULL,
  UNIQUE (text_norm, ward_no)
);

CREATE TABLE alerts (
  id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL REFERENCES bite_reports(id) ON DELETE CASCADE,
  ward_no INTEGER,
  cluster_id TEXT,
  place_type TEXT NOT NULL,
  local_day TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('new','claimed','acted','not_acted')),
  claimed_by TEXT,
  claimed_at TEXT,
  closed_at TEXT,
  outcome_note TEXT,
  first_seen_by_vet_at TEXT,
  created_at TEXT NOT NULL,
  is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE callback_requests (
  id TEXT PRIMARY KEY,
  alert_id TEXT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
  course_id TEXT REFERENCES courses(id) ON DELETE SET NULL,
  requested_by TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','done','declined','closed_no_contact')),
  done_at TEXT,
  note TEXT
);

CREATE TABLE worked_wards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ward_no INTEGER NOT NULL,
  worked_on TEXT NOT NULL,
  recorded_by TEXT NOT NULL,
  note TEXT,
  is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sight_counts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ward_no INTEGER NOT NULL,
  counted_on TEXT NOT NULL,
  notched INTEGER NOT NULL CHECK (notched >= 0),
  unnotched INTEGER NOT NULL CHECK (unnotched >= 0),
  unsure INTEGER NOT NULL CHECK (unsure >= 0),
  recorded_by TEXT NOT NULL,
  is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  object_type TEXT,
  object_id TEXT,
  detail_json TEXT
);

CREATE TABLE locks (
  name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL
);

CREATE TABLE kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (1);
