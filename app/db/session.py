from typing import Generator

from sqlmodel import SQLModel, create_engine, Session
from app.core import config

# SQLite-specific connection args for async/multi-threaded FastAPI
connect_args = {"check_same_thread": False, "timeout": 30} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    config.DATABASE_URL, 
    echo=False, 
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=3600
)

if config.DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event as _event

    @_event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:
        # The web server and the Huey worker write concurrently: WAL lets readers
        # and one writer coexist instead of failing with "database is locked".
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session

def migrate_db() -> None:
    """Idempotent runtime migrations: add columns/tables that newer code expects.

    Uses PRAGMA table_info instead of try/except around ALTER TABLE, so a real
    failure (locked DB, bad type) is logged instead of silently swallowed.
    """
    from loguru import logger

    columns = {
        "briefs": [
            ("title", "VARCHAR(255)"), ("media_normal_path", "VARCHAR(500)"),
            ("media_vertical_path", "VARCHAR(500)"), ("music_path", "VARCHAR(500)"),
            ("music_offset_sec", "INTEGER"), ("fade_in_ms", "INTEGER"), ("fade_out_ms", "INTEGER"),
            ("vertical_916", "BOOLEAN"), ("crop_x", "INTEGER"), ("crop_y", "INTEGER"),
            ("crop_scale", "REAL"),
        ],
        # Shorts render-preset columns
        "projects": [
            ("crop_x", "INTEGER"), ("crop_y", "INTEGER"), ("crop_scale", "REAL"),
            ("music_path", "VARCHAR(500)"), ("music_offset_sec", "INTEGER"),
            ("fade_in_ms", "INTEGER"), ("fade_out_ms", "INTEGER"),
            ("platforms_csv", "VARCHAR(200)"),
        ],
        # "clip type" preset columns (title/description/music)
        "presets": [("title", "VARCHAR(300)"), ("text_en", "TEXT"), ("music_path", "VARCHAR(500)")],
    }
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS projects ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL DEFAULT 1, "
            "name VARCHAR NOT NULL, "
            "icon_path VARCHAR, "
            "tags_json VARCHAR, "
            "platforms_json VARCHAR, "
            "sort_order INTEGER NOT NULL DEFAULT 0, "
            "created_at DATETIME, "
            "updated_at DATETIME)"
        )
        for table, cols in columns.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table not created yet (create_all handles fresh DBs)
            for col, typ in cols:
                if col in existing:
                    continue
                try:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    logger.info(f"migrate_db: added {table}.{col}")
                except Exception as exc:
                    logger.error(f"migrate_db: could not add {table}.{col}: {exc}")
