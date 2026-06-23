from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL


# SQLite needs check_same_thread disabled when sessions are used by FastAPI.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _ensure_sqlite_schema_updates() -> None:
    """补齐 SQLite 老数据库中 create_all 不会自动新增的列。"""
    if not DATABASE_URL.startswith("sqlite"):
        return

    inspector = inspect(engine)
    if "tasks" not in inspector.get_table_names():
        return

    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    with engine.begin() as connection:
        if "error_message" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN error_message VARCHAR"))


def init_db() -> None:
    """初始化数据库表；兼容直接运行 main.py 和包模式运行。"""
    try:
        from .models import Base
    except ImportError:
        from models import Base

    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_schema_updates()
