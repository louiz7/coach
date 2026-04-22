import sys
import os
from logging.config import fileConfig
from sqlalchemy import pool, create_engine
from sqlalchemy.orm import DeclarativeBase
from alembic import context

# Add app directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Patch DATABASE_URL before importing app modules so database.py doesn't crash
# when using a sync driver for migrations
_raw_url = os.environ.get("DATABASE_URL", "")
if _raw_url and "postgresql+asyncpg://" in _raw_url:
    os.environ["DATABASE_URL"] = _raw_url  # keep as-is, database.py will still error

# Import Base and models AFTER patching env
# We define a fresh Base here to avoid triggering the async engine creation in database.py
from app.config import settings

# Dynamically import model metadata without triggering database.py's engine creation
# by importing models after temporarily replacing the engine creation
import importlib

# Temporarily stub out the async engine so database.py can be imported
import unittest.mock as mock
import sqlalchemy.ext.asyncio as _async_ext

with mock.patch.object(_async_ext, "create_async_engine", return_value=mock.MagicMock()):
    from app.database import Base
    import app.models  # noqa: F401 — registers all model classes on Base.metadata

target_metadata = Base.metadata

def get_sync_url():
    """Convert asyncpg URL to psycopg2 synchronous URL."""
    url = os.environ.get("DATABASE_URL", settings.DATABASE_URL)
    return url.replace("postgresql+asyncpg://", "postgresql://")

def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    """Run migrations using a synchronous psycopg2 connection."""
    engine = create_engine(get_sync_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        do_run_migrations(connection)
    engine.dispose()

if context.is_offline_mode():
    url = get_sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
