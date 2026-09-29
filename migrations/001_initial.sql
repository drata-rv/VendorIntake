CREATE TABLE schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  installation_uuid TEXT NOT NULL,
  account_id TEXT,
  account_name TEXT,
  account_domain TEXT,
  api_key_enc TEXT,
  credential_version INTEGER NOT NULL DEFAULT 0,
  connection_state TEXT NOT NULL DEFAULT 'DISCONNECTED'
    CHECK (connection_state IN ('DISCONNECTED', 'ACTIVE', 'BLOCKED', 'RESTORED_UNVERIFIED')),
  writes_enabled INTEGER NOT NULL DEFAULT 0 CHECK (writes_enabled IN (0, 1)),
  updates_enabled INTEGER NOT NULL DEFAULT 0 CHECK (updates_enabled IN (0, 1)),
  attest_create INTEGER NOT NULL DEFAULT 0 CHECK (attest_create IN (0, 1)),
  attest_update INTEGER NOT NULL DEFAULT 0 CHECK (attest_update IN (0, 1)),
  custom_fields_enabled INTEGER NOT NULL DEFAULT 0 CHECK (custom_fields_enabled IN (0, 1)),
  write_observed_version INTEGER NOT NULL DEFAULT 0,
  retention_terminal_days INTEGER NOT NULL DEFAULT 30,
  retention_unresolved_days INTEGER NOT NULL DEFAULT 90,
  retention_ledger_days INTEGER NOT NULL DEFAULT 365,
  rate_limit_until TEXT,
  connected_at TEXT,
  last_cleanup_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('ADMIN', 'REQUESTER')),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_login_at TEXT
);

CREATE TABLE sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE login_limits (
  key_hash TEXT PRIMARY KEY,
  window_start TEXT NOT NULL,
  count INTEGER NOT NULL,
  cooldown_until TEXT
);

CREATE TABLE form_versions (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  schema_json TEXT NOT NULL,
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE TRIGGER form_versions_immutable_update BEFORE UPDATE ON form_versions
BEGIN SELECT RAISE(ABORT, 'form_versions is immutable'); END;
CREATE TRIGGER form_versions_immutable_delete BEFORE DELETE ON form_versions
BEGIN SELECT RAISE(ABORT, 'form_versions is immutable'); END;

CREATE TABLE active_form (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER REFERENCES form_versions(version),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  definitions_json TEXT
);

CREATE TABLE submissions (
  id TEXT PRIMARY KEY,
  requester_id TEXT NOT NULL REFERENCES users(id),
  form_version INTEGER NOT NULL REFERENCES form_versions(version),
  idempotency_key TEXT NOT NULL UNIQUE,
  request_hash TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'IN_PROGRESS', 'CREATED', 'UPDATED', 'NEEDS_REVIEW', 'NEEDS_CORRECTION',
    'BLOCKED', 'RETRYABLE', 'UNKNOWN', 'CANCELLED')),
  state_reason TEXT,
  operation TEXT NOT NULL CHECK (operation IN ('CREATE', 'UPDATE')),
  target_drata_id INTEGER,
  result_drata_id INTEGER,
  result_link TEXT,
  answers_enc TEXT,
  payload_enc TEXT,
  snapshot_enc TEXT,
  candidates_enc TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  credential_version INTEGER,
  last_error TEXT,
  duplicate_reason TEXT,
  origin_submission_id TEXT REFERENCES submissions(id),
  action_nonce TEXT,
  action_binding TEXT,
  action_expires_at TEXT,
  payload_purged_at TEXT,
  terminal_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX submissions_requester ON submissions(requester_id, created_at);
CREATE INDEX submissions_state ON submissions(state);

CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
  attempt_number INTEGER NOT NULL,
  method TEXT NOT NULL,
  path_template TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  credential_version INTEGER,
  http_status INTEGER,
  outcome TEXT,
  dispatched INTEGER NOT NULL DEFAULT 0 CHECK (dispatched IN (0, 1)),
  UNIQUE (submission_id, attempt_number)
);

CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN (
    'LOGIN', 'LOGOUT', 'PASSWORD_CHANGED', 'USER_CREATED', 'USER_UPDATED',
    'CONNECTION_SAVED', 'CONNECTION_DISCONNECTED', 'SETTINGS_UPDATED',
    'FORM_SAVED', 'FORM_PUBLISHED', 'FORM_DISABLED',
    'SUBMISSION_ACCEPTED', 'SUBMISSION_STATE', 'SUBMISSION_RETRY', 'SUBMISSION_RECONCILE',
    'SUBMISSION_RESOLVE', 'UPDATE_PREVIEW', 'UPDATE_CONFIRM', 'SUBMISSION_EXPORT',
    'CLEANUP', 'STARTUP_RECOVERY', 'RESTORE_CHECK', 'KEY_ROTATED')),
  subject TEXT,
  at TEXT NOT NULL,
  meta TEXT
);
CREATE INDEX audit_subject ON audit_events(subject);
CREATE TRIGGER audit_events_immutable BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is immutable'); END;
