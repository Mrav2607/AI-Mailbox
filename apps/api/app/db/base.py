from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from ..core.config import settings

# Bound how long a new connection may hang: /ready probes Postgres on a
# request thread, and without this an unreachable DB blocks that worker until
# the TCP stack gives up. (Postgres-only knob; other schemes don't take it.)
_connect_args = (
    {"connect_timeout": 5} if settings.database_url.startswith("postgresql") else {}
)
# Size the pool explicitly rather than inheriting SQLAlchemy's defaults. The
# worker holds two connections per task (the ingest session plus the heartbeat's
# own), and pool_recycle keeps a cloud NAT from handing us back a dead socket it
# quietly dropped while idle.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    pool_recycle=1800,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass
