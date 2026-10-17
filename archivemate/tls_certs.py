"""从系统证书库导出根证书，喂给 git 的 OpenSSL 后端。

为什么需要这个：Git for Windows 自带的 CA 包不一定覆盖本机的实际信任链，
而 Windows 的 schannel 后端在部分机器上会直接拿不到凭据
（`SEC_E_NO_CREDENTIALS`）。两条路都堵死时，把**系统当前信任的根证书**
导出成一个 PEM，再用 `-c http.sslCAInfo=<pem>` 交给 OpenSSL 后端，
是最省事、也最贴近「系统本来就信任它」的解法。
"""

from __future__ import annotations

import base64
import ssl
from pathlib import Path

BUNDLE_NAME = "ca-bundle.pem"
_STALE_SECONDS = 7 * 24 * 3600


def _dump_pem(certs: list[bytes]) -> str:
    chunks = []
    for der in certs:
        body = base64.encodebytes(der).decode("ascii").strip()
        chunks.append(f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n")
    return "".join(chunks)


def export_system_roots(dest: Path) -> Path | None:
    """把系统信任的根证书写成 PEM。拿不到就返回 None（绝不抛异常）。"""
    try:
        context = ssl.create_default_context()
        certs = context.get_ca_certs(binary_form=True)
    except Exception:
        return None
    if not certs:
        return None
    pem = _dump_pem(certs)
    if not pem.strip():
        return None
    try:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(pem, encoding="ascii")
    except OSError:
        return None
    return dest


def ensure_ca_bundle(directory: Path) -> Path | None:
    """在 `directory` 里准备一份 CA 包；已经存在且不太旧就直接复用。"""
    import time

    bundle = Path(directory) / BUNDLE_NAME
    try:
        if bundle.is_file() and bundle.stat().st_size > 0:
            if time.time() - bundle.stat().st_mtime < _STALE_SECONDS:
                return bundle
    except OSError:
        pass
    return export_system_roots(bundle)


def count_certificates(path: Path) -> int:
    """测试与自检用：数一下 PEM 里有几张证书。"""
    try:
        return path.read_text(encoding="ascii", errors="ignore").count(
            "-----BEGIN CERTIFICATE-----")
    except OSError:
        return 0
