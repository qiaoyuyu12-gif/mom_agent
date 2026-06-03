"""
SQLAlchemy 引擎与 Session 工厂。

提供:
- engine:进程内单例引擎(连接 PostgreSQL)
- SessionLocal:同步 Session 工厂
- get_db():FastAPI 依赖注入,按请求开/关 Session
"""

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

# 进程级引擎(连接池由 SQLAlchemy 维护)
_settings = get_settings()
engine = create_engine(
    _settings.DATABASE_URL,
    pool_pre_ping=True,   # 自动剔除断开连接,避免长闲后报错
    future=True,
)

# Session 工厂:autoflush=False 让我们显式控制写入时机
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖:产出一个数据库 Session,请求结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
