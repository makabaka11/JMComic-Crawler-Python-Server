"""
JMComic AstrBot 插件 - 配置管理
"""
from __future__ import annotations

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context


class PluginConfig:
    """插件配置管理器"""

    _plugin_name: str = "astrbot_plugin_jmcomic"

    def __init__(self, cfg: AstrBotConfig, context: Context):
        self._cfg = cfg
        self._context = context

    # ---- Server 连接 ----

    @property
    def server_url(self) -> str:
        return self._cfg.get("server", {}).get("url", "http://127.0.0.1:8899")

    # ---- 功能开关 ----

    @property
    def enabled(self) -> bool:
        return self._cfg.get("enabled", True)

    @property
    def auto_detect(self) -> bool:
        return self._cfg.get("auto_detect", False)

    # ---- 下载参数（传递给 Server） ----

    @property
    def download_max_pages(self) -> int:
        return self._cfg.get("download", {}).get("max_pages", 100)

    @property
    def download_timeout(self) -> int:
        return self._cfg.get("download", {}).get("timeout", 300)

    # ---- 发送配置 ----

    @property
    def send_mode(self) -> str:
        return self._cfg.get("send", {}).get("mode", "images")

    @property
    def send_max_images_per_message(self) -> int:
        return self._cfg.get("send", {}).get("max_images_per_message", 5)

    @property
    def send_cover_first(self) -> bool:
        return self._cfg.get("send", {}).get("send_cover_first", True)

    # ---- 频率限制 ----

    @property
    def rate_limit_enabled(self) -> bool:
        return self._cfg.get("rate_limit", {}).get("enabled", True)

    @property
    def rate_limit_cooldown(self) -> int:
        return self._cfg.get("rate_limit", {}).get("cooldown_seconds", 60)

    def save_config(self) -> None:
        if hasattr(self._cfg, "save_config"):
            self._cfg.save_config()
