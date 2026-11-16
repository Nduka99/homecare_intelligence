-- Expired sessions are deleted each time a session starts, by their last-seen and created times.
-- Without these indexes that delete reads every session while it holds the write lock.

CREATE INDEX sessions_seen ON sessions (seen);
CREATE INDEX sessions_created ON sessions (created);
