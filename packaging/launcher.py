"""打包后的程序入口。

PyInstaller 需要一个「顶层脚本」当入口，而 `archivemate/__main__.py` 用的是相对
导入，被当成 `__main__` 执行时会失败，所以这里单独留一个薄入口。

默认进图形界面；带 `--cli`（或 `--help` / `--version`）时转发给命令行接口，
方便在打包版里做排障。
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # 冻结成 exe 后必须调用，否则子进程会重新执行整个程序
    multiprocessing.freeze_support()

    arguments = sys.argv[1:]
    wants_cli = (
        "--cli" in arguments
        or any(flag in arguments for flag in ("-h", "--help", "--version"))
    )
    if wants_cli:
        from archivemate.cli import main as cli_main

        return cli_main([value for value in arguments if value != "--cli"])

    from archivemate.gui import run

    return run(arguments)


if __name__ == "__main__":
    sys.exit(main())
