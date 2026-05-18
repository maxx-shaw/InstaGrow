import datetime
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean,
    DateTime, Text, Float
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DB_PATH = os.environ.get("INSTAGROW_DB_PATH", "./instagrow.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    user_id = Column(String, nullable=True)
    session_data = Column(Text, nullable=True)   # JSON settings from instagrapi
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class FollowRecord(Base):
    __tablename__ = "follow_records"
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    target_user_id = Column(String, nullable=False, index=True)
    target_username = Column(String, nullable=False)
    target_full_name = Column(String, nullable=True)
    target_profile_pic = Column(String, nullable=True)
    followed_at = Column(DateTime, nullable=True)
    unfollowed_at = Column(DateTime, nullable=True)
    # True = follows back, False = doesn't, None = unchecked
    followed_back = Column(Boolean, nullable=True)
    followed_back_checked_at = Column(DateTime, nullable=True)
    # manual | auto_mutual | auto_fof (followers-of-following)
    source = Column(String, default="manual")
    is_active = Column(Boolean, default=True)   # False = unfollowed


class Blacklist(Base):
    __tablename__ = "blacklist"
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    target_user_id = Column(String, nullable=False)
    target_username = Column(String, nullable=False)
    reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Whitelist(Base):
    """Accounts to never unfollow."""
    __tablename__ = "whitelist"
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    target_user_id = Column(String, nullable=False)
    target_username = Column(String, nullable=False)
    reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class FollowQueue(Base):
    """Accounts queued to be followed by the auto-follow job."""
    __tablename__ = "follow_queue"
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    target_user_id = Column(String, nullable=False)
    target_username = Column(String, nullable=False)
    target_full_name = Column(String, nullable=True)
    target_profile_pic = Column(String, nullable=True)
    mutual_count = Column(Integer, default=0)
    source = Column(String, default="auto_fof")
    added_at = Column(DateTime, default=datetime.datetime.utcnow)


class AutomationSettings(Base):
    __tablename__ = "automation_settings"
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, unique=True, nullable=False)
    # Unfollow
    auto_unfollow_enabled = Column(Boolean, default=False)
    unfollow_after_days = Column(Integer, default=7)
    unfollow_per_day = Column(Integer, default=50)
    unfollow_only_non_followers = Column(Boolean, default=True)
    # Follow
    auto_follow_enabled = Column(Boolean, default=False)
    follow_per_day = Column(Integer, default=30)
    follow_delay_min = Column(Integer, default=45)    # seconds between follows
    follow_delay_max = Column(Integer, default=180)
    mutual_follows_threshold = Column(Integer, default=2)
    # Active hours (24h)
    active_hours_start = Column(Integer, default=9)
    active_hours_end = Column(Integer, default=22)
    # Scan settings
    scan_depth = Column(Integer, default=5)   # how many followings to sample for FOF scan
    # State
    last_unfollow_run = Column(DateTime, nullable=True)
    last_follow_run = Column(DateTime, nullable=True)
    last_scan_run = Column(DateTime, nullable=True)
    follows_today = Column(Integer, default=0)
    unfollows_today = Column(Integer, default=0)
    daily_reset_date = Column(String, nullable=True)   # YYYY-MM-DD


class ActivityLog(Base):
    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    # follow | unfollow | login | logout | scan | error | blacklist
    action_type = Column(String, nullable=False)
    target_username = Column(String, nullable=True)
    target_user_id = Column(String, nullable=True)
    details = Column(Text, nullable=True)
    success = Column(Boolean, default=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
