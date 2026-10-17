"""`python -m archivemate` 入口。

默认启动图形界面；如果 PySide6 不可用（或显式传入 --cli），
自动退回命令行，保证在任何环境下都还有得用。
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    force_cli = "--cli" in args
    if force_cli:
        args = [arg for arg in args if arg != "--cli"]
        from .cli import main as cli_main
        return cli_main(args)

    try:
        from .gui import run
    except ImportError as exc:
        print(f"无法加载图形界面（{exc}），改用命令行模式。", file=sys.stderr)
        from .cli import main as cli_main
        return cli_main(args)

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
