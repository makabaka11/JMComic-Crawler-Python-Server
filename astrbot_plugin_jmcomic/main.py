"""
JMComic AstrBot 插件 - 主入口
通过 HTTP 调用 JMComic Server，支持指令下载 + LLM 工具自主调用
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, Image, File

from .core.client import JMComicClient, TaskStatus
from .core.config import PluginConfig


@register(
    "astrbot_plugin_jmcomic",
    "makabaka11",
    "禁漫下载插件：通过 JMComic Server 下载禁漫本子/章节，自动发送到会话",
    "1.0.0",
)
class JMComicPlugin(Star):
    """禁漫下载 AstrBot 插件

    架构：插件(Client) ──HTTP──> JMComic Server ──> jmcomic 库

    - /jm <id>  命令式下载
    - LLM 工具自动调用（AI 自主判断并下载）
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.client = JMComicClient(self.cfg)
        self._download_locks: dict[str, asyncio.Lock] = {}
        self._user_cooldowns: dict[str, float] = {}

    async def initialize(self):
        """启动时检查 Server 连接"""
        if await self.client.health_check():
            logger.info(f"[JMComic] Server 连接正常: {self.cfg.server_url}")
        else:
            logger.warning(
                f"[JMComic] 无法连接到 Server: {self.cfg.server_url}，"
                f"请确保已启动 jmcomic_server.py"
            )

    async def terminate(self):
        """插件卸载"""
        await self.client.close()
        logger.info("[JMComic] 插件已卸载")

    # ================================================================
    #  命令处理
    # ================================================================

    @filter.command("jm")
    async def on_jm_command(self, event: AstrMessageEvent):
        """
        /jm <id>          下载本子
        /jm photo <id>    下载章节
        /jm <链接>        从链接下载
        """
        if not self.cfg.enabled:
            return

        message = event.message_str.strip()
        args = message.split(maxsplit=1)

        if len(args) < 2:
            yield event.plain_result(
                "📖 **禁漫下载帮助**\n"
                "• `/jm <ID>`  下载指定本子\n"
                "• `/jm photo <ID>`  下载指定章节\n"
                "• `/jm <链接>`  从禁漫链接下载\n"
                "• 直接发禁漫链接（需开启自动识别）\n\n"
                f"🔗 Server: `{self.cfg.server_url}`\n"
                "示例: `/jm 123456`"
            )
            return

        sub_cmd = args[1].strip()
        jm_type = "album"

        if sub_cmd.lower().startswith("photo "):
            jm_type = "photo"
            sub_cmd = sub_cmd[6:].strip()
        elif sub_cmd.lower() == "photo":
            yield event.plain_result("❌ 请提供章节 ID，例如: `/jm photo 123456`")
            return

        extracted = self.client.extract_jm_id(sub_cmd)
        if extracted is None:
            yield event.plain_result(f"❌ 无法识别禁漫 ID 或链接: `{sub_cmd}`")
            return

        jm_id, detected_type = extracted
        if jm_type == "album" and detected_type == "photo":
            jm_type = "photo"

        async for result in self._handle_download(event, jm_id, jm_type):
            yield result

    # ================================================================
    #  自动识别
    # ================================================================

    @filter.regex(
        r'(?:https?://)?(?:www\.)?(?:18comic|jmcomic\d*|jmcmic)\.[a-zA-Z]+/(?:album|photo)/(\d+)'
    )
    async def on_jm_link(self, event: AstrMessageEvent):
        """自动识别禁漫链接"""
        if not self.cfg.enabled or not self.cfg.auto_detect:
            return

        extracted = self.client.extract_jm_id(event.message_str)
        if extracted is None:
            return

        jm_id, jm_type = extracted
        async for result in self._handle_download(event, jm_id, jm_type):
            yield result

    # ================================================================
    #  黑名单管理
    # ================================================================

    @filter.command("jmban")
    async def on_jmban(self, event: AstrMessageEvent):
        """
        添加黑名单
        /jmban <ID> [原因]   屏蔽指定 ID
        /jmban list          查看黑名单
        """
        if not self.cfg.enabled:
            return

        args = event.message_str.strip().split(maxsplit=1)

        if len(args) < 2:
            yield event.plain_result(
                "📖 **黑名单帮助**\n"
                "• `/jmban <ID> [原因]`  屏蔽指定 ID\n"
                "• `/jmban list`  查看黑名单\n"
                "示例: `/jmban 123456 违规内容`"
            )
            return

        sub_cmd = args[1].strip()

        # 查看黑名单
        if sub_cmd.lower() == "list":
            entries = await self.client.list_blacklist()
            if not entries:
                yield event.plain_result("📋 黑名单为空")
                return
            lines = ["📋 **黑名单列表**"]
            for e in entries:
                t = e.get("added_time", "")
                lines.append(f"• JM{e.get('jm_id')} — {e.get('reason', '')} ({t})")
            yield event.plain_result("\n".join(lines))
            return

        # 解析 ID + 原因
        parts = sub_cmd.split(maxsplit=1)
        extracted = self.client.extract_jm_id(parts[0])
        if extracted is None:
            yield event.plain_result(f"❌ 无法识别 ID: `{parts[0]}`")
            return

        jm_id, _ = extracted
        reason = parts[1].strip() if len(parts) > 1 else "manual"

        ok = await self.client.add_blacklist(jm_id, reason)
        if ok:
            yield event.plain_result(f"✅ 已将 **JM{jm_id}** 加入黑名单\n📝 原因: {reason}")
        else:
            yield event.plain_result(f"❌ 添加黑名单失败: JM{jm_id}")

    @filter.command("jmunban", alias={"jmuban"})
    async def on_jmunban(self, event: AstrMessageEvent):
        """移除黑名单"""
        if not self.cfg.enabled:
            return

        args = event.message_str.strip().split(maxsplit=1)

        if len(args) < 2:
            yield event.plain_result("用法: `/jmunban <ID>` — 从黑名单移除指定 ID")
            return

        extracted = self.client.extract_jm_id(args[1].strip())
        if extracted is None:
            yield event.plain_result(f"❌ 无法识别 ID: `{args[1]}`")
            return

        jm_id, _ = extracted
        ok = await self.client.remove_blacklist(jm_id)
        if ok:
            yield event.plain_result(f"✅ 已将 **JM{jm_id}** 移出黑名单")
        else:
            yield event.plain_result(f"❌ 移除黑名单失败: JM{jm_id}")

    # ================================================================
    #  LLM 工具
    # ================================================================

    @filter.llm_tool()
    async def jm_download_album(self, event: AstrMessageEvent, album_id: str = ""):
        """
        下载禁漫本子（album），根据用户提供的禁漫ID或链接下载本子，返回图片给用户。

        当用户要求下载某个禁漫本子、看某个本子、获取某个JM号的内容时调用此工具。

        Args:
            album_id(string): 禁漫本子ID（纯数字如 123456）或禁漫链接
        """
        if not self.cfg.enabled:
            return "禁漫下载插件未启用"

        extracted = self.client.extract_jm_id(album_id)
        if extracted is None:
            return f"无法从 \"{album_id}\" 中识别出禁漫本子ID，请提供有效的数字ID或禁漫链接"

        jm_id, _ = extracted
        return await self._llm_download(event, jm_id, "album")

    @filter.llm_tool()
    async def jm_download_photo(self, event: AstrMessageEvent, photo_id: str = ""):
        """
        下载禁漫的单个章节（photo），根据用户提供的章节ID或链接下载章节图片。

        当用户要求下载某个禁漫章节、某个特定话时调用此工具。

        Args:
            photo_id(string): 禁漫章节ID（纯数字）或禁漫章节链接
        """
        if not self.cfg.enabled:
            return "禁漫下载插件未启用"

        extracted = self.client.extract_jm_id(photo_id)
        if extracted is None:
            return f"无法从 \"{photo_id}\" 中识别出禁漫章节ID，请提供有效的数字ID或禁漫链接"

        jm_id, _ = extracted
        return await self._llm_download(event, jm_id, "photo")

    # ================================================================
    #  核心流程
    # ================================================================

    async def _llm_download(
        self, event: AstrMessageEvent, jm_id: str, jm_type: str
    ) -> str:
        """LLM 工具用的下载流程（返回字符串）"""
        user_id = event.get_sender_id()

        # 频率检查
        allowed, remaining = self._check_rate_limit(user_id)
        if not allowed:
            return f"下载频率过快，请 {remaining:.0f} 秒后再试"

        # 并发锁
        lock = self._download_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            return "你有一个下载任务正在进行中，请等待完成后再试"

        async with lock:
            self._mark_used(user_id)

            # 调 Server
            if jm_type == "album":
                task = await self.client.download_album(jm_id)
            else:
                task = await self.client.download_photo(jm_id)

            if not task.ok:
                return f"下载失败: {task.error}"

            info = await self._send_task_results(event, task)
            return info

    async def _handle_download(
        self, event: AstrMessageEvent, jm_id: str, jm_type: str
    ):
        """命令用的下载流程（生成器）"""
        user_id = event.get_sender_id()

        allowed, remaining = self._check_rate_limit(user_id)
        if not allowed:
            yield event.plain_result(f"⏳ 请稍等 **{remaining:.0f}** 秒后再试~")
            return

        lock = self._download_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            yield event.plain_result("⏳ 你有一个下载任务正在进行中，请等待完成后再试~")
            return

        type_label = "本子" if jm_type == "album" else "章节"

        async with lock:
            self._mark_used(user_id)

            if jm_type == "album":
                task = await self.client.download_album(jm_id)
            else:
                task = await self.client.download_photo(jm_id)

            if not task.ok:
                yield event.plain_result(f"❌ 下载失败: {task.error}")
                return

            if task.status == "cached":
                yield event.plain_result(
                    f"💾 命中缓存，直接发送: **JM{jm_id}**\n📄 共 {task.file_count} 张图片"
                )
            else:
                yield event.plain_result(
                    f"✅ 下载完成: **JM{jm_id}**\n📄 共 {task.file_count} 张图片"
                )

            async for result in self._yield_task_results(event, task):
                yield result

    # ================================================================
    #  发送
    # ================================================================

    async def _send_task_results(
        self, event: AstrMessageEvent, task: TaskStatus
    ) -> str:
        """发送任务结果（LLM 工具用，event.send）"""
        if not task.files:
            await event.send(event.plain_result("⚠️ 下载完成但没有获取到图片文件"))
            return "下载完成但没有图片"

        info_text = self._build_info_text(task)
        await event.send(event.plain_result(info_text))

        await self._send_files(event, task)
        return info_text

    async def _yield_task_results(self, event: AstrMessageEvent, task: TaskStatus):
        """发送任务结果（命令用，yield）"""
        if not task.files:
            yield event.plain_result("⚠️ 下载完成但没有获取到图片文件")
            return

        yield event.plain_result(self._build_info_text(task))

        mode = self.cfg.send_mode

        if mode == "pdf":
            yield event.plain_result("📦 正在生成 PDF...")
            export = await self.client.export_pdf(task.task_id)
            if export.pdf_path:
                local_pdf = await self._fetch_export_file(task.task_id, export.pdf_path)
                if local_pdf:
                    yield event.chain_result([
                        Plain(f"📕 PDF: JM{task.jm_id}.pdf"),
                        File(name=f"JM{task.jm_id}.pdf", file=local_pdf),
                    ])
                else:
                    yield event.plain_result("❌ PDF 文件下载失败")
            else:
                yield event.plain_result(f"❌ {export.error}")

        elif mode == "zip":
            yield event.plain_result("📦 正在打包 ZIP...")
            export = await self.client.export_zip(task.task_id)
            if export.zip_path:
                local_zip = await self._fetch_export_file(task.task_id, export.zip_path)
                if local_zip:
                    yield event.chain_result([
                        Plain(f"📦 ZIP: JM{task.jm_id}.zip"),
                        File(name=f"JM{task.jm_id}.zip", file=local_zip),
                    ])
                else:
                    yield event.plain_result("❌ ZIP 文件下载失败")
            else:
                yield event.plain_result(f"❌ {export.error}")

        elif mode == "images_and_pdf":
            async for r in self._yield_images_batched(event, task):
                yield r
            yield event.plain_result("📦 正在生成 PDF...")
            export = await self.client.export_pdf(task.task_id)
            if export.pdf_path:
                local_pdf = await self._fetch_export_file(task.task_id, export.pdf_path)
                if local_pdf:
                    yield event.chain_result([
                        Plain(f"📕 PDF: JM{task.jm_id}.pdf"),
                        File(name=f"JM{task.jm_id}.pdf", file=local_pdf),
                    ])

        else:
            # 默认逐张发图
            async for r in self._yield_images_batched(event, task):
                yield r

    async def _send_files(self, event: AstrMessageEvent, task: TaskStatus):
        """发送文件（LLM 工具用）"""
        mode = self.cfg.send_mode

        if mode == "pdf":
            export = await self.client.export_pdf(task.task_id)
            if export.pdf_path:
                local_pdf = await self._fetch_export_file(task.task_id, export.pdf_path)
                if local_pdf:
                    await event.send(event.chain_result([
                        Plain(f"📕 PDF: JM{task.jm_id}.pdf"),
                        File(name=f"JM{task.jm_id}.pdf", file=local_pdf),
                    ]))

        elif mode == "zip":
            export = await self.client.export_zip(task.task_id)
            if export.zip_path:
                local_zip = await self._fetch_export_file(task.task_id, export.zip_path)
                if local_zip:
                    await event.send(event.chain_result([
                        Plain(f"📦 ZIP: JM{task.jm_id}.zip"),
                        File(name=f"JM{task.jm_id}.zip", file=local_zip),
                    ]))

        elif mode == "images_and_pdf":
            await self._send_images(event, task)
            export = await self.client.export_pdf(task.task_id)
            if export.pdf_path:
                local_pdf = await self._fetch_export_file(task.task_id, export.pdf_path)
                if local_pdf:
                    await event.send(event.chain_result([
                        Plain(f"📕 PDF: JM{task.jm_id}.pdf"),
                        File(name=f"JM{task.jm_id}.pdf", file=local_pdf),
                    ]))

        else:
            await self._send_images(event, task)

    async def _yield_images_batched(self, event: AstrMessageEvent, task: TaskStatus):
        """逐批发送图片（yield 版）"""
        files = task.files
        total = len(files)
        max_per = self.cfg.send_max_images_per_message

        for i in range(0, total, max_per):
            batch = files[i : i + max_per]
            components = []

            for fpath in batch:
                local_path = await self._fetch_file(task.task_id, fpath)
                if local_path:
                    try:
                        components.append(Image.fromFileSystem(local_path))
                    except Exception as e:
                        logger.warning(f"[JMComic] 图片加载失败: {local_path}, {e}")

            if components:
                batch_info = f"📷 ({i + 1}-{min(i + len(batch), total)}/{total})"
                components.insert(0, Plain(batch_info))
                yield event.chain_result(components)

            if i + max_per < total:
                await asyncio.sleep(0.3)

    async def _send_images(self, event: AstrMessageEvent, task: TaskStatus):
        """逐批发送图片（event.send 版）"""
        files = task.files
        total = len(files)
        max_per = self.cfg.send_max_images_per_message

        for i in range(0, total, max_per):
            batch = files[i : i + max_per]
            components = []

            for fpath in batch:
                local_path = await self._fetch_file(task.task_id, fpath)
                if local_path:
                    try:
                        components.append(Image.fromFileSystem(local_path))
                    except Exception as e:
                        logger.warning(f"[JMComic] 图片加载失败: {local_path}, {e}")

            if components:
                batch_info = f"📷 ({i + 1}-{min(i + len(batch), total)}/{total})"
                components.insert(0, Plain(batch_info))
                await event.send(event.chain_result(components))

            if i + max_per < total:
                await asyncio.sleep(0.3)

    # ================================================================
    #  辅助方法
    # ================================================================

    def _build_info_text(self, task: TaskStatus) -> str:
        """构建信息文本"""
        if task.album_name:
            return (
                f"✅ 下载完成: **{task.album_name}**\n"
                f"📖 作者: {task.album_author}\n"
                f"📄 共 {task.file_count} 张图片"
            )
        return f"✅ 下载完成: **JM{task.jm_id}**\n📄 共 {task.file_count} 张图片"

    def _check_rate_limit(self, user_id: str) -> tuple[bool, float]:
        """检查频率限制"""
        if not self.cfg.rate_limit_enabled:
            return (True, 0)
        import time
        now = time.time()
        last = self._user_cooldowns.get(user_id, 0)
        elapsed = now - last
        cooldown = self.cfg.rate_limit_cooldown
        if elapsed < cooldown:
            return (False, cooldown - elapsed)
        return (True, 0)

    def _mark_used(self, user_id: str):
        """标记用户已使用"""
        import time
        self._user_cooldowns[user_id] = time.time()

    async def _fetch_file(
        self, task_id: str, remote_path: str
    ) -> Optional[str]:
        """从 Server 下载单个文件到本地临时目录，返回本地路径"""
        data = await self.client.download_file(task_id, remote_path)
        if data is None:
            return None

        fname = Path(remote_path).name
        tmp_path = os.path.join(tempfile.gettempdir(), f"jmcomic_{task_id}_{fname}")

        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            return tmp_path
        except Exception as e:
            logger.warning(f"[JMComic] 写入临时文件失败: {tmp_path}, {e}")
            return None

    async def _fetch_export_file(
        self, task_id: str, server_path: str
    ) -> Optional[str]:
        """从 Server 下载导出的 PDF/ZIP 文件到本地，返回本地路径"""
        data = await self.client.download_export_file(task_id, server_path)
        if data is None:
            return None

        fname = Path(server_path).name
        tmp_path = os.path.join(tempfile.gettempdir(), f"jmcomic_export_{task_id}_{fname}")

        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            return tmp_path
        except Exception as e:
            logger.warning(f"[JMComic] 写入导出文件失败: {tmp_path}, {e}")
            return None
