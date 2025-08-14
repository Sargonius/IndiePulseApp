from typing import Generator

from sqlmodel import SQLModel, create_engine, Session
from app.core import config

engine = create_engine(config.DATABASE_URL, echo=False)

def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
