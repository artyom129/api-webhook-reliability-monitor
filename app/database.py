from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

IN_MEMORY_URLS = {"sqlite://", "sqlite:///:memory:"}


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    if database_url in IN_MEMORY_URLS:
        # One shared connection, so every session sees the same in-memory database.
        return create_engine(database_url, connect_args=connect_args, poolclass=StaticPool, future=True)
    return create_engine(database_url, connect_args=connect_args, future=True)


def build_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
