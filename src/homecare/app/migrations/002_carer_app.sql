-- Notes written in the carer app. A note from the seed keeps these new columns at their defaults.
-- An app note records:
--   source: 'app';
--   amends: the note it adds to (a record is never edited, so an addition is a new note);
--   idempotency_key: the key its Send carried, so a resent Send never stores it twice;
--   written: when it arrived;
--   visitor: in demo mode, the visitor who wrote it, so no visitor sees another's.
-- The 001 tamper columns are NOT NULL: an app note stores 0 there, because the tamper tag was
-- measured on the demo week only and is never run on app notes.

ALTER TABLE notes ADD COLUMN source TEXT NOT NULL DEFAULT 'seed' CHECK (source IN ('seed', 'app'));
ALTER TABLE notes ADD COLUMN amends TEXT REFERENCES notes (id);
ALTER TABLE notes ADD COLUMN idempotency_key TEXT;
ALTER TABLE notes ADD COLUMN written REAL;
ALTER TABLE notes ADD COLUMN visitor TEXT;
CREATE UNIQUE INDEX notes_one_per_key ON notes (carer, idempotency_key);

-- When the laptop's model read an app note (`homecare platform read`); its findings go in
-- `findings` as usual. An app note with no reading has not been read: the lead sees it listed
-- as such, so a note is never silently unseen.
CREATE TABLE readings (
    note TEXT PRIMARY KEY REFERENCES notes (id),
    extraction TEXT NOT NULL,
    read REAL NOT NULL
);

CREATE TRIGGER readings_append_only_update BEFORE UPDATE ON readings
BEGIN SELECT RAISE (ABORT, 'readings are append-only'); END;
CREATE TRIGGER readings_append_only_delete BEFORE DELETE ON readings
BEGIN SELECT RAISE (ABORT, 'readings are append-only'); END;
