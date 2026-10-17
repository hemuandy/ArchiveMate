"""ArchiveMate 异常层级。"""

from __future__ import annotations


class ArchiveMateError(Exception):
    """所有本软件自有异常的基类。"""


class ConfigError(ArchiveMateError):
    """配置文件损坏或字段非法。"""


class CredentialError(ArchiveMateError):
    """系统密钥链不可用或读写失败。"""


class GitError(ArchiveMateError):
    """调用 git 命令失败。"""

    def __init__(self, message: str, *, command: list[str] | None = None,
                 returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.command = command or []
        self.returncode = returncode
        self.stderr = stderr


class GitHubError(ArchiveMateError):
    """GitHub REST API 调用失败。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PushRejected(ArchiveMateError):
    """远端有新提交，推送被拒绝（可 fetch 后重试）。"""


class FileTooLarge(ArchiveMateError):
    """文件体积超过阻塞阈值。"""
