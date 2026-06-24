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
    table_names = inspector.get_table_names()
    if "tasks" not in table_names:
        return

    with engine.begin() as connection:
        task_columns = {column["name"] for column in inspector.get_columns("tasks")}
        if "error_message" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN error_message VARCHAR"))
        if "source_video_url" not in task_columns:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN source_video_url VARCHAR"))

        if "users" in table_names:
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            if "phone" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR"))
                connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users (phone)"))
            if "password_hash" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR"))
            if "is_unlimited" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN is_unlimited BOOLEAN DEFAULT 0 NOT NULL"))


def _seed_unlimited_accounts() -> None:
    """Create or update the two permanent unlimited accounts."""
    try:
        from .auth_utils import hash_password, verify_password
        from .models import User
    except ImportError:
        from auth_utils import hash_password, verify_password
        from models import User

    accounts = ("19908654220", "13084900320")
    password = "qinyashu123456"

    with SessionLocal() as db:
        for phone in accounts:
            user = db.query(User).filter(User.phone == phone).first()
            if user is None:
                device_id = f"phone_{phone}"
                user = db.query(User).filter(User.device_id == device_id).first()
                if user is None:
                    user = User(device_id=device_id, remaining_count=0)
                    db.add(user)

                user.phone = phone

            user.device_id = user.device_id or f"phone_{phone}"
            user.is_unlimited = True
            if not verify_password(password, user.password_hash):
                user.password_hash = hash_password(password)

        db.commit()


def init_db() -> None:
    """初始化数据库表；兼容直接运行 main.py 和包模式运行。"""
    try:
        from .models import Base
    except ImportError:
        from models import Base

    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_schema_updates()
    _seed_unlimited_accounts()
