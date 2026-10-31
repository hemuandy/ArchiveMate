"""ArchiveMate (归巢) —— 把学校下发的文件自动归档并推送到 GitHub 私有仓库。

运行时依赖：Python 3.10+ 与 PySide6（仅 GUI 需要）。核心逻辑仅用标准库。
"""

__version__ = "2.0.0"
APP_NAME = "ArchiveMate"
APP_DISPLAY_NAME = "ArchiveMate 归巢"
CREDENTIAL_TARGET = "ArchiveMate/github-token"
#: 3：分类目录从「日期/类型」改成「学年/学期/类型/科目」。
CONFIG_SCHEMA_VERSION = 3
