"""
JMComic AstrBot 插件 - HTTP 客户端
通过 REST API 调用 JMComic Server
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

from astrbot.api import logger

from .config import PluginConfig


@dataclass
class TaskStatus:
    """服务器返回的任务状态"""
    task_id: str
    status: str  # pending / downloading / success / failed
    jm_id: str
    jm_type: str
    error: Optional[str] = None
    album_name: Optional[str] = None
    album_author: Optional[str] = None
    file_count: int = 0
    files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @classmethod
    def from_dict(cls, d: dict) -> "TaskStatus":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class ExportResult:
    """导出结果"""
    task_id: str
    pdf_path: Optional[str] = None
    zip_path: Optional[str] = None
    error: Optional[str] = None


class JMComicClient:
    """JMComic Server HTTP 客户端"""

    # 禁漫链接匹配
    JM_URL_PATTERNS = [
        re.compile(
            r'(?:https?://)?(?:www\.)?(?:18comic|jmcomic\d*|jmcmic)\.[a-zA-Z]+/'
            r'(?:album|photo)/(\d+)',
            re.IGNORECASE,
        ),
    ]

    JM_ID_PATTERN = re.compile(r'^(?:JM)?(\d{3,8})$', re.IGNORECASE)

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def base_url(self) -> str:
        return self.cfg.server_url.rstrip("/")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.cfg.download_timeout + 30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ---- ID 提取 ----

    @classmethod
    def extract_jm_id(cls, text: str) -> Optional[tuple[str, str]]:
        """从文本中提取禁漫 ID，返回 (id, type)"""
        text = text.strip()
        for pattern in cls.JM_URL_PATTERNS:
            match = pattern.search(text)
            if match and match.lastindex and match.lastindex >= 1:
                jm_id = match.group(1)
                jm_type = "photo" if "/photo/" in match.group(0).lower() else "album"
                return (jm_id, jm_type)

        match = cls.JM_ID_PATTERN.match(text)
        if match:
            return (match.group(1), "album")
        return None

    # ---- API 调用 ----

    async def health_check(self) -> bool:
        """检查 Server 是否可用"""
        try:
            session = await self._ensure_session()
            async with session.get(f"{self.base_url}/api/health") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def download_album(self, jm_id: str, max_pages: int = 0) -> TaskStatus:
        """下载本子"""
        session = await self._ensure_session()
        payload = {"id": jm_id, "max_pages": max_pages or self.cfg.download_max_pages}
        logger.info(f"[JMComic] POST /api/download/album id={jm_id}")

        async with session.post(
            f"{self.base_url}/api/download/album", json=payload
        ) as resp:
            data = await resp.json()
            return TaskStatus.from_dict(data)

    async def download_photo(self, jm_id: str, max_pages: int = 0) -> TaskStatus:
        """下载章节"""
        session = await self._ensure_session()
        payload = {"id": jm_id, "max_pages": max_pages or self.cfg.download_max_pages}
        logger.info(f"[JMComic] POST /api/download/photo id={jm_id}")

        async with session.post(
            f"{self.base_url}/api/download/photo", json=payload
        ) as resp:
            data = await resp.json()
            return TaskStatus.from_dict(data)

    async def export_pdf(self, task_id: str) -> ExportResult:
        """导出 PDF"""
        session = await self._ensure_session()
        logger.info(f"[JMComic] POST /api/export/pdf task_id={task_id}")

        async with session.post(
            f"{self.base_url}/api/export/pdf", params={"task_id": task_id}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return ExportResult(task_id=task_id, pdf_path=data.get("pdf_path"))
            else:
                data = await resp.json()
                return ExportResult(
                    task_id=task_id,
                    error=data.get("detail", "PDF 导出失败"),
                )

    async def export_zip(self, task_id: str) -> ExportResult:
        """导出 ZIP"""
        session = await self._ensure_session()
        logger.info(f"[JMComic] POST /api/export/zip task_id={task_id}")

        async with session.post(
            f"{self.base_url}/api/export/zip", params={"task_id": task_id}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return ExportResult(task_id=task_id, zip_path=data.get("zip_path"))
            else:
                data = await resp.json()
                return ExportResult(
                    task_id=task_id,
                    error=data.get("detail", "ZIP 打包失败"),
                )

    async def delete_task(self, task_id: str):
        """删除任务并清理文件"""
        session = await self._ensure_session()
        try:
            async with session.delete(
                f"{self.base_url}/api/task/{task_id}"
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[JMComic] 删除任务失败: {task_id}")
        except Exception:
            pass

    async def download_file(self, task_id: str, file_path: str) -> Optional[bytes]:
        """下载单个文件内容（任务下载目录中的图片等）"""
        session = await self._ensure_session()
        file_name = Path(file_path).name

        try:
            async with session.get(
                f"{self.base_url}/api/file/{task_id}/{file_name}"
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as e:
            logger.warning(f"[JMComic] 下载文件失败: {file_name}, {e}")
        return None

    async def download_export_file(self, task_id: str, server_path: str) -> Optional[bytes]:
        """下载导出的 PDF/ZIP 文件"""
        session = await self._ensure_session()
        file_name = Path(server_path).name

        try:
            async with session.get(
                f"{self.base_url}/api/export/download/{task_id}/{file_name}"
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as e:
            logger.warning(f"[JMComic] 下载导出文件失败: {file_name}, {e}")
        return None
