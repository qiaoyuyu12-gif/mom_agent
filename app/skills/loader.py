"""
Skill 文件加载与解析。

Skill 文件格式(.md + YAML frontmatter,与 Claude Code skill 一致):

    ---
    name: 翻译助手
    description: 把后续用户输入翻译为英文
    trigger: 翻译
    ---
    # 翻译助手

    请把用户消息翻译为英文,保留原意,使用专业术语。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from app.core.guards import assert_safe_skill_text


@dataclass
class ParsedSkill:
    """从一个 .md 文件解析出的 skill 数据。"""

    name: str
    description: str
    trigger: str | None
    body: str           # 正文(用于注入 prompt)
    file_path: str      # 磁盘绝对/相对路径


class SkillFormatError(ValueError):
    """skill 文件格式错误。"""


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\u4e00-\u9fa5\-]+")


def safe_filename(name: str) -> str:
    """把 skill 名转为可作为文件名的安全形式,保留中英文与下划线/连字符。"""
    cleaned = _SAFE_NAME_RE.sub("_", name.strip())
    return cleaned[:80] or "skill"


def parse_skill_file(path: str | Path) -> ParsedSkill:
    """读取并解析一个 skill 文件。失败抛 SkillFormatError。"""
    p = Path(path)
    if not p.exists():
        raise SkillFormatError(f"skill 文件不存在: {p}")
    try:
        post = frontmatter.load(p)
    except Exception as e:  # frontmatter 自己会抛多种异常
        raise SkillFormatError(f"frontmatter 解析失败: {e}") from e

    meta = post.metadata or {}
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    trigger = meta.get("trigger")
    if trigger is not None:
        trigger = str(trigger).strip() or None

    if not name:
        raise SkillFormatError("frontmatter 缺少 name 字段")
    if not description:
        raise SkillFormatError("frontmatter 缺少 description 字段")

    body = (post.content or "").strip()
    if not body:
        raise SkillFormatError("skill 正文为空")
    try:
        assert_safe_skill_text(f"{name}\n{description}\n{trigger or ''}\n{body}")
    except ValueError as e:
        raise SkillFormatError(str(e)) from e

    return ParsedSkill(
        name=name,
        description=description,
        trigger=trigger,
        body=body,
        file_path=str(p),
    )


def parse_skill_bytes(content: bytes, original_filename: str | None = None) -> ParsedSkill:
    """从上传的字节流解析 skill(不落盘),用于上传接口先校验后保存。"""
    text = content.decode("utf-8", errors="replace")
    try:
        post = frontmatter.loads(text)
    except Exception as e:
        raise SkillFormatError(f"frontmatter 解析失败: {e}") from e

    meta = post.metadata or {}
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    trigger = meta.get("trigger")
    if trigger is not None:
        trigger = str(trigger).strip() or None

    if not name:
        raise SkillFormatError("frontmatter 缺少 name 字段")
    if not description:
        raise SkillFormatError("frontmatter 缺少 description 字段")
    body = (post.content or "").strip()
    if not body:
        raise SkillFormatError("skill 正文为空")
    try:
        assert_safe_skill_text(f"{name}\n{description}\n{trigger or ''}\n{body}")
    except ValueError as e:
        raise SkillFormatError(str(e)) from e

    return ParsedSkill(
        name=name,
        description=description,
        trigger=trigger,
        body=body,
        file_path=original_filename or "",
    )
