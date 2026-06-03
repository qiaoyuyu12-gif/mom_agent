"""
Skill 运行时:被 agent 调用,根据 skill 名取出正文,准备注入 prompt。

当前是"指令注入式":把 skill 正文作为附加 system 指令,引导 LLM 行为。
若未来要扩展为带工具的子 agent,可在这里包装更复杂的执行链路,
对调用方接口不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session as OrmSession

from app.skills.loader import parse_skill_file
from app.skills.registry import get_skill


@dataclass
class RuntimeSkill:
    """运行时使用的 skill 数据。"""

    name: str
    description: str
    body: str


def load_runtime_skill(db: OrmSession, name: str) -> RuntimeSkill | None:
    """
    按 skill 名加载运行时数据。

    - 先从 DB 取元数据(含 file_path)。
    - 从磁盘读取正文(避免每次写入 DB)。
    - 任何环节失败返回 None,调用方降级为"不带 skill"执行。
    """
    meta = get_skill(db, name)
    if meta is None or not meta.file_path:
        return None
    if not Path(meta.file_path).exists():
        return None
    try:
        parsed = parse_skill_file(meta.file_path)
    except Exception:
        return None
    return RuntimeSkill(name=parsed.name, description=parsed.description, body=parsed.body)


def render_skill_as_instruction(skill: RuntimeSkill) -> str:
    """把 skill 正文渲染为加在系统消息里的"任务指令"段。"""
    return (
        f"用户当前选择了 skill【{skill.name}】(用途:{skill.description})。\n"
        f"请严格按照以下 skill 指令完成本轮任务:\n\n{skill.body}"
    )
