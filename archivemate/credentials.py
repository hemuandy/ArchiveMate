"""跨平台令牌存取：全部走操作系统密钥链，绝不落明文盘。

- Windows：凭据管理器（advapi32 的 CredWriteW / CredReadW / CredDeleteW）
- macOS：钥匙串（security 命令）
- Linux：libsecret（secret-tool 命令）

若某平台密钥链不可用，降级为「本次会话内存保存」，仍不写明文文件。
如果环境里恰好装了 keyring 库，也会优先使用它。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import os
import shutil
import subprocess
import sys
from typing import Optional

from .errors import CredentialError

SERVICE = "ArchiveMate"
ACCOUNT = "github-token"

_MEMORY_FALLBACK: dict[str, str] = {}
_UNSUPPORTED_MESSAGE = "当前平台没有可用的系统密钥链，Token 仅保存在本次运行的内存中。"


# --------------------------------------------------------------------------
# Windows 凭据管理器
# --------------------------------------------------------------------------

class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168

_advapi32 = None


def _windows_api():
    global _advapi32
    if _advapi32 is None:
        _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
        _advapi32.CredWriteW.restype = wintypes.BOOL
        _advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CredentialW)),
        ]
        _advapi32.CredReadW.restype = wintypes.BOOL
        _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        _advapi32.CredDeleteW.restype = wintypes.BOOL
        _advapi32.CredFree.argtypes = [ctypes.c_void_p]
        _advapi32.CredFree.restype = None
    return _advapi32


def _win_target(target: str) -> str:
    return target


def _win_write(target: str, secret: str, account: str) -> None:
    api = _windows_api()
    blob = secret.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    cred = _CredentialW()
    cred.Flags = 0
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = _win_target(target)
    cred.Comment = f"{SERVICE} GitHub Token"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = account
    cred.AttributeCount = 0
    cred.Attributes = None
    cred.TargetAlias = None
    if not api.CredWriteW(ctypes.byref(cred), 0):
        raise CredentialError(f"写入 Windows 凭据管理器失败（错误码 {ctypes.get_last_error()}）")


def _win_read(target: str) -> Optional[str]:
    api = _windows_api()
    pointer = ctypes.POINTER(_CredentialW)()
    if not api.CredReadW(_win_target(target), _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return None
        raise CredentialError(f"读取 Windows 凭据管理器失败（错误码 {code}）")
    try:
        cred = pointer.contents
        if not cred.CredentialBlob or not cred.CredentialBlobSize:
            return None
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        api.CredFree(pointer)


def _win_delete(target: str) -> None:
    api = _windows_api()
    if not api.CredDeleteW(_win_target(target), _CRED_TYPE_GENERIC, 0):
        code = ctypes.get_last_error()
        if code != _ERROR_NOT_FOUND:
            raise CredentialError(f"删除 Windows 凭据失败（错误码 {code}）")


# --------------------------------------------------------------------------
# macOS 钥匙串
# --------------------------------------------------------------------------

def _mac_write(target: str, secret: str, account: str) -> None:
    _run(["security", "delete-generic-password", "-s", target, "-a", account], check=False)
    _run(["security", "add-generic-password", "-s", target, "-a", account,
          "-w", secret, "-U", "-D", "application password",
          "-l", f"{SERVICE} GitHub Token"])


def _mac_read(target: str) -> Optional[str]:
    result = _run(["security", "find-generic-password", "-s", target,
                   "-a", ACCOUNT, "-w"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _mac_delete(target: str) -> None:
    _run(["security", "delete-generic-password", "-s", target, "-a", ACCOUNT], check=False)


# --------------------------------------------------------------------------
# Linux libsecret
# --------------------------------------------------------------------------

def _linux_write(target: str, secret: str, account: str) -> None:
    result = _run(["secret-tool", "store", "--label", f"{SERVICE} GitHub Token",
                   "service", target, "account", account],
                  check=False, stdin=secret)
    if result.returncode != 0:
        raise CredentialError(f"secret-tool 写入失败：{result.stderr.strip()}")


def _linux_read(target: str) -> Optional[str]:
    result = _run(["secret-tool", "lookup", "service", target, "account", ACCOUNT],
                  check=False)
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n") or None


def _linux_delete(target: str) -> None:
    _run(["secret-tool", "clear", "service", target, "account", ACCOUNT], check=False)


def _run(args: list[str], *, check: bool, stdin: str | None = None):
    try:
        return subprocess.run(
            args, capture_output=True, text=True, input=stdin,
            timeout=20, check=False,
        )
    except FileNotFoundError as exc:
        if check:
            raise CredentialError(f"找不到命令 {args[0]}") from exc
        raise CredentialError(f"找不到命令 {args[0]}，无法访问系统密钥链") from exc
    except subprocess.SubprocessError as exc:
        raise CredentialError(f"调用 {args[0]} 失败：{exc}") from exc


# --------------------------------------------------------------------------
# 后端选择
# --------------------------------------------------------------------------

def backend_name() -> str:
    """给界面显示用的后端名称。"""
    if sys.platform == "win32":
        return "Windows 凭据管理器"
    if sys.platform == "darwin":
        return "macOS 钥匙串"
    if shutil.which("secret-tool"):
        return "libsecret (GNOME Keyring)"
    return "不可用"


def is_available() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return sys.platform.startswith("linux") and shutil.which("secret-tool") is not None


def _backend():
    if sys.platform == "win32":
        return _win_write, _win_read, _win_delete
    if sys.platform == "darwin":
        return _mac_write, _mac_read, _mac_delete
    if shutil.which("secret-tool"):
        return _linux_write, _linux_read, _linux_delete
    return None


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def _target(target: str | None = None) -> str:
    return target or os.environ.get("ARCHIVEMATE_CRED_TARGET") or f"{SERVICE}/{ACCOUNT}"


def get_token(target: str | None = None) -> Optional[str]:
    """读取 Token；读不到返回 None。密钥链不可用时看内存兜底。"""
    key = _target(target)
    backend = _backend()
    if backend is None:
        return _MEMORY_FALLBACK.get(key)
    try:
        return backend[1](key)
    except CredentialError:
        return _MEMORY_FALLBACK.get(key)


def set_token(token: str, target: str | None = None, account: str = ACCOUNT) -> None:
    """写入系统密钥链；失败则抛 CredentialError，由调用方降级为内存保存。"""
    key = _target(target)
    if not token:
        raise CredentialError("Token 不能为空")
    backend = _backend()
    if backend is None:
        raise CredentialError(_UNSUPPORTED_MESSAGE)
    backend[0](key, token, account)
    _MEMORY_FALLBACK.pop(key, None)


def delete_token(target: str | None = None) -> None:
    key = _target(target)
    _MEMORY_FALLBACK.pop(key, None)
    backend = _backend()
    if backend is None:
        return
    backend[2](key)


def remember_for_session(token: str, target: str | None = None) -> None:
    """密钥链失败时的降级：只在内存里留着，本次运行有效。"""
    _MEMORY_FALLBACK[_target(target)] = token


def has_session_token(target: str | None = None) -> bool:
    return _target(target) in _MEMORY_FALLBACK


def storage_hint() -> str:
    """给设置界面的一句提示。"""
    if is_available():
        return f"Token 保存在{backend_name()}，不会写入配置文件。"
    return _UNSUPPORTED_MESSAGE


def _basic_header(token: str) -> str:
    """GitHub 的 HTTP Basic 头：用户名 x-access-token，密码是 Token。"""
    raw = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {raw}"
