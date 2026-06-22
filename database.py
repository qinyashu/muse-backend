from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL


# SQLite needs check_same_thread disabled when sessions are used by FastAPI.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """初始化数据库表；兼容直接运行 main.py 和包模式运行。"""
    try:
        from .models import Base
    except ImportError:
        from models import Base

    Base.metadata.create_all(bind=engine)
