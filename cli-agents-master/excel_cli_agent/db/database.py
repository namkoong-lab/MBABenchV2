from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from ..repo_config import resolve_db_url

# Base is always available (needed for model definitions)
Base = declarative_base()

# Engine and session are lazy — only created when first used
_engine = None

# Set by configure() before first DB use so resolve_db_url can pick
# database.v1_url vs database.v2_url from the monorepo config. None means
# "no benchmark declared" (e.g. local/legacy modes), which resolves from
# DATABASE_URL alone.
_benchmark = None


def configure(benchmark: str) -> None:
    """Pin the benchmark that selects the database URL.

    Must be called before the first session is opened; once the engine
    exists the URL is baked in, so a benchmark change would silently keep
    talking to the old database.
    """
    global _benchmark
    if _engine is not None and benchmark != _benchmark:
        raise RuntimeError(
            f"Database already connected for benchmark={_benchmark!r}; "
            f"cannot switch to {benchmark!r} in the same process."
        )
    _benchmark = benchmark


def _get_database_url() -> str:
    url, _source = resolve_db_url(_benchmark)
    if not url:
        raise RuntimeError(
            "No database URL. Set database.v1_url / database.v2_url in "
            "<MBABenchV2>/config/config.yaml (the batch config's `benchmark` "
            "key selects between them), or export DATABASE_URL."
        )
    return url


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_get_database_url(), echo=True, pool_pre_ping=True)
    return _engine


def SessionLocal():
    """Create a new database session."""
    factory = sessionmaker(autocommit=False, autoflush=False, bind=_get_engine())
    return factory()


def get_db():
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize the database"""
    Base.metadata.create_all(bind=_get_engine())


if __name__ == "__main__":
    init_db()
