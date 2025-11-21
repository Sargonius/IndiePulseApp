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

def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session

def migrate_db() -> None:
    # Simple runtime migration for newly added columns
    from sqlmodel import text
    with engine.begin() as conn:
        # Add title column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN title VARCHAR(255)")
        except Exception:
            pass
        
        # Add media_normal_path column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN media_normal_path VARCHAR(500)")
        except Exception:
            pass
        
        # Add media_vertical_path column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN media_vertical_path VARCHAR(500)")
        except Exception:
            pass
        
        # Add music_path column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN music_path VARCHAR(500)")
        except Exception:
            pass
        
        # Add music_offset_sec column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN music_offset_sec INTEGER")
        except Exception:
            pass
        
        # Add fade_in_ms column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN fade_in_ms INTEGER")
        except Exception:
            pass
        
        # Add fade_out_ms column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN fade_out_ms INTEGER")
        except Exception:
            pass
        
        # Add vertical_916 column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN vertical_916 BOOLEAN")
        except Exception:
            pass
        
        # Add crop_x column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN crop_x INTEGER")
        except Exception:
            pass
        
        # Add crop_y column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN crop_y INTEGER")
        except Exception:
            pass
        
        # Add crop_scale column if not exists
        try:
            conn.exec_driver_sql("ALTER TABLE briefs ADD COLUMN crop_scale REAL")
        except Exception:
            pass
