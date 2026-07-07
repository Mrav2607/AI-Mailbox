from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from ..core.config import settings

# Bound how long a new connection may hang: /ready probes Postgres on a
# request thread, and without this an unreachable DB blocks that worker until
# the TCP stack gives up. (Postgres-only knob; other schemes don't take it.)
_connect_args = (
    {"connect_timeout": 5} if settings.database_url.startswith("postgresql") else {}
)
engine = create_engine(
    settings.database_url, pool_pre_ping=True, connect_args=_connect_args
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass
