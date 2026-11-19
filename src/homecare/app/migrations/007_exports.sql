-- A client's record leaves the app only with two people: the safeguarding lead asks, with a
-- reason; a coordinator approves within the request's deadline; the person who asked then
-- downloads the file, once. Who did each step comes from their session, never a form. Each row
-- keeps its demo visitor. All three are append-only, and a request is approved and downloaded
-- at most once.

CREATE TABLE export_requests (
    id INTEGER PRIMARY KEY,
    client TEXT NOT NULL REFERENCES clients (id),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES users (id),
    visitor TEXT,
    requested REAL NOT NULL,
    deadline REAL NOT NULL
);

CREATE TABLE export_approvals (
    id INTEGER PRIMARY KEY,
    request INTEGER NOT NULL UNIQUE REFERENCES export_requests (id),
    approved_by TEXT NOT NULL REFERENCES users (id),
    approved REAL NOT NULL
);

CREATE TABLE export_releases (
    id INTEGER PRIMARY KEY,
    request INTEGER NOT NULL UNIQUE REFERENCES export_requests (id),
    released_by TEXT NOT NULL REFERENCES users (id),
    released REAL NOT NULL,
    resources INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);

CREATE TRIGGER export_requests_append_only_update BEFORE UPDATE ON export_requests
BEGIN SELECT RAISE (ABORT, 'export requests are append-only'); END;
CREATE TRIGGER export_requests_append_only_delete BEFORE DELETE ON export_requests
BEGIN SELECT RAISE (ABORT, 'export requests are append-only'); END;
CREATE TRIGGER export_approvals_append_only_update BEFORE UPDATE ON export_approvals
BEGIN SELECT RAISE (ABORT, 'export approvals are append-only'); END;
CREATE TRIGGER export_approvals_append_only_delete BEFORE DELETE ON export_approvals
BEGIN SELECT RAISE (ABORT, 'export approvals are append-only'); END;
CREATE TRIGGER export_releases_append_only_update BEFORE UPDATE ON export_releases
BEGIN SELECT RAISE (ABORT, 'export releases are append-only'); END;
CREATE TRIGGER export_releases_append_only_delete BEFORE DELETE ON export_releases
BEGIN SELECT RAISE (ABORT, 'export releases are append-only'); END;
