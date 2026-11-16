-- Referrals for new clients, and the coordinator's confirmed profiles.
-- A referral holds its text and the laptop model's read of it (claims with their quotes, and the
-- claims dropped because their quote was not found), loaded from the pinned demo file.
-- A decision is the profile a person confirmed, stored as JSON in the agency's own keys, with
-- who confirmed it (from the session, never the form) and, in demo mode, the visitor. Both are
-- append-only: a changed profile is a new decision, and the newest counts.

CREATE TABLE referrals (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    claims TEXT NOT NULL,
    dropped TEXT NOT NULL
);

CREATE TABLE referral_decisions (
    id INTEGER PRIMARY KEY,
    referral TEXT NOT NULL REFERENCES referrals (id),
    profile TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users (id),
    visitor TEXT,
    decided REAL NOT NULL
);

CREATE TRIGGER referrals_append_only_update BEFORE UPDATE ON referrals
BEGIN SELECT RAISE (ABORT, 'referrals are append-only'); END;
CREATE TRIGGER referrals_append_only_delete BEFORE DELETE ON referrals
BEGIN SELECT RAISE (ABORT, 'referrals are append-only'); END;
CREATE TRIGGER referral_decisions_append_only_update BEFORE UPDATE ON referral_decisions
BEGIN SELECT RAISE (ABORT, 'referral decisions are append-only'); END;
CREATE TRIGGER referral_decisions_append_only_delete BEFORE DELETE ON referral_decisions
BEGIN SELECT RAISE (ABORT, 'referral decisions are append-only'); END;
