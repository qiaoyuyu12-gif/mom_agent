"""
Skill 相关 HTTP 接口:
- GET  /skills            列表(供前端 / 自动补全)
- POST /skills/upload     上传 skill:单个 .md 文件,或一个文件夹(渐进式披露)

上传规则:
- 单文件:必须是 .md,且 frontmatter 含 name / description。
- 文件夹:必须至少含一个 .md;入口 md(优先名为 SKILL.md;否则文件夹内唯一的 .md)
  必须是「渐进式披露」格式 —— frontmatter 含 name / description。其余文件(脚本/参考)
  原样保存到该 skill 目录,供运行时按需使用。
"""

from __future__ import annotations

import posixpath
import shutil
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session as OrmSession

from app.db.session import get_db
from app.skills.loader import SkillFormatError, parse_skill_bytes, safe_filename
from app.skills.registry import list_skills, skills_dir, upsert_skill

router = APIRouter(prefix="/skills", tags=["skills"])

# 文件夹上传的粗粒度上限,挡住误传整盘 / 超大目录
MAX_SKILL_FILES = 50
MAX_SKILL_TOTAL_BYTES = 5 * 1024 * 1024  # 5MB


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


def _norm_member_path(raw: str) -> str:
    """把上传的相对路径标准化为安全相对路径:统一分隔符、剔除空段/.、禁止 ..。"""
    raw = (raw or "").replace("\\", "/")
    parts: list[str] = []
    for seg in raw.split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            raise HTTPException(status_code=400, detail=f"非法文件路径: {raw}")
        parts.append(seg)
    return "/".join(parts)


def _strip_common_top(members: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """若所有相对路径共享同一顶层目录(浏览器目录上传的特征),把它剥掉。"""
    firsts = {p.split("/", 1)[0] for p, _ in members if "/" in p}
    only_tops = {p for p, _ in members if "/" not in p}
    # 仅当存在统一顶层目录且没有散落在根的文件时才剥离
    if len(firsts) == 1 and not only_tops:
        top = next(iter(firsts))
        out: list[tuple[str, bytes]] = []
        for p, c in members:
            rest = p[len(top) + 1:] if p.startswith(top + "/") else p
            if rest:
                out.append((rest, c))
        return out
    return members


def _pick_entry(md_members: list[tuple[str, bytes]]) -> tuple[str, bytes]:
    """从文件夹内的 .md 中挑入口:SKILL.md 优先,否则唯一的 .md,否则报错。"""
    for p, c in md_members:
        if posixpath.basename(p).lower() == "skill.md":
            return p, c
    if len(md_members) == 1:
        return md_members[0]
    raise HTTPException(
        status_code=400,
        detail="文件夹包含多个 .md,请用 SKILL.md 作为入口文件",
    )


def _clear_existing(name: str) -> None:
    """落盘前清掉同名的旧存储(单文件 or 文件夹),避免单/夹两种形态并存。"""
    d = skills_dir()
    safe = safe_filename(name)
    old_file = d / f"{safe}.md"
    if old_file.exists():
        old_file.unlink()
    old_dir = d / safe
    if old_dir.exists() and old_dir.is_dir():
        shutil.rmtree(old_dir)


@router.post("/upload", response_model=SkillItem)
async def api_upload_skill(
    files: List[UploadFile] = File(...),
    db: OrmSession = Depends(get_db),
) -> SkillItem:
    """上传 skill:单个 .md 文件,或一个文件夹(多文件)。"""
    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")

    # 读全部字节 + 标准化相对路径
    members: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        data = await f.read()
        total += len(data)
        if total > MAX_SKILL_TOTAL_BYTES:
            raise HTTPException(status_code=400, detail="上传内容过大(>5MB)")
        rel = _norm_member_path(f.filename or "")
        if rel:
            members.append((rel, data))
    if not members:
        raise HTTPException(status_code=400, detail="未收到有效文件")
    if len(members) > MAX_SKILL_FILES:
        raise HTTPException(status_code=400, detail=f"文件过多(>{MAX_SKILL_FILES})")

    # —— 情形 A:单个 .md 文件 ——
    if len(members) == 1 and members[0][0].lower().endswith(".md"):
        rel, content = members[0]
        try:
            parsed = parse_skill_bytes(content, original_filename=rel)
        except SkillFormatError as e:
            raise HTTPException(status_code=400, detail=f"skill 格式错误: {e}") from e

        _clear_existing(parsed.name)
        target = skills_dir() / f"{safe_filename(parsed.name)}.md"
        target.write_bytes(content)
        parsed.file_path = str(target.resolve())
        upsert_skill(db, parsed)
        return SkillItem(name=parsed.name, description=parsed.description, trigger=parsed.trigger)

    # —— 情形 B:文件夹 ——
    members = _strip_common_top(members)
    md_members = [(p, c) for p, c in members if p.lower().endswith(".md")]
    if not md_members:
        raise HTTPException(status_code=400, detail="文件夹中必须包含至少一个 .md 文件")

    entry_path, entry_content = _pick_entry(md_members)
    try:
        parsed = parse_skill_bytes(entry_content, original_filename=entry_path)
    except SkillFormatError as e:
        raise HTTPException(
            status_code=400,
            detail=f"入口 md 需为渐进式披露格式(frontmatter 含 name/description): {e}",
        ) from e

    # 落盘:整个文件夹保存到 SKILLS_DIR/{safe_name}/,入口指向其内的 md
    _clear_existing(parsed.name)
    base = skills_dir() / safe_filename(parsed.name)
    base.mkdir(parents=True, exist_ok=True)
    base_resolved = base.resolve()

    entry_disk = None
    for p, c in members:
        dest = base / p
        dest_resolved = dest.resolve()
        # 兜底防穿越:落点必须仍在 base 之内
        if base_resolved != dest_resolved and base_resolved not in dest_resolved.parents:
            raise HTTPException(status_code=400, detail=f"非法文件路径: {p}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(c)
        if p == entry_path:
            entry_disk = dest_resolved

    parsed.file_path = str(entry_disk)
    upsert_skill(db, parsed)
    return SkillItem(name=parsed.name, description=parsed.description, trigger=parsed.trigger)
