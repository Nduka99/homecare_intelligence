-- A carer opens their own records for the demo clock's day. To open another day ("Open another
-- day") they give a reason, stored here as an alert with when it was given, for the manager to
-- review; the opening lasts the day it was given. A review records who marked the alert
-- reviewed, once. In demo mode each alert is kept to its visitor. Both are append-only.

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    user TEXT NOT NULL REFERENCES users (id),
    visitor TEXT,
    day INTEGER NOT NULL,
    reason TEXT NOT NULL,
    clock_day INTEGER NOT NULL,
    clock_minute INTEGER NOT NULL,
    opened REAL NOT NULL
);

CREATE INDEX alerts_by_user ON alerts (user, clock_day);

CREATE TABLE alert_reviews (
    id INTEGER PRIMARY KEY,
    alert INTEGER NOT NULL UNIQUE REFERENCES alerts (id),
    reviewer TEXT NOT NULL REFERENCES users (id),
    reviewed REAL NOT NULL
);

CREATE TRIGGER alerts_append_only_update BEFORE UPDATE ON alerts
BEGIN SELECT RAISE (ABORT, 'alerts are append-only'); END;
CREATE TRIGGER alerts_append_only_delete BEFORE DELETE ON alerts
BEGIN SELECT RAISE (ABORT, 'alerts are append-only'); END;
CREATE TRIGGER alert_reviews_append_only_update BEFORE UPDATE ON alert_reviews
BEGIN SELECT RAISE (ABORT, 'alert reviews are append-only'); END;
CREATE TRIGGER alert_reviews_append_only_delete BEFORE DELETE ON alert_reviews
BEGIN SELECT RAISE (ABORT, 'alert reviews are append-only'); END;
