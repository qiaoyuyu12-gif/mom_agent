"""
FastAPI 应用入口。

- 启动时:同步 skills 目录到 DB(扫盘 upsert)。
- 路由:挂载 /chat 与 /skills/*。
- 静态:把 web/ 挂在根路径,直接访问 http://host:port 即看到聊天页。
- /health:简易健康检查。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.chat import router as chat_router
from app.api.history import router as history_router
from app.api.nap import router as nap_router
from app.api.skills import router as skills_router
from app.config import get_settings
from app.db.session import SessionLocal
from app.skills.registry import sync_disk_to_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """应用工厂(便于测试中替换依赖)。"""
    settings = get_settings()
    app = FastAPI(title="mom_agent", version="0.1.0")

    # 嵌入到现有系统时,可能从不同前端域名访问 → 允许跨域(可按需收紧)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 业务路由
    app.include_router(chat_router)
    app.include_router(skills_router)
    app.include_router(history_router)
    # NAP(NoDeskClaw Agent Protocol)兼容路由：/health /meta /stream
    app.include_router(nap_router)

    @app.get("/health")
    def health() -> dict:
        """NAP 健康检查端点，返回 status=ok 及当前 Unix 时间戳。"""
        return {"status": "ok", "timestamp": int(time.time())}

    # 启动:同步 skill 目录到 DB
    @app.on_event("startup")
    def _on_startup() -> None:
        with SessionLocal() as db:
            n = sync_disk_to_db(db)
        logger.info("skill 目录同步完成,共注册 %d 个 skill", n)

    # 静态前端(放在最后,避免吞掉上面的路由)
    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


# uvicorn 入口:`uvicorn app.main:app --reload`
app = create_app()
