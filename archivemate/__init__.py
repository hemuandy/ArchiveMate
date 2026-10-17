"""ArchiveMate (归巢) —— 把学校下发的文件自动归档并推送到 GitHub 私有仓库。

运行时依赖：Python 3.10+ 与 PySide6（仅 GUI 需要）。核心逻辑仅用标准库。
"""

__version__ = "1.2.0"
APP_NAME = "ArchiveMate"
APP_DISPLAY_NAME = "ArchiveMate 归巢"
CREDENTIAL_TARGET = "ArchiveMate/github-token"
CONFIG_SCHEMA_VERSION = 1
