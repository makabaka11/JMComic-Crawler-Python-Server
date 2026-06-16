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
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Optional

# 确保 jmcomic 库在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
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
        self.download_root = Path(
            os.environ.get("JMCOMIC_DOWNLOAD_DIR", "./data/jmcomic/downloads")
        ).resolve()
        self.download_root.mkdir(parents=True, exist_ok=True)

        self.temp_root = Path(
            os.environ.get("JMCOMIC_TEMP_DIR", "./data/jmcomic/temp")
        ).resolve()
        self.temp_root.mkdir(parents=True, exist_ok=True)

        self.max_concurrent = int(os.environ.get("JMCOMIC_MAX_CONCURRENT", "3"))
        self.download_timeout = int(os.environ.get("JMCOMIC_DOWNLOAD_TIMEOUT", "600"))
        self.max_pages = int(os.environ.get("JMCOMIC_MAX_PAGES", "200"))
        self.proxy = os.environ.get("JMCOMIC_PROXY", "")
        self.api_key = os.environ.get("JMCOMIC_API_KEY", "")

        # 缓存 TTL（小时），0 表示永不过期
        self.cache_ttl_hours = int(os.environ.get("JMCOMIC_CACHE_TTL_HOURS", "24"))

        # 缓存清理间隔（秒）
        self.cache_cleanup_interval = int(
            os.environ.get("JMCOMIC_CACHE_CLEANUP_INTERVAL", "3600")
        )

        # SQLite 缓存数据库路径
        db_dir = self.download_root.parent / "cache"
        db_dir.mkdir(parents=True, exist_ok=True)
        self.cache_db_path = db_dir / "cache.db"


config = ServerConfig()

# 并发控制
_semaphore = asyncio.Semaphore(config.max_concurrent)

# 活跃任务
_active_tasks: dict[str, dict] = {}

# ================================================================
#  SQLite 缓存
# ================================================================

_cache_conn: Optional[sqlite3.Connection] = None
_cache_lock = Lock()


def _get_cache_db() -> sqlite3.Connection:
    global _cache_conn
    if _cache_conn is None:
        _cache_conn = sqlite3.connect(str(config.cache_db_path), check_same_thread=False)
        _cache_conn.execute("PRAGMA journal_mode=WAL")
        _cache_conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                jm_id       TEXT PRIMARY KEY,
                jm_type     TEXT NOT NULL,
                task_id     TEXT NOT NULL,
                download_dir TEXT NOT NULL,
                album_name  TEXT,
                album_author TEXT,
                file_count  INTEGER DEFAULT 0,
                files_json  TEXT DEFAULT '[]',
                cached_at   REAL NOT NULL
            )"""
        )
        _cache_conn.commit()
    return _cache_conn


def _cache_get(jm_id: str) -> Optional[dict]:
    """查询缓存"""
    db = _get_cache_db()
    row = db.execute(
        "SELECT jm_id, jm_type, task_id, download_dir, album_name, "
        "album_author, file_count, files_json, cached_at "
        "FROM cache WHERE jm_id = ?",
        (jm_id,),
    ).fetchone()
    if row is None:
        return None

    cached = {
        "jm_id": row[0],
        "jm_type": row[1],
        "task_id": row[2],
        "download_dir": row[3],
        "album_name": row[4],
        "album_author": row[5],
        "file_count": row[6],
        "files": json.loads(row[7]),
        "cached_at": row[8],
    }

    # 检查 TTL 是否过期
    if config.cache_ttl_hours > 0:
        age_hours = (time.time() - cached["cached_at"]) / 3600
        if age_hours > config.cache_ttl_hours:
            return None

    # 检查文件是否仍然存在
    existing = [f for f in cached["files"] if Path(f).exists()]
    if not existing and cached["file_count"] > 0:
        return None

    cached["files"] = existing
    cached["file_count"] = len(existing)
    return cached


def _cache_put(jm_id: str, task: dict):
    """写入缓存"""
    db = _get_cache_db()
    db.execute(
        "INSERT OR REPLACE INTO cache "
        "(jm_id, jm_type, task_id, download_dir, album_name, "
        "album_author, file_count, files_json, cached_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            jm_id,
            task["jm_type"],
            task["task_id"],
            task["download_dir"],
            task.get("album_name"),
            task.get("album_author"),
            task["file_count"],
            json.dumps(task["files"]),
            time.time(),
        ),
    )
    db.commit()


def _cache_remove(jm_id: str):
    """删除缓存"""
    db = _get_cache_db()
    db.execute("DELETE FROM cache WHERE jm_id = ?", (jm_id,))
    db.commit()


def _cache_cleanup_expired() -> int:
    """清理过期缓存，返回清理数量"""
    if config.cache_ttl_hours <= 0:
        return 0

    db = _get_cache_db()
    cutoff = time.time() - config.cache_ttl_hours * 3600
    rows = db.execute(
        "SELECT jm_id, download_dir, task_id FROM cache WHERE cached_at < ?",
        (cutoff,),
    ).fetchall()

    cleaned = 0
    for row in rows:
        jm_id, download_dir, task_id = row
        # 删除文件
        dl_path = Path(download_dir)
        if dl_path.exists():
            shutil.rmtree(dl_path, ignore_errors=True)
        temp_path = config.temp_root / task_id
        if temp_path.exists():
            shutil.rmtree(temp_path, ignore_errors=True)
        # 从活跃任务中移除
        _active_tasks.pop(task_id, None)
        cleaned += 1

    db.execute("DELETE FROM cache WHERE cached_at < ?", (cutoff,))
    db.commit()
    return cleaned


async def _cache_cleanup_loop():
    """后台定时清理过期缓存"""
    while True:
        await asyncio.sleep(config.cache_cleanup_interval)
        try:
            count = _cache_cleanup_expired()
            if count > 0:
                print(f"[Cache] 清理了 {count} 条过期缓存")
        except Exception as e:
            print(f"[Cache] 清理异常: {e}")


@app.on_event("startup")
async def startup():
    """启动时执行一次过期清理，并开启后台清理任务"""
    _cache_cleanup_expired()
    asyncio.create_task(_cache_cleanup_loop())


# ================================================================
#  Pydantic Models
# ================================================================


class DownloadRequest(BaseModel):
    id: str  # JM ID
    max_pages: int = 0  # 0 = 使用 server 默认值
    force: bool = False  # 跳过缓存，强制重新下载


class TaskStatus(BaseModel):
    task_id: str
    status: str  # pending / downloading / success / failed / cached
    jm_id: str
    jm_type: str  # album / photo
    error: Optional[str] = None
    album_name: Optional[str] = None
    album_author: Optional[str] = None
    file_count: int = 0
    files: list[str] = []


# ================================================================
#  辅助函数
# ================================================================


def _build_option(download_dir: Path) -> jmcomic.JmOption:
    option_dict = {
        "log": False,
        "dir_rule": {"rule": "Bd", "base_dir": str(download_dir)},
        "download": {
            "image": {"suffix": ".jpg"},
            "threading": {
                "image": min(config.max_concurrent * 2, 10),
                "photo": config.max_concurrent,
            },
        },
        "client": {"domain": [], "cache": True},
    }
    if config.proxy:
        option_dict["client"]["postman"] = {
            "meta_data": {"proxies": config.proxy}
        }
    return jmcomic.JmOption.construct(option_dict)


def _collect_files(downloader, download_dir: Path, max_pages: int) -> list[str]:
    success_dict = downloader.download_success_dict
    files = []
    for album, photo_dict in success_dict.items():
        for photo, img_list in photo_dict.items():
            for save_path, _img_detail in img_list:
                p = Path(save_path)
                if p.exists():
                    files.append(str(p))
    files.sort()
    if max_pages > 0 and len(files) > max_pages:
        files = files[:max_pages]
    return files


# ================================================================
#  Health & Cache
# ================================================================


@app.get("/api/health")
async def health():
    db = _get_cache_db()
    count = db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    return {"status": "ok", "version": jmcomic.__version__, "cached": count}


@app.get("/api/cache/stats")
async def cache_stats():
    """缓存统计"""
    db = _get_cache_db()
    count = db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    rows = db.execute(
        "SELECT jm_id, jm_type, album_name, file_count, cached_at FROM cache "
        "ORDER BY cached_at DESC LIMIT 50"
    ).fetchall()
    entries = [
        {
            "jm_id": r[0], "jm_type": r[1], "album_name": r[2],
            "file_count": r[3], "cached_at": r[4],
            "age_hours": round((time.time() - r[4]) / 3600, 1),
        }
        for r in rows
    ]
    return {"total": count, "recent": entries}


@app.post("/api/cache/cleanup")
async def force_cleanup():
    """手动触发缓存清理"""
    count = _cache_cleanup_expired()
    return {"cleaned": count}


# ================================================================
#  Download Album
# ================================================================


@app.post("/api/download/album")
async def download_album(req: DownloadRequest):
    """下载本子（优先命中缓存）"""
    jm_id = req.id

    # 1. 检查缓存
    if not req.force:
        cached = _cache_get(jm_id)
        if cached and cached["jm_type"] == "album":
            # 确保在活跃任务中可访问
            _active_tasks[cached["task_id"]] = cached
            return TaskStatus(
                task_id=cached["task_id"],
                status="cached",
                jm_id=jm_id,
                jm_type="album",
                album_name=cached["album_name"],
                album_author=cached["album_author"],
                file_count=cached["file_count"],
                files=cached["files"],
            )

    # 2. 缓存未命中，执行下载
    task_id = f"album_{jm_id}_{uuid.uuid4().hex[:8]}"
    download_dir = config.download_root / task_id
    download_dir.mkdir(parents=True, exist_ok=True)

    task = {
        "task_id": task_id,
        "status": "downloading",
        "jm_id": jm_id,
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
                jmcomic.download_album_async(jm_id, option=option),
                timeout=config.download_timeout,
            )

            files = _collect_files(downloader, download_dir, max_pages)
            task["status"] = "success"
            task["album_name"] = album.name
            task["album_author"] = album.author
            task["file_count"] = len(files)
            task["files"] = files

            # 写入缓存
            _cache_put(jm_id, task)

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
    """下载章节（优先命中缓存）"""
    jm_id = req.id

    # 1. 检查缓存
    if not req.force:
        cached = _cache_get(jm_id)
        if cached and cached["jm_type"] == "photo":
            _active_tasks[cached["task_id"]] = cached
            return TaskStatus(
                task_id=cached["task_id"],
                status="cached",
                jm_id=jm_id,
                jm_type="photo",
                album_name=cached["album_name"],
                album_author=cached["album_author"],
                file_count=cached["file_count"],
                files=cached["files"],
            )

    # 2. 执行下载
    task_id = f"photo_{jm_id}_{uuid.uuid4().hex[:8]}"
    download_dir = config.download_root / task_id
    download_dir.mkdir(parents=True, exist_ok=True)

    task = {
        "task_id": task_id,
        "status": "downloading",
        "jm_id": jm_id,
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
                jmcomic.download_photo_async(jm_id, option=option),
                timeout=config.download_timeout,
            )

            files = _collect_files(downloader, download_dir, max_pages)
            task["status"] = "success"
            task["file_count"] = len(files)
            task["files"] = files

            _cache_put(jm_id, task)

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


def _check_cached_pdf(task_id: str, task: dict) -> Optional[Path]:
    """检查是否已有缓存的 PDF"""
    pdf_path = config.temp_root / task_id / f"{task['jm_id']}.pdf"
    return pdf_path if pdf_path.exists() else None


def _check_cached_zip(task_id: str, task: dict) -> Optional[Path]:
    """检查是否已有缓存的 ZIP"""
    zip_path = config.temp_root / task_id / f"{task['jm_id']}.zip"
    return zip_path if zip_path.exists() else None


@app.post("/api/export/pdf")
async def export_pdf(task_id: str = Query(...)):
    """将已下载的图片合并为 PDF（带缓存）"""
    task = _active_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("success", "cached"):
        raise HTTPException(400, "任务未完成，无法导出")

    # 检查已有 PDF 缓存
    cached = _check_cached_pdf(task_id, task)
    if cached:
        return {"pdf_path": str(cached), "task_id": task_id, "cached": True}

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

        return {"pdf_path": str(pdf_path), "task_id": task_id, "cached": False}

    except Exception as e:
        raise HTTPException(500, f"PDF 导出失败: {e}")


@app.post("/api/export/zip")
async def export_zip(task_id: str = Query(...)):
    """将已下载的图片打包为 ZIP（带缓存）"""
    task = _active_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("success", "cached"):
        raise HTTPException(400, "任务未完成，无法导出")

    cached = _check_cached_zip(task_id, task)
    if cached:
        return {"zip_path": str(cached), "task_id": task_id, "cached": True}

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

        return {"zip_path": str(zip_path), "task_id": task_id, "cached": False}

    except Exception as e:
        raise HTTPException(500, f"ZIP 打包失败: {e}")


# ================================================================
#  Task 管理
# ================================================================


@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    task = _active_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return TaskStatus(**task)


@app.delete("/api/task/{task_id}")
async def delete_task(task_id: str):
    """删除任务（同时清除缓存和文件）"""
    task = _active_tasks.pop(task_id, None)
    if not task:
        raise HTTPException(404, "任务不存在")

    jm_id = task.get("jm_id", "")
    if jm_id:
        _cache_remove(jm_id)

    dl_dir = Path(task["download_dir"])
    if dl_dir.exists():
        shutil.rmtree(dl_dir, ignore_errors=True)

    temp_dir = config.temp_root / task_id
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)

    return {"ok": True}


# ================================================================
#  文件服务
# ================================================================


@app.get("/api/file/{task_id}/{file_name:path}")
async def serve_file(task_id: str, file_name: str):
    task = _active_tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    file_path = Path(task["download_dir"]) / file_name
    if not file_path.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(file_path)


@app.get("/api/export/download/{task_id}/{file_name:path}")
async def serve_export_file(task_id: str, file_name: str):
    file_path = config.temp_root / task_id / file_name
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
    print(f"Cache TTL: {config.cache_ttl_hours}h, cleanup every {config.cache_cleanup_interval}s")

    uvicorn.run(app, host=host, port=port, log_level="info")
