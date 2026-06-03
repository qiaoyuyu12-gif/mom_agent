"""
Skill 相关 HTTP 接口:
- GET  /skills            列表(供前端 / 自动补全)
- POST /skills/upload     上传 .md skill 文件
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session as OrmSession

from app.db.session import get_db
from app.skills.loader import SkillFormatError, parse_skill_bytes, safe_filename
from app.skills.registry import list_skills, skills_dir, upsert_skill

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillItem(BaseModel):
    """前端 / 自动补全用的最小 skill 描述。"""

    name: str
    description: str
    trigger: str | None = None


@router.get("", response_model=List[SkillItem])
def api_list_skills(db: OrmSession = Depends(get_db)) -> List[SkillItem]:
    """列出全部 skill。"""
    rows = list_skills(db)
    return [SkillItem(name=r.name, description=r.description or "", trigger=r.trigger) for r in rows]


@router.post("/upload", response_model=SkillItem)
async def api_upload_skill(
    file: UploadFile = File(...),
    db: OrmSession = Depends(get_db),
) -> SkillItem:
    """
    上传一个 skill 文件:
    1. 读取字节并先解析(失败直接 400 报错,不落盘)。
    2. 把文件以"安全文件名"保存到 SKILLS_DIR。
    3. upsert 到 DB(以 frontmatter 中的 name 为主键)。
    """
    content = await file.read()
    try:
        parsed = parse_skill_bytes(content, original_filename=file.filename)
    except SkillFormatError as e:
        raise HTTPException(status_code=400, detail=f"skill 格式错误: {e}") from e

    # 落盘:以 skill name 为文件名,统一 .md 后缀
    target = skills_dir() / f"{safe_filename(parsed.name)}.md"
    target.write_bytes(content)

    # 落 DB:用磁盘上的绝对路径,方便运行时再加载
    parsed.file_path = str(target.resolve())
    upsert_skill(db, parsed)

    return SkillItem(name=parsed.name, description=parsed.description, trigger=parsed.trigger)
