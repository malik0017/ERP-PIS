# app/database/session.py
"""Database session and engine configuration"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.engine import Engine
from ..core.config import settings

# Create SQLAlchemy engine
engine = create_engine(
    settings.DATABASE_URL,
    echo=False,  
    pool_pre_ping=True, 
    pool_size=10,
    max_overflow=20,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# Declarative base for models
Base = declarative_base()

def get_db():
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    """MySQL connection initialization (Batch 11: skip on non-MySQL engines)."""
    if "mysql" not in (settings.DATABASE_URL or "").lower():
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("SET CHARACTER SET utf8mb4")
    cursor.execute("SET COLLATION_CONNECTION = utf8mb4_unicode_ci")
    cursor.close()