-- The platform's first schema. Lists are stored as JSON text. Notes, findings, decisions and
-- the audit log are append-only: a changed decision is a new row, the newest
-- counts, and the history stays.

CREATE TABLE clients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    details TEXT NOT NULL,
    client_group TEXT NOT NULL,
    needs TEXT NOT NULL
);

CREATE TABLE carers (
    id TEXT PRIMARY KEY,
    gender TEXT NOT NULL,
    skills TEXT NOT NULL,
    contract TEXT NOT NULL,
    days_off TEXT NOT NULL
);

CREATE TABLE visits (
    id TEXT PRIMARY KEY,
    client TEXT NOT NULL REFERENCES clients (id),
    day INTEGER NOT NULL,
    slot TEXT NOT NULL,
    opens INTEGER NOT NULL,
    closes INTEGER NOT NULL,
    duration INTEGER NOT NULL,
    carer TEXT REFERENCES carers (id),
    position INTEGER
);

CREATE TABLE notes (
    id TEXT PRIMARY KEY,
    visit TEXT NOT NULL REFERENCES visits (id),
    client TEXT NOT NULL REFERENCES clients (id),
    carer TEXT NOT NULL REFERENCES carers (id),
    day INTEGER NOT NULL,
    body TEXT NOT NULL,
    tamper INTEGER NOT NULL,
    tamper_probability REAL NOT NULL
);

CREATE TABLE findings (
    id INTEGER PRIMARY KEY,
    note TEXT NOT NULL REFERENCES notes (id),
    position INTEGER NOT NULL,
    category TEXT NOT NULL,
    band TEXT NOT NULL,
    quote TEXT NOT NULL,
    reason TEXT NOT NULL,
    found_by TEXT NOT NULL
);

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('carer', 'coordinator', 'manager', 'lead')),
    carer TEXT REFERENCES carers (id),
    password TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user TEXT REFERENCES users (id),  -- NULL while signed out (the sign-in form's CSRF token)
    csrf TEXT NOT NULL,
    created REAL NOT NULL,
    seen REAL NOT NULL
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    note TEXT NOT NULL REFERENCES notes (id),
    finding INTEGER REFERENCES findings (id),
    label TEXT NOT NULL CHECK (label IN ('confirmed', 'wrong category', 'not a concern', 'missed concern')),
    category TEXT,
    reviewer TEXT NOT NULL REFERENCES users (id),
    visitor TEXT,
    route TEXT NOT NULL,
    decided REAL NOT NULL
);

CREATE TABLE audit (
    position INTEGER PRIMARY KEY,
    entry TEXT NOT NULL,
    mac TEXT NOT NULL
);

CREATE TRIGGER notes_append_only_update BEFORE UPDATE ON notes
BEGIN SELECT RAISE (ABORT, 'notes are append-only'); END;
CREATE TRIGGER notes_append_only_delete BEFORE DELETE ON notes
BEGIN SELECT RAISE (ABORT, 'notes are append-only'); END;
CREATE TRIGGER findings_append_only_update BEFORE UPDATE ON findings
BEGIN SELECT RAISE (ABORT, 'findings are append-only'); END;
CREATE TRIGGER findings_append_only_delete BEFORE DELETE ON findings
BEGIN SELECT RAISE (ABORT, 'findings are append-only'); END;
CREATE TRIGGER decisions_append_only_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE (ABORT, 'decisions are append-only'); END;
CREATE TRIGGER decisions_append_only_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE (ABORT, 'decisions are append-only'); END;
CREATE TRIGGER audit_append_only_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE (ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER audit_append_only_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE (ABORT, 'the audit log is append-only'); END;
