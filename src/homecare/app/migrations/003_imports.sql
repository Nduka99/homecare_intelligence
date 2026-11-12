-- Imported visit exports. One row per file, found by its SHA-256, so the same file imported
-- twice adds nothing. Kept rows (with flags for a person to look at) and quarantined rows (with
-- reasons, the lines they relate to, and their cells exactly as written) are kept apart, and
-- nothing here is ever edited or deleted.

CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    imported REAL NOT NULL,
    by TEXT NOT NULL,
    kept INTEGER NOT NULL,
    quarantined INTEGER NOT NULL,
    flagged INTEGER NOT NULL,
    blank INTEGER NOT NULL
);

CREATE TABLE actuals (
    import INTEGER NOT NULL REFERENCES imports (id),
    line INTEGER NOT NULL,
    visit TEXT NOT NULL REFERENCES visits (id),
    carer TEXT NOT NULL REFERENCES carers (id),
    date TEXT NOT NULL,
    check_in INTEGER NOT NULL,
    check_out INTEGER NOT NULL,
    note TEXT,
    flags TEXT NOT NULL,
    PRIMARY KEY (import, line)
);

CREATE TABLE quarantine (
    import INTEGER NOT NULL REFERENCES imports (id),
    line INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    related TEXT NOT NULL,
    cells TEXT NOT NULL,
    PRIMARY KEY (import, line)
);

CREATE TRIGGER imports_append_only_update BEFORE UPDATE ON imports
BEGIN SELECT RAISE (ABORT, 'imports are append-only'); END;
CREATE TRIGGER imports_append_only_delete BEFORE DELETE ON imports
BEGIN SELECT RAISE (ABORT, 'imports are append-only'); END;
CREATE TRIGGER actuals_append_only_update BEFORE UPDATE ON actuals
BEGIN SELECT RAISE (ABORT, 'actuals are append-only'); END;
CREATE TRIGGER actuals_append_only_delete BEFORE DELETE ON actuals
BEGIN SELECT RAISE (ABORT, 'actuals are append-only'); END;
CREATE TRIGGER quarantine_append_only_update BEFORE UPDATE ON quarantine
BEGIN SELECT RAISE (ABORT, 'quarantine is append-only'); END;
CREATE TRIGGER quarantine_append_only_delete BEFORE DELETE ON quarantine
BEGIN SELECT RAISE (ABORT, 'quarantine is append-only'); END;
