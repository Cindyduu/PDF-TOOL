"""应用配置与限额。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "PDF 自动设置工具"
APP_VERSION = "0.1.0"
SCHEMA_VERSION = 1
BACKUP_FORMAT_VERSION = 1

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

ENV_VAR_DIR = "PDFTOOL_VAR_DIR"
ENV_PORT = "PDFTOOL_PORT"
ENV_STATIC_DIR = "PDFTOOL_STATIC_DIR"


@dataclass(frozen=True)
class Limits:
    """各类硬性限额。恢复包校验与上传校验共用这里的数值。"""

    max_upload_bytes: int = 200 * 1024 * 1024
    # 批量上传一次最多几个文件。定 20 不是随手取的：
    # 每个任务都要独立解析标签与页数，一次上百个会让"上传"这一步长时间无响应；
    # 20 个足够覆盖"一批同类报告"的真实场景，又能保证失败提示是明确的一次性拒绝。
    max_batch_files: int = 20
    max_pages: int = 5000
    max_zip_entries: int = 20_000
    max_zip_total_bytes: int = 8 * 1024**3
    max_zip_entry_bytes: int = 2 * 1024**3
    max_zip_ratio: float = 200.0
    upload_chunk_bytes: int = 1024 * 1024
    event_log_max_bytes: int = 8 * 1024 * 1024


@dataclass
class Settings:
    var_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    limits: Limits = field(default_factory=Limits)
    app_version: str = APP_VERSION
    schema_version: int = SCHEMA_VERSION
    backup_format_version: int = BACKUP_FORMAT_VERSION

    # ---- 派生路径 -------------------------------------------------------
    @property
    def generations_dir(self) -> Path:
        return self.var_dir / "generations"

    @property
    def backups_dir(self) -> Path:
        return self.var_dir / "backups"

    @property
    def tmp_dir(self) -> Path:
        return self.var_dir / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.var_dir / "logs"

    @property
    def locks_dir(self) -> Path:
        return self.var_dir / "locks"

    @property
    def active_path(self) -> Path:
        return self.var_dir / "active.json"

    @property
    def static_dir(self) -> Path:
        """静态资源目录。三级解析：显式覆盖 → 冻结态 → 源码态。

        PyInstaller onedir 打包后 `app/static` 被释放到 `sys._MEIPASS` 下，
        源码态则与 `app/config.py` 同级的 `static/`。两级都探不到时退回源码态，
        让调用方在挂载阶段拿到明确的「目录不存在」错误，而不是静默 404。
        """
        override = os.environ.get(ENV_STATIC_DIR)
        if override:
            return Path(override).expanduser().resolve()
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            frozen = Path(meipass) / "app" / "static"
            if frozen.is_dir():
                return frozen
        return Path(__file__).resolve().parent / "static"

    def ensure_dirs(self) -> None:
        for directory in (
            self.var_dir,
            self.generations_dir,
            self.backups_dir,
            self.tmp_dir,
            self.logs_dir,
            self.locks_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("服务只允许绑定 127.0.0.1")
        if not (1 <= int(self.port) <= 65535):
            raise ValueError(f"端口超出范围：{self.port}")


def default_var_dir() -> Path:
    override = os.environ.get(ENV_VAR_DIR)
    if override:
        return Path(override).expanduser()
    return Path.cwd() / "var"


def load_settings(var_dir: Path | str | None = None, port: int | None = None) -> Settings:
    resolved = Path(var_dir) if var_dir is not None else default_var_dir()
    resolved = resolved.expanduser().resolve()
    if port is None:
        port = int(os.environ.get(ENV_PORT, DEFAULT_PORT))
    settings = Settings(var_dir=resolved, port=port)
    settings.validate()
    return settings
