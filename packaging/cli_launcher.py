"""打包后的命令行入口（ArchiveMate-命令行.exe）。

和图形版共用一个文件夹，所以读到的 config.json / state.json 是同一份。
"""

from __future__ import annotations

import multiprocessing
import sys

from archivemate.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
