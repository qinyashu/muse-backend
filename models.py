from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base, relationship

from config import DEFAULT_REMAINING_COUNT


Base = declarative_base()


class User(Base):
    """User quota record keyed by device ID."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, unique=True, index=True, nullable=False)
    remaining_count = Column(Integer, default=DEFAULT_REMAINING_COUNT, nullable=False)

    tasks = relationship("Task", back_populates="user")


class Task(Base):
    """Processing task submitted by a user."""

    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    type = Column(String, nullable=False)
    douyin_url = Column(String, nullable=True)
    source_video_url = Column(String, nullable=True)
    photo_url = Column(String, nullable=True)
    audio_url = Column(String, nullable=True)
    output_url = Column(String, nullable=True)
    status = Column(String, default="pending", index=True, nullable=False)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="tasks")
