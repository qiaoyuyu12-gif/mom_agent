"""
Skill 注册表:磁盘扫描 + 数据库元数据 + 缓存查询。

策略:
- 上传 .md 落到 SKILLS_DIR;元数据 upsert 到 PG `skills` 表。
- 启动时调用 sync_disk_to_db() 扫一遍目录,确保 DB 与磁盘一致。
- 查询 list_skills() 给前端 / 自动补全用。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.db.models import Skill
from app.skills.loader import ParsedSkill, parse_skill_file


def skills_dir() -> Path:
    """返回 skill 目录的 Path 对象,目录不存在则创建。"""
    p = Path(get_settings().SKILLS_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def upsert_skill(db: OrmSession, parsed: ParsedSkill) -> None:
    """把解析后的 skill 元数据写入 PG(name 主键 upsert)。"""
    stmt = pg_insert(Skill).values(
        name=parsed.name,
        description=parsed.description,
        trigger=parsed.trigger,
        file_path=parsed.file_path,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Skill.name],
        set_={
            "description": parsed.description,
            "trigger": parsed.trigger,
            "file_path": parsed.file_path,
        },
    )
    db.execute(stmt)
    db.commit()


def list_skills(db: OrmSession) -> List[Skill]:
    """列出所有已注册 skill(按名字排序)。"""
    return list(db.execute(select(Skill).order_by(Skill.name)).scalars().all())


def get_skill(db: OrmSession, name: str) -> Skill | None:
    """按 name 取一个 skill 元数据。"""
    return db.get(Skill, name)


def sync_disk_to_db(db: OrmSession) -> int:
    """
    扫描 SKILLS_DIR 下所有 .md,解析并 upsert 到 DB。

    返回成功同步的数量。解析失败的文件会被跳过(不阻塞启动)。
    """
    count = 0
    for f in skills_dir().glob("*.md"):
        try:
            parsed = parse_skill_file(f)
        except Exception:
            continue
        upsert_skill(db, parsed)
        count += 1
    return count
