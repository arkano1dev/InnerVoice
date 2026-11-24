"""Content-free metadata retained separately from result delivery."""
import time


def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS performance(
      id TEXT PRIMARY KEY, owner TEXT, profile TEXT, created REAL, finished REAL,
      state TEXT, done INTEGER DEFAULT 0, total INTEGER, attempts INTEGER DEFAULT 0,
      audio_seconds REAL, admission_seconds REAL, decode_seconds REAL, inference_seconds REAL DEFAULT 0,
      first_started REAL, first_served REAL, cleanup TEXT, error TEXT);
    CREATE INDEX IF NOT EXISTS performance_recent ON performance(created DESC);
    CREATE TABLE IF NOT EXISTS rejections(code TEXT PRIMARY KEY,count INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS heartbeat(singleton INTEGER PRIMARY KEY,observed REAL,profile TEXT,admitting INTEGER);
    CREATE TRIGGER IF NOT EXISTS track_job AFTER UPDATE ON jobs
    WHEN NEW.digest!='capture' AND NEW.state!='expired'
    BEGIN
      UPDATE performance SET state=NEW.state,done=NEW.done,finished=NEW.finished,error=NEW.error WHERE id=NEW.id;
    END;
    """)
