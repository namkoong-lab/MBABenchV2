import os
from pathlib import Path

import yaml
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker


def _get_database_url() -> str:
    """Resolve DATABASE_URL from env var or configs.yaml fallback."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    # Fallback: look for configs.yaml in common locations
    for candidate in [
        Path(__file__).parent / "configs.yaml",
        Path(__file__).parent.parent / "configs.yaml",
        Path.cwd() / "configs.yaml",
    ]:
        if candidate.exists():
            with open(candidate) as f:
                configs = yaml.load(f, Loader=yaml.FullLoader)
            return configs["DATABASE_URL"]
    raise RuntimeError(
        "DATABASE_URL not set. Either set the environment variable or "
        "create a configs.yaml file."
    )


# Base is always available (needed for model definitions)
Base = declarative_base()

# Engine and session are lazy — only created when first used
_engine = None
_SessionLocal = None


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
