"""命令行入口。

GUI 之外保留一套完整的无界面操作，既方便自动化测试，也方便用户在
没有图形界面的机器（比如服务器或排障时）处理队列。

    python -m archivemate.cli status
    python -m archivemate.cli add ~/Downloads/数学课件.pptx
    python -m archivemate.cli sync
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from . import __version__, app_paths, classify, credentials, logging_setup
from .config import (SYNC_SCHEDULE_CHOICES, Config,
                     load as load_config, save as save_config)
from .fsutil import human_size
from .schedule import SCHEDULE_KEYS, schedule_label
from .tracker import ORIGIN_LABELS, STATUS_LABELS

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: --config 指定的配置文件；None 表示用便携默认位置。
_CONFIG_PATH: Path | None = None


def _save(cfg: Config) -> Path:
    return save_config(cfg, _CONFIG_PATH)


# ----------------------------------------------------------------------
# 输出小工具
# ----------------------------------------------------------------------


def _configure_stdio() -> None:
    """Windows 控制台默认是 GBK，直接打印中文/符号会崩，这里统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _echo(message: str = "") -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding, "replace"),
              flush=True)


def _header(title: str) -> None:
    _echo()
    _echo(f"== {title} ==")


def _table(rows: list[tuple[str, str]], indent: str = "  ") -> None:
    if not rows:
        _echo(f"{indent}（无）")
        return
    width = max(len(str(key)) for key, _ in rows)
    for key, value in rows:
        _echo(f"{indent}{str(key).ljust(width)}  {value}")


# ----------------------------------------------------------------------
# 各子命令
# ----------------------------------------------------------------------


def cmd_status(args, service) -> int:
    report = service.environment_report()
    cfg = service.config

    if args.json:
        payload = {
            "version": __version__,
            "environment": report,
            "config_path": str(app_paths.config_path()),
            "repository": {
                "slug": cfg.repository.slug if cfg.repository.is_configured() else "",
                "branch": cfg.repository.branch,
                "remote": cfg.repository.resolved_remote_url(),
            },
            "archive_folder": cfg.archive_folder,
            "watch_folders": cfg.watch_folders,
            "file_types": cfg.file_types,
            "sync_schedule": cfg.sync_schedule,
            "has_token": bool(service.engine.token),
            "queue": [
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "dest_rel": item.dest_rel,
                    "size": item.size,
                    "status": item.status,
                    "origin": item.origin,
                    "error": item.error,
                }
                for item in service.tracker.items
            ],
            "history_count": len(service.tracker.history),
            "stats": service.tracker.stats,
        }
        _echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    _header(f"ArchiveMate 归巢 v{__version__}")
    _table([
        ("配置目录", report["data_root"]),
        ("便携模式", report["portable"]),
        ("配置文件", report["config_path"]),
        ("状态文件", report["state_path"]),
        ("日志文件", report["log_path"]),
        ("git", report["git"]),
        ("凭据后端", report["credential_backend"]),
    ])

    _header("仓库")
    _table([
        ("仓库", cfg.repository.slug if cfg.repository.is_configured() else "（未配置）"),
        ("分支", cfg.repository.branch),
        ("远端", cfg.repository.resolved_remote_url() if cfg.repository.is_configured() else "-"),
        ("Token", "已保存" if service.engine.token else "未设置"),
        ("代理", cfg.git.proxy or "（直连）"),
    ])

    _header("行为")
    _table([
        ("监控文件夹", f"{len(cfg.watch_folders)} 个"),
        ("目标类型", " ".join(cfg.file_types) or "（空）"),
        ("归档文件夹", cfg.archive_folder or "（未设置）"),
        ("定时同步", cfg.interval_label()),
    ])
    for raw, ok, note in service.watch_folder_status():
        _echo(f"    {'✓' if ok else '✗'} {raw}  — {note}")

    _header(f"待推送队列（{len(service.tracker.items)}）")
    if service.tracker.items:
        for item in service.tracker.items:
            status = STATUS_LABELS.get(item.status, item.status)
            origin = ORIGIN_LABELS.get(item.origin, item.origin)
            _echo(f"  [{status}] {item.dest_rel}")
            _echo(f"           {human_size(item.size)} · {origin} · {item.source}")
            if item.error:
                _echo(f"           ⚠ {item.error}")
            if item.note:
                _echo(f"           ⓘ {item.note}")
    else:
        _echo("  （空）")

    _header("最近完成")
    for entry in service.tracker.history[-10:][::-1]:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.finished_at))
        _echo(f"  {stamp}  {entry.dest_rel}  →  {entry.archived_to or '(未归档)'}")
    if not service.tracker.history:
        _echo("  （暂无）")
    return EXIT_OK


def cmd_doctor(args, service) -> int:
    report = service.environment_report()
    problems = service.engine.validate()
    _header("环境自检")
    _table(list(report.items()))
    if problems:
        _header("待解决的问题")
        for problem in problems:
            _echo(f"  ✗ {problem}")
        return EXIT_FAIL
    _echo()
    _echo("  ✓ 一切就绪，可以同步。")
    return EXIT_OK


def cmd_add(args, service) -> int:
    accepted = 0
    for raw in args.paths:
        target = Path(raw).expanduser()
        if target.is_dir():
            found = 0
            for child in sorted(target.rglob("*")):
                if not child.is_file():
                    continue
                if not service.config.matches_type(child.name):
                    continue
                found += 1
                ok, reason = service.ingest(child, origin="drop")
                if ok:
                    accepted += 1
                    _echo(f"  + {child.name}  ({reason})")
                elif args.verbose:
                    _echo(f"  - {child.name}  ({reason})")
            if not found:
                _echo(f"  ! {target} 里没有匹配的文件类型")
        else:
            ok, reason = service.ingest(target, origin="drop")
            if ok:
                accepted += 1
                _echo(f"  + {target.name}  ({reason})")
            else:
                _echo(f"  - {target.name}  ({reason})")
    _echo(f"已加入队列：{accepted} 个文件。")
    return EXIT_OK if accepted else EXIT_FAIL


def cmd_scan(args, service) -> int:
    found, accepted = service.scan_existing()
    _echo(f"扫描到 {found} 个候选文件，新增 {accepted} 个待推送项。")
    return EXIT_OK


def cmd_queue(args, service) -> int:
    items = service.tracker.items
    if args.json:
        _echo(json.dumps([item.to_dict() for item in items],
                         ensure_ascii=False, indent=2))
        return EXIT_OK
    if not items:
        _echo("待推送队列为空。")
        return EXIT_OK
    for item in items:
        status = STATUS_LABELS.get(item.status, item.status)
        _echo(f"{item.item_id}  [{status}]  {human_size(item.size):>10}  {item.dest_rel}")
    return EXIT_OK


def cmd_sync(args, service) -> int:
    problems = service.engine.validate()
    if problems:
        for problem in problems:
            _echo(f"✗ {problem}")
        return EXIT_FAIL

    outcome = service.sync_blocking()
    _echo()
    _echo(f"{'✓' if outcome.ok else '✗'} {outcome.message}")
    if outcome.commit:
        _echo(f"  提交：{outcome.commit[:12]}")
    for item, target in outcome.archived:
        _echo(f"  已归档：{item.dest_rel} → {target}")
    for item, reason in outcome.blocked:
        _echo(f"  已阻止：{item.name} — {reason}")
    for item, reason in outcome.skipped:
        _echo(f"  已跳过：{item.name} — {reason}")
    for item, reason in outcome.failed:
        _echo(f"  失败：{item.name} — {reason}")
    return EXIT_OK if outcome.ok else EXIT_FAIL


def cmd_watch(args, service) -> int:
    _echo("开始监控（Ctrl+C 结束）……")
    for raw, ok, note in service.watch_folder_status():
        _echo(f"  {'✓' if ok else '✗'} {raw} — {note}")
    service.start()
    try:
        while True:
            time.sleep(0.5)
            for event in service.drain_events():
                if event.kind == "log":
                    _echo(f"  · {event.payload}")
    except KeyboardInterrupt:
        _echo()
        _echo("正在停止……")
    finally:
        service.stop()
    return EXIT_OK


def cmd_remove(args, service) -> int:
    count = service.remove_items(args.item_ids)
    _echo(f"已移除 {count} 个待推送项。")
    return EXIT_OK if count else EXIT_FAIL


def cmd_clear(args, service) -> int:
    if not args.yes:
        _echo("这会清空整个待推送队列（不会删除本地文件）。加 --yes 确认。")
        return EXIT_USAGE
    count = service.clear_queue()
    _echo(f"已清空 {count} 个待推送项。")
    return EXIT_OK


def cmd_retry(args, service) -> int:
    ids = args.item_ids or [item.item_id for item in service.tracker.items
                            if item.status == "failed"]
    count = service.mark_pending_again(ids)
    _echo(f"已把 {count} 个文件重新排入待推送。")
    return EXIT_OK


def cmd_clear_archive(args, service) -> int:
    try:
        files, size = service.archive_summary()
    except OSError as exc:
        _echo(f"✗ 无法读取归档文件夹：{exc}")
        return EXIT_FAIL
    root = service.config.archive_folder or "(默认)"
    _echo(f"归档文件夹：{root}")
    _echo(f"当前占用：{files} 个文件，{human_size(size)}")
    if not args.yes:
        _echo("这会永久删除归档文件夹里的所有内容。加 --yes 确认。")
        return EXIT_USAGE
    try:
        removed, freed = service.clear_archive()
    except OSError as exc:
        _echo(f"✗ {exc}")
        return EXIT_FAIL
    _echo(f"✓ 已删除 {removed} 个文件，释放 {human_size(freed)}。")
    return EXIT_OK


def cmd_config(args, service) -> int:
    cfg = service.config
    if args.set:
        # --set 可以重复出现，每次可以带多个 KEY=VALUE
        pairs = [pair for group in args.set for pair in group]
        for pair in pairs:
            if "=" not in pair:
                _echo(f"✗ 参数格式应为 KEY=VALUE：{pair}")
                return EXIT_USAGE
            key, value = pair.split("=", 1)
            error = _apply_setting(cfg, key.strip(), value)
            if error:
                _echo(f"✗ {error}")
                return EXIT_FAIL
        cfg.normalize()
        _save(cfg)
        _echo(f"已更新配置：{_CONFIG_PATH or app_paths.config_path()}")
        return EXIT_OK
    if args.json:
        _echo(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))
    else:
        _echo(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def _apply_setting(cfg: Config, key: str, value: str) -> str:
    if key in ("watch_folders", "file_types"):
        if key == "file_types":
            parts = [classify.normalize_extension(p)
                     for p in re.split(r"[,;\s]+", value) if p.strip()]
        else:
            parts = [p.strip() for p in value.split(os.pathsep)
                     if p.strip() and p.strip() != ","]
        setattr(cfg, key, parts)
        return ""
    if key == "archive_folder":
        cfg.archive_folder = value
        return ""
    if key in ("sync_schedule", "sync_interval"):
        value = value.strip().lower()
        if value not in SCHEDULE_KEYS:
            allowed = ", ".join(SCHEDULE_KEYS)
            return f"{key} 只能是：{allowed}"
        cfg.sync_schedule = value
        return ""
    if key == "repository":
        if "/" not in value:
            return "repository 格式应为 owner/name"
        owner, name = value.split("/", 1)
        cfg.repository.owner = owner.strip()
        cfg.repository.name = name.strip()
        return ""
    if key == "branch":
        cfg.repository.branch = value.strip() or "main"
        return ""
    if key == "remote_url":
        cfg.repository.remote_url = value.strip()
        return ""
    if key == "proxy":
        cfg.git.proxy = value.strip()
        return ""
    if key == "git_executable":
        cfg.git.executable = value.strip()
        return ""
    if key == "ssl_backend":
        backend = value.strip().lower()
        if backend not in ("", "auto", "schannel", "openssl"):
            return "ssl_backend 只能是 auto、schannel 或 openssl"
        cfg.git.ssl_backend = "" if backend == "auto" else backend
        return ""
    if key == "date_source":
        if value not in ("mtime", "now"):
            return "date_source 只能是 mtime 或 now"
        cfg.date_source = value
        return ""
    if key in ("network_timeout", "network_timeout_seconds"):
        try:
            cfg.git.network_timeout_seconds = int(value)
        except ValueError:
            return "network_timeout 需要一个整数（秒，30-3600）"
        return ""
    if key in ("stall_timeout", "stall_timeout_seconds"):
        try:
            cfg.git.stall_timeout_seconds = int(value)
        except ValueError:
            return "stall_timeout 需要一个整数（秒，0 表示关闭）"
        return ""
    return f"未知的配置项：{key}"


def cmd_set_token(args, service) -> int:
    if args.delete:
        if credentials.delete_token():
            _echo("✓ 已从系统密钥链删除 Token。")
            return EXIT_OK
        _echo("没有找到已保存的 Token。")
        return EXIT_FAIL

    token = args.token
    if not token:
        import getpass
        try:
            token = getpass.getpass("粘贴 GitHub Token（输入不回显）：").strip()
        except (EOFError, KeyboardInterrupt):
            _echo()
            return EXIT_FAIL
    if not token:
        _echo("✗ 没有输入内容。")
        return EXIT_FAIL

    ok, message = credentials.set_token(token)
    if ok:
        _echo(f"✓ {message}")
        _echo(f"  Token 存放在：{credentials.backend_name()}")
        return EXIT_OK
    credentials.remember_for_session(token)
    _echo(f"⚠ {message}")
    _echo("  已降级为「仅本次运行有效」，没有写入明文文件。")
    return EXIT_FAIL


def cmd_init(args, service) -> int:
    cfg = service.config
    if args.repository:
        if "/" not in args.repository:
            _echo("✗ --repository 格式应为 owner/name")
            return EXIT_USAGE
        owner, name = args.repository.split("/", 1)
        cfg.repository.owner, cfg.repository.name = owner.strip(), name.strip()
    if args.branch:
        cfg.repository.branch = args.branch
    if args.remote_url:
        cfg.repository.remote_url = args.remote_url
    if args.archive:
        cfg.archive_folder = str(Path(args.archive).expanduser())
    if args.watch:
        cfg.watch_folders = [str(Path(p).expanduser()) for p in args.watch]
    if args.types:
        cfg.file_types = [classify.normalize_extension(t) for t in args.types]
    if args.schedule is not None:
        cfg.sync_schedule = args.schedule
    if args.proxy is not None:
        cfg.git.proxy = args.proxy
    cfg.normalize()
    _save(cfg)
    _header("配置已写入")
    _table([
        ("配置文件", str(_CONFIG_PATH or app_paths.config_path())),
        ("仓库", cfg.repository.slug or "（未配置）"),
        ("归档文件夹", cfg.archive_folder or "（未设置）"),
        ("监控文件夹", ", ".join(cfg.watch_folders) or "（空）"),
        ("目标类型", " ".join(cfg.file_types)),
    ])
    if not service.engine.token:
        _echo()
        _echo("提示：还没有保存 Token，运行 `set-token` 写入系统密钥链。")
    return EXIT_OK


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archivemate",
        description="ArchiveMate 归巢 —— 把学校资料归档到 GitHub 私有仓库。",
    )
    parser.add_argument("--version", action="version", version=f"ArchiveMate {__version__}")
    parser.add_argument("--home", metavar="DIR",
                        help="把配置/状态/缓存放到指定目录（默认跟着软件走）")
    parser.add_argument("--config", metavar="FILE",
                        help="指定配置文件路径（等价于 --home 到它所在目录）")

    sub = parser.add_subparsers(dest="command", metavar="命令")

    p = sub.add_parser("status", help="显示环境、配置与队列概况")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="只做环境自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("add", help="把文件或目录加入待推送队列")
    p.add_argument("paths", nargs="+")
    p.add_argument("-v", "--verbose", action="store_true", help="显示被跳过的文件")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("scan", help="扫描监控目录并抓取已有的目标文件")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("sync", help="立即推送并归档")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("watch", help="前台常驻监控")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("queue", help="打印待推送队列")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("remove", help="从队列移除条目")
    p.add_argument("item_ids", nargs="+")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("clear", help="清空待推送队列")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("retry", help="把失败项重新排入队列")
    p.add_argument("item_ids", nargs="*")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("clear-archive", help="清空归档文件夹")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_clear_archive)

    p = sub.add_parser("config", help="查看或修改配置")
    p.add_argument("--set", action="append", nargs="+", metavar="KEY=VALUE",
                   help="可重复：--set key=value [key=value ...]")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("set-token", help="把 GitHub Token 写入系统密钥链")
    p.add_argument("--token", help="直接给出 Token（不建议，会留在命令历史里）")
    p.add_argument("--delete", action="store_true", help="删除已保存的 Token")
    p.set_defaults(func=cmd_set_token)

    p = sub.add_parser("init", help="非交互式写入配置")
    p.add_argument("--repository", metavar="OWNER/NAME")
    p.add_argument("--branch")
    p.add_argument("--remote-url", dest="remote_url")
    p.add_argument("--archive", metavar="DIR")
    p.add_argument("--watch", action="append", metavar="DIR")
    p.add_argument("--types", nargs="+")
    p.add_argument("--schedule", choices=list(SCHEDULE_KEYS),
                   help="定时同步：off / daily（每天 0 点）/ weekly（周一 0 点）/ monthly（每月 1 号 0 点）")
    p.add_argument("--proxy")
    p.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    # 路径相关的一切都依赖 ARCHIVEMATE_HOME，所以必须在导入服务层之前设定。
    global _CONFIG_PATH
    if args.config:
        _CONFIG_PATH = Path(args.config).expanduser().resolve()
        os.environ["ARCHIVEMATE_HOME"] = str(_CONFIG_PATH.parent)
    elif args.home:
        os.environ["ARCHIVEMATE_HOME"] = str(Path(args.home).expanduser().resolve())
    app_paths.reset_cache()

    logging_setup.setup()

    from .errors import ConfigError
    from .service import ArchiveMateService

    try:
        cfg = load_config(_CONFIG_PATH) if _CONFIG_PATH else load_config()
    except ConfigError as exc:
        _echo(f"✗ {exc}")
        return EXIT_FAIL

    service = ArchiveMateService(cfg, auto_start_watcher=False)
    try:
        return args.func(args, service)
    except KeyboardInterrupt:
        _echo()
        _echo("已中断。")
        return EXIT_FAIL


def _console() -> None:
    sys.exit(main())


if __name__ == "__main__":
    _console()
