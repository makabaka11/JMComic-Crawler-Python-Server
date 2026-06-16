"""
JMComic Server - FastAPI 后端
独立运行，提供 REST API 供 AstrBot 插件调用

启动方式:
    python jmcomic_server.py
    uvicorn jmcomic_server:app --host 0.0.0.0 --port 8899

依赖:
    pip install fastapi uvicorn
    以及 jmcomic 本身的依赖 (commonx, curl-cffi, pillow, pycryptodome, pyyaml)
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# 确保 jmcomic 库在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import jmcomic

app = FastAPI(
    title="JMComic Server",
    description="禁漫下载后端服务，为 AstrBot 插件提供 REST API",
    version=jmcomic.__version__,
)

# ================================================================
#  配置
# ================================================================


class ServerConfig:
    """Server 运行时配置"""

    def __init__(self):
        # 下载根目录
        self.download_root = Path(
            os.environ.get("JMCOMIC_DOWNLOAD_DIR", "./data/jmcomic/downloads")
        ).resolve()
        self.download_root.mkdir(parents=True, exist_ok=True)

        # 临时目录（PDF/ZIP）
        self.temp_root = Path(
            os.environ.get("JMCOMIC_TEMP_DIR", "./data/jmcomic/temp")
        ).resolve()
        self.temp_root.mkdir(parents=True, exist_ok=True)

        # 最大并发下载数
        self.max_concurrent = int(os.environ.get("JMCOMIC_MAX_CONCURRENT", "3"))

        # 下载超时（秒）
        self.download_timeout = int(os.environ.get("JMCOMIC_DOWNLOAD_TIMEOUT", "600"))

        # 最大页数限制（0=不限制）
        self.max_pages = int(os.environ.get("JMCOMIC_MAX_PAGES", "200"))

        # 代理
        self.proxy = os.environ.get("JMCOMIC_PROXY", "")

        # API Key（可选，用于鉴权）
        self.api_key = os.environ.get("JMCOMIC_API_KEY", "")


config = ServerConfig()

# 并发控制
_semaphore = asyncio.Semaphore(config.max_concurrent)

# 活跃任务
_active_tasks: dict[str, dict] = {}


# ================================================================
#  Pydantic Models
# ================================================================


class DownloadRequest(BaseModel):
    id: str  # JM ID
    max_pages: int = 0  # 0 = 使用 server 默认值


class TaskStatus(BaseModel):
    task_id: str
    status: str  # pending / downloading / success / failed
    jm_id: str
    jm_type: str  # album / photo
    error: Optional[str] = None
    album_name: Optional[str] = None
    album_author: Optional[str] = None
    file_count: int = 0
    files: list[str] = []  # 相对下载目录的文件路径


# ================================================================
#  辅助函数
# ================================================================


def _build_option(download_dir: Path) -> jmcomic.JmOption:
    """构建 jmcomic 下载选项"""
    option_dict = {
        "log": False,
        "dir_rule": {
            "rule": "Bd",
            "base_dir": str(download_dir),
        },
        "download": {
            "image": {"suffix": ".jpg"},
            "threading": {
                "image": min(config.max_concurrent * 2, 10),
                "photo": config.max_concurrent,
            },
        },
        "client": {
            "domain": [],
            "cache": True,
        },
    }

    if config.proxy:
        option_dict["client"]["postman"] = {
            "meta_data": {"proxies": config.proxy}
        }

    return jmcomic.JmOption.construct(option_dict)


def _collect_files(downloader, download_dir: Path, max_pages: int) -> list[str]:
    """收集下载的文件路径（相对路径）"""
    success_dict = downloader.download_success_dict
    files = []

    for album, photo_dict in success_dict.items():
        for photo, img_list in photo_dict.items():
            for save_path, img_detail in img_list:
                p = Path(save_path)
                if p.exists():
                    files.append(str(p))

    files.sort()

    if max_pages > 0 and len(files) > max_pages:
        files = files[:max_pages]

    return files


def _check_auth(request) -> bool:
    """检查 API Key 鉴权"""
    if not config.api_key:
        return True
    auth = getattr(request, "headers", {}).get("Authorization", "")
    return auth == f"Bearer {config.api_key}"


# ================================================================
#  Health
# ================================================================


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": jmcomic.__version__}


# ================================================================
#  Download Album
# ================================================================


@app.post("/api/download/album")
async def download_album(req: DownloadRequest):
    """下载本子"""
    task_id = f"album_{req.id}_{uuid.uuid4().hex[:8]}"
    download_dir = config.download_root / task_id
    download_dir.mkdir(parents=True, exist_ok=True)

    task = {
        "task_id": task_id,
        "status": "downloading",
        "jm_id": req.id,
        "jm_type": "album",
        "error": None,
        "album_name": None,
        "album_author": None,
        "file_count": 0,
        "files": [],
        "download_dir": str(download_dir),
    }
    _active_tasks[task_id] = task

    try:
        async with _semaphore:
            max_pages = req.max_pages if req.max_pages > 0 else config.max_pages
            option = _build_option(download_dir)

            album, downloader = await asyncio.wait_for(
                jmcomic.download_album_async(req.id, option=option),
                timeout=config.download_timeout,
            )

            files = _collect_files(downloader, download_dir, max_pages)

            task["status"] = "success"
            task["album_name"] = album.name
            task["album_author"] = album.author
            task["file_count"] = len(files)
            task["files"] = files

            return TaskStatus(**task)

    except asyncio.TimeoutError:
        task["status"] = "failed"
        task["error"] = f"下载超时（{config.download_timeout}秒）"
        return TaskStatus(**task)

    except Exception as e:
        task["status"] = "failed"
        task["error"] = f"{type(e).__name__}: {e}"
        return TaskStatus(**task)


# ================================================================
#  Download Photo
# ================================================================


@app.post("/api/download/photo")
async def download_photo(req: DownloadRequest):
    """下载章节"""
    task_id = f"photo_{req.id}_{uuid.uuid4().hex[:8]}"
    download_dir = config.download_root / task_id
    download_dir.mkdir(parents=True, exist_ok=True)

    task = {
        "task_id": task_id,
        "status": "downloading",
        "jm_id": req.id,
        "jm_type": "photo",
        "error": None,
        "album_name": None,
        "album_author": None,
        "file_count": 0,
        "files": [],
        "download_dir": str(download_dir),
    }
    _active_tasks[task_id] = task

    try:
        async with _semaphore:
            max_pages = req.max_pages if req.max_pages > 0 else config.max_pages
            option = _build_option(download_dir)

            photo, downloader = await asyncio.wait_for(
                jmcomic.download_photo_async(req.id, option=option),
                timeout=config.download_timeout,
            )

            files = _collect_files(downloader, download_dir, max_pages)

            task["status"] = "success"
            task["file_count"] = len(files)
            task["files"] = files

            return TaskStatus(**task)

    except asyncio.TimeoutError:
        task["status"] = "failed"
        task["error"] = f"下载超时（{config.download_timeout}秒）"
        return TaskStatus(**task)

    except Exception as e:
        task["status"] = "failed"
        task["error"] = f"{type(e).__name__}: {e}"
        return TaskStatus(**task)


# ================================================================
#  Export PDF / ZIP
# ================================================================


@app.post("/api/export/pdf")
async def export_pdf(task_id: str = Query(...)):
    """将已下载的图片合并为 PDF"""
    task = _active_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "success":
        raise HTTPException(400, "任务未完成，无法导出")

    files = task["files"]
    if not files:
        raise HTTPException(400, "没有可导出的文件")

    try:
        from PIL import Image

        pdf_dir = config.temp_root / task_id
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / f"{task['jm_id']}.pdf"

        images = []
        for fpath in files:
            try:
                img = Image.open(fpath)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                images.append(img)
            except Exception:
                pass

        if not images:
            raise HTTPException(400, "没有可用的图片")

        images[0].save(
            pdf_path, "PDF", save_all=True,
            append_images=images[1:] if len(images) > 1 else [],
            quality=85,
        )
        for img in images:
            img.close()

        return {"pdf_path": str(pdf_path), "task_id": task_id}

    except Exception as e:
        raise HTTPException(500, f"PDF 导出失败: {e}")


@app.post("/api/export/zip")
async def export_zip(task_id: str = Query(...)):
    """将已下载的图片打包为 ZIP"""
    task = _active_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "success":
        raise HTTPException(400, "任务未完成，无法导出")

    files = task["files"]
    if not files:
        raise HTTPException(400, "没有可导出的文件")

    try:
        import zipfile

        zip_dir = config.temp_root / task_id
        zip_dir.mkdir(parents=True, exist_ok=True)
        zip_path = zip_dir / f"{task['jm_id']}.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath in files:
                zf.write(fpath, Path(fpath).name)

        return {"zip_path": str(zip_path), "task_id": task_id}

    except Exception as e:
        raise HTTPException(500, f"ZIP 打包失败: {e}")


# ================================================================
#  Task 管理
# ================================================================


@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    """查询任务状态"""
    task = _active_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return TaskStatus(**task)


@app.delete("/api/task/{task_id}")
async def delete_task(task_id: str):
    """删除任务并清理文件"""
    task = _active_tasks.pop(task_id, None)
    if not task:
        raise HTTPException(404, "任务不存在")

    # 清理下载目录
    dl_dir = Path(task["download_dir"])
    if dl_dir.exists():
        shutil.rmtree(dl_dir)

    # 清理临时目录
    temp_dir = config.temp_root / task_id
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    return {"ok": True}


@app.get("/api/file/{task_id}/{file_name:path}")
async def serve_file(task_id: str, file_name: str):
    """直接提供文件下载（图片等）"""
    task = _active_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    file_path = Path(task["download_dir"]) / file_name
    if not file_path.exists():
        raise HTTPException(404, "文件不存在")

    return FileResponse(file_path)


@app.get("/api/export/download/{task_id}/{file_name:path}")
async def serve_export_file(task_id: str, file_name: str):
    """下载导出的 PDF/ZIP 文件"""
    temp_dir = config.temp_root / task_id
    file_path = temp_dir / file_name
    if not file_path.exists():
        raise HTTPException(404, "导出文件不存在")
    return FileResponse(file_path)


# ================================================================
#  启动
# ================================================================

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("JMCOMIC_HOST", "127.0.0.1")
    port = int(os.environ.get("JMCOMIC_PORT", "8899"))

    print(f"JMComic Server v{jmcomic.__version__}")
    print(f"Listening on http://{host}:{port}")
    print(f"Download dir: {config.download_root}")

    uvicorn.run(app, host=host, port=port, log_level="info")
