"""基于 git 命令行的后端。

设计要点：
* 本地缓存仓库是 **bare** 的，只用底层管道（hash-object / mktree /
  commit-tree / update-ref / push），不 checkout 工作区。
  这样推送成功的文件不会在本地再留一份副本，归档后空间是真实释放的。
* Token 通过环境变量 ``GIT_CONFIG_VALUE_*`` 传给 git，不出现在命令行里，
  也因此不会被写进任何 git 配置文件；每次调用都临时设置，用完即弃。
* 关闭所有凭据助手与交互式提示，避免卡死或把 Token 落盘。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .errors import GitError, PushRejected
from .tls_certs import ensure_ca_bundle

Logger = Callable[[str], None]

_CREATE_NO_WINDOW = 0x08000000

_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "failed to push some refs",
    "fetch first",
    "behind",
)

# 连接建立之后数据停滞（握手卡住、被中间设备黑洞掉）时，
# 必须由 git 自己放弃，否则它会一直等下去。
_STALL_LIMIT_BYTES_PER_SECOND = 1000
_DEFAULT_STALL_SECONDS = 60
_DEFAULT_NETWORK_TIMEOUT = 1800


def _noop(_message: str) -> None:
    pass


def kill_process_tree(process: subprocess.Popen) -> None:
    """杀掉整棵进程树。

    git 会派生子进程（例如 ``git-remote-https``）。只杀直接子进程是不够的：
    孙进程仍然持有输出管道的写端，``communicate()`` 就永远等不到 EOF ——
    于是「超时」反而变成永久卡死。必须把子孙一起杀掉。
    """
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            killer = subprocess.Popen(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                # 输出丢给 DEVNULL：这里绝不能用管道，
                # 否则清理动作自己也可能挂住。
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            killer = None
        if killer is not None:
            try:
                killer.wait(timeout=15)
            except subprocess.TimeoutExpired:
                killer.kill()
    except Exception:  # noqa: BLE001 —— 清理失败也不能盖住原始错误
        pass
    try:
        process.kill()
    except Exception:  # noqa: BLE001
        pass


def _read_back(stream) -> bytes:
    """把临时文件里的内容读出来；任何情况下都不阻塞。"""
    if stream is None:
        return b""
    try:
        stream.flush()
        stream.seek(0)
        return stream.read() or b""
    except Exception:  # noqa: BLE001
        return b""


def _close_quietly(stream) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except Exception:  # noqa: BLE001
        pass


def run_command(
    command: Sequence[str],
    *,
    env: dict,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
    on_process: Callable[[subprocess.Popen | None], None] | None = None,
) -> subprocess.CompletedProcess:
    """执行外部命令并采集输出；超时**保证**返回，不会挂住。

    为什么不用 ``subprocess.run`` / ``communicate``：
    它们都靠「管道读到 EOF」来判定子进程结束。git 会派生 ``git-remote-https``
    之类的孙进程并让它继承管道的写端；即使杀掉了直接子进程，只要孙进程还在，
    EOF 就永远不来，调用线程会无限阻塞（``close()`` 也会因为读线程占着锁而死锁）。
    连接被中间设备黑洞掉时，这正是「点同步就永久卡死」的成因。

    所以这里改成：子进程的 stdout/stderr/stdin 全部用**临时文件**，
    等待只用 ``Popen.wait(timeout=…)``（只依赖进程句柄，不依赖 EOF），
    超时则杀掉整棵进程树后读走已有输出。任何路径都会在有限时间内返回。
    """
    creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0
    stdout_file = tempfile.TemporaryFile()
    stderr_file = tempfile.TemporaryFile()

    stdin_arg: object = subprocess.DEVNULL
    stdin_file = None
    if input_bytes is not None:
        stdin_file = tempfile.TemporaryFile()
        stdin_file.write(input_bytes)
        stdin_file.flush()
        stdin_file.seek(0)
        stdin_arg = stdin_file

    process = subprocess.Popen(
        list(command),
        # 没有输入时用 DEVNULL：打包后的图形程序没有控制台，
        # 继承来的 stdin 句柄是无效的，绝不能让它去读。
        stdin=stdin_arg,
        stdout=stdout_file,
        stderr=stderr_file,
        env=env,
        creationflags=creationflags,
        start_new_session=(os.name != "nt"),
    )
    if on_process is not None:
        on_process(process)

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(process)
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # 进程树也没能杀掉（权限不足等）：不再等，直接读走已有输出返回。
            returncode = None
    finally:
        if on_process is not None:
            on_process(None)

    stdout = _read_back(stdout_file)
    stderr = _read_back(stderr_file)
    _close_quietly(stdout_file)
    _close_quietly(stderr_file)
    _close_quietly(stdin_file)

    if timed_out:
        raise subprocess.TimeoutExpired(list(command), timeout or 0.0,
                                        output=stdout, stderr=stderr)

    return subprocess.CompletedProcess(list(command),
                                       returncode if returncode is not None else -1,
                                       stdout, stderr)



def find_git(executable: str = "") -> str:
    """定位 git 可执行文件；找不到返回空串。"""
    if executable.strip():
        candidate = executable.strip()
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        return found or ""
    found = shutil.which("git")
    if found:
        return found
    common = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe",
        Path("/usr/bin/git"),
        Path("/usr/local/bin/git"),
        Path("/opt/homebrew/bin/git"),
    ]
    for path in common:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return ""


def git_version(git_exe: str) -> str:
    try:
        result = subprocess.run([git_exe, "--version"], capture_output=True,
                                text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


@dataclass
class FileToCommit:
    """一个待提交文件：本地绝对路径 + 仓库内相对路径。"""

    source: Path
    dest_rel: str


def _classify_transport_error(stderr: str) -> str:
    """把 git 的 stderr 归类，供自动修复与提示复用。"""
    lowered = (stderr or "").lower()
    if ("acquirecredentialshandle" in lowered
            or "sec_e_no_credentials" in lowered
            or "schannel" in lowered):
        return "schannel"
    if ("ssl certificate" in lowered
            or "certificate verify" in lowered
            or "local issuer certificate" in lowered
            or "server certificate verification failed" in lowered
            or "ssl_error" in lowered):
        return "cert"
    if "could not resolve host" in lowered:
        return "dns"
    if "too slow" in lowered or "bytes/sec" in lowered:
        return "slow"
    if "timed out" in lowered or "operation timed out" in lowered:
        return "timeout"
    if ("authentication failed" in lowered or " 401" in lowered or " 403" in lowered):
        return "auth"
    if "repository not found" in lowered or "not found" in lowered:
        return "repo"
    return "other"


class GitBackend:
    def __init__(
        self,
        repo_dir: Path,
        remote_url: str,
        branch: str = "main",
        *,
        git_exe: str = "git",
        proxy: str = "",
        ssl_backend: str = "",
        ssl_ca_info: str = "",
        author_name: str = "ArchiveMate",
        author_email: str = "archivemate@localhost",
        token: str | None = None,
        stall_seconds: int = _DEFAULT_STALL_SECONDS,
        network_timeout: int = _DEFAULT_NETWORK_TIMEOUT,
        logger: Logger | None = None,
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.remote_url = remote_url
        self.branch = (branch or "main").strip() or "main"
        self.git_exe = git_exe or "git"
        self.proxy = (proxy or "").strip()
        backend = (ssl_backend or "").strip().lower()
        self.ssl_backend = backend if backend in ("schannel", "openssl") else ""
        self.ssl_ca_info = (ssl_ca_info or "").strip()
        self._tls_healed = False
        # 自愈成功后回调一次，好让上层（SyncEngine）在本进程内记住这个结果，
        # 下次同步就不用再撞一次同样的墙。只存内存，不写配置文件。
        self.on_tls_healed: "Callable[[GitBackend], None] | None" = None
        self.author_name = author_name
        self.author_email = author_email
        self.token = token
        # 网络操作（ls-remote / fetch / push）的上限。git 默认没有任何
        # 「连接已建立但数据不动」的超时，必须自己兜住。
        self.stall_seconds = max(0, int(stall_seconds or 0))
        self.network_timeout = max(30, int(network_timeout or _DEFAULT_NETWORK_TIMEOUT))
        self._log: Logger = logger or _noop
        self._base_commit: str | None = None
        self._active_process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()
        self._cancelled = False

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def _track_process(self, process: subprocess.Popen | None) -> None:
        with self._process_lock:
            self._active_process = process

    def cancel(self) -> bool:
        """中断当前正在跑的 git 命令（如果有）。返回是否真的打断了什么。"""
        self._cancelled = True
        with self._process_lock:
            process = self._active_process
        if process is None:
            return False
        kill_process_tree(process)
        return True

    def reset_cancel(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise GitError("同步已取消。")

    # ------------------------------------------------------------------
    # 进程调用
    # ------------------------------------------------------------------

    def _secret_env(self) -> dict[str, str]:
        """把敏感配置通过环境变量交给 git，避免出现在命令行里。"""
        entries: list[tuple[str, str]] = []
        if self.token:
            import base64

            raw = base64.b64encode(
                f"x-access-token:{self.token}".encode("utf-8")).decode("ascii")
            entries.append(("http.extraheader", f"Authorization: Basic {raw}"))
        if not entries:
            return {}

        env: dict[str, str] = {}
        count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or "0")
        for key, value in entries:
            env[f"GIT_CONFIG_KEY_{count}"] = key
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
        return env

    def _base_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._secret_env())
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "Never"
        env["GIT_ASKPASS"] = ""
        env["SSH_ASKPASS"] = ""
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
        # bare 仓库没有工作区、通常也没有本地 user.name/user.email，
        # 提交身份只能靠环境变量给，否则 commit-tree 会直接报错。
        env["GIT_AUTHOR_NAME"] = self.author_name
        env["GIT_AUTHOR_EMAIL"] = self.author_email
        env["GIT_COMMITTER_NAME"] = self.author_name
        env["GIT_COMMITTER_EMAIL"] = self.author_email
        if self.proxy:
            # 由 git 自己处理代理，兼容 http/https
            env["HTTPS_PROXY"] = self.proxy
            env["HTTP_PROXY"] = self.proxy
        return env

    def _run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: float | None = 1800,
        use_git_dir: bool = True,
    ) -> subprocess.CompletedProcess:
        command = [self.git_exe]
        if use_git_dir:
            command += [f"--git-dir={self.repo_dir}"]
            command += [
                "-c", "credential.helper=",
                "-c", "core.askPass=",
                "-c", "advice.detachedHead=false",
                "-c", "core.quotePath=false",
            ]
            if self.proxy:
                command += ["-c", f"http.proxy={self.proxy}"]
            if self.ssl_backend:
                command += ["-c", f"http.sslBackend={self.ssl_backend}"]
            if self.ssl_ca_info:
                command += ["-c", f"http.sslCAInfo={self.ssl_ca_info}"]
            if self.stall_seconds:
                # 连接建立后长时间没有数据就自己放弃，而不是一直等。
                command += ["-c", f"http.lowSpeedLimit={_STALL_LIMIT_BYTES_PER_SECOND}",
                            "-c", f"http.lowSpeedTime={self.stall_seconds}"]
        command += list(args)

        self._check_cancelled()
        # 不用 text=True：Windows 上 TextIOWrapper 会把写入 stdin 的 "\n"
        # 翻译成 "\r\n"，而 mktree 是按行解析的，路径里会混进 "\r"。
        payload = input_text.encode("utf-8") if input_text is not None else None
        started = time.monotonic()
        try:
            result = run_command(
                command,
                env=self._base_env(),
                input_bytes=payload,
                timeout=timeout,
                on_process=self._track_process,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            raise GitError(
                f"git 命令超时（等待 {elapsed:.0f} 秒无响应）：{' '.join(args[:2])}。"
                "如果是网络问题，可以在「设置 → 高级」里填写代理地址。",
                command=command,
            ) from exc
        except OSError as exc:
            raise GitError(f"无法执行 git：{exc}", command=command) from exc

        result.stdout = (result.stdout or b"").decode("utf-8", "replace")
        result.stderr = (result.stderr or b"").decode("utf-8", "replace")

        # 证书类失败不分 check 与否都要试着自愈一次：否则 check=False 的
        # 调用（remote_branch_exists 等）会绕过修复，用户只看到证书报错。
        if result.returncode != 0 and self._heal_tls(result.stderr or ""):
            self._log(
                "HTTPS 连接被证书问题挡住，已自动改用 OpenSSL + 系统根证书重试一次。")
            return self._run(args, check=check, input_text=input_text,
                             timeout=timeout, use_git_dir=use_git_dir)

        if check and result.returncode != 0:
            raise GitError(
                self._friendly_error(result),
                command=command,
                returncode=result.returncode,
                stderr=result.stderr or "",
            )
        return result

    def _heal_tls(self, stderr: str) -> bool:
        """证书类失败时，自动切到「OpenSSL + 系统根证书」再试一次。

        这一步能救回来的情况很具体：Windows 的 schannel 拿不到凭据
        （`SEC_E_NO_CREDENTIALS`），或者 OpenSSL 不认 Git 自带的 CA 包
        （`unable to get local issuer certificate`）。两者都只需要把
        系统真正信任的根证书导出来交给 OpenSSL。
        """
        if self._tls_healed:
            return False
        if _classify_transport_error(stderr) not in ("schannel", "cert"):
            return False
        bundle = self.ssl_ca_info
        if bundle and not Path(bundle).is_file():
            # 配置里记的旧 CA 包被删了（换过机器、清过 cache），重新导出。
            bundle = ""
        if bundle:
            found = bundle
        else:
            exported = ensure_ca_bundle(self.repo_dir.parent)
            if exported is None:
                # 导不出证书（非 Windows，或系统库不给），只能把原错误抛回去。
                # 记下「已经试过」，免得每次失败都重新导一遍证书。
                self._tls_healed = True
                return False
            found = str(exported)
        # 确定拿到了 CA 包再改状态：否则报错文案会和实际后端对不上。
        self._tls_healed = True
        self.ssl_backend = "openssl"
        self.ssl_ca_info = found
        if self.on_tls_healed is not None:
            try:
                self.on_tls_healed(self)
            except Exception:       # 回调只是锦上添花，不能拖垮同步
                pass
        return True

    def _friendly_error(self, result: subprocess.CompletedProcess) -> str:
        stderr = result.stderr or ""
        if isinstance(stderr, bytes):     # 兜底：格式化函数自己绝不能抛异常
            stderr = stderr.decode("utf-8", "replace")
        stderr = stderr.strip()
        lowered = stderr.lower()
        detail = stderr.splitlines()[-1] if stderr else ""

        # 注意顺序：git 把所有传输层失败都套上 "unable to access"，
        # 所以必须先把证书/凭据这类具体原因挑出来，否则会被网络提示盖掉，
        # 用户会照着「检查网络」白折腾。
        if ("acquirecredentialshandle" in lowered
                or "sec_e_no_credentials" in lowered
                or "schannel" in lowered):
            hint = (
                "Windows 的证书组件（schannel）拿不到系统凭据，"
                "所以无法建立 HTTPS 连接。\n"
                "请在「设置 → 高级」把 HTTPS 证书后端改成 OpenSSL 再试。"
            )
            return f"{hint}\n{detail}" if detail else hint
        if ("ssl certificate" in lowered
                or "certificate verify" in lowered
                or "local issuer certificate" in lowered
                or "server certificate verification failed" in lowered
                or "ssl_error" in lowered):
            hint = (
                "HTTPS 证书校验失败，git 无法建立安全连接。\n"
                "可以试试：在「设置 → 高级」把 HTTPS 证书后端在「系统」和「OpenSSL」之间换一个；"
                "如果是公司/学校网络做过证书拦截，需要先把它的根证书装进系统。"
            )
            return f"{hint}\n{detail}" if detail else hint
        if "could not resolve host" in lowered:
            hint = "无法解析 github.com，请检查网络连接或 DNS 设置。"
            return f"{hint}\n{detail}" if detail else hint
        if "too slow" in lowered or "bytes/sec" in lowered:
            hint = (
                "网络太慢或连接被中断，git 已经主动放弃这次传输"
                "（ArchivMate 也加了自己的等待上限，不会再无限卡住）。\n"
                "如果经常这样：换个网络，或在「设置 → 高级」里填写代理地址；"
                "也可以把「高级」里的等待上限调大一点再重试。"
            )
            return f"{hint}\n{detail}" if detail else hint
        if "timed out" in lowered or "operation timed out" in lowered:
            hint = "连接 GitHub 超时，请检查网络，或在「设置 → 高级」里填写代理地址。"
            return f"{hint}\n{detail}" if detail else hint
        if "authentication failed" in lowered or "403" in lowered or "401" in lowered:
            return "GitHub 认证失败，请检查 Token 是否有效、是否具备 repo 权限。"
        if "repository not found" in lowered:
            return "找不到仓库，请确认仓库名与 Token 权限（需要 repo 范围）。"
        if "unable to access" in lowered:
            hint = "无法连接 GitHub，请检查网络，或在「设置 → 高级」里填写代理地址。"
            return f"{hint}\n{detail}" if detail else hint
        tail = detail or f"退出码 {result.returncode}"
        return f"git 执行失败：{tail}"

    # ------------------------------------------------------------------
    # 仓库准备
    # ------------------------------------------------------------------

    def _init_bare(self, extra: Sequence[str]) -> subprocess.CompletedProcess:
        return run_command(
            [self.git_exe, "init", "--bare", *extra, str(self.repo_dir)],
            env=self._base_env(), timeout=60, on_process=self._track_process,
        )

    def ensure_repo(self) -> None:
        """本地缓存仓库不存在就建一个 bare 仓库；远端地址变了就更新。"""
        if not (self.repo_dir / "HEAD").exists():
            self.repo_dir.parent.mkdir(parents=True, exist_ok=True)
            result = self._init_bare(["--initial-branch", self.branch])
            if result.returncode != 0:
                # 老版本 git 不认 --initial-branch
                result = self._init_bare([])
            if result.returncode != 0:
                stderr = (result.stderr or b"").decode("utf-8", "replace")
                raise GitError(f"初始化本地缓存仓库失败：{stderr.strip()}")
            self._log(f"已创建本地缓存仓库：{self.repo_dir}")

        current = self._run(["remote", "get-url", "origin"], check=False)
        if current.returncode != 0:
            self._run(["remote", "add", "origin", self.remote_url])
        elif current.stdout.strip() != self.remote_url:
            self._run(["remote", "set-url", "origin", self.remote_url])

    def remote_branch_exists(self) -> bool:
        result = self._run(
            ["ls-remote", "--exit-code", "--heads", "origin",
             f"refs/heads/{self.branch}"],
            check=False,
            timeout=self.network_timeout,
        )
        if result.returncode == 0:
            return bool(result.stdout.strip())
        if result.returncode == 2:
            return False
        if self._cancelled:
            raise GitError("同步已取消。")
        raise GitError(self._friendly_error(result), command=[], returncode=result.returncode,
                       stderr=result.stderr or "")

    def fetch(self) -> None:
        """把远端分支抓到 refs/remotes/origin/<branch>，并刷新 _base_commit。"""
        self.ensure_repo()
        self._base_commit = None
        if not self.remote_branch_exists():
            self._log("远端还没有这个分支，将创建首个提交。")
            return
        self._run([
            "fetch", "--prune", "origin",
            f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}",
        ], timeout=self.network_timeout)
        result = self._run(
            ["rev-parse", "--verify", "--quiet",
             f"refs/remotes/origin/{self.branch}"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            self._base_commit = result.stdout.strip()
            self._run(["update-ref", f"refs/heads/{self.branch}", self._base_commit])
            self._log(f"已同步远端 {self.branch} @ {self._base_commit[:8]}")

    @property
    def base_commit(self) -> str | None:
        return self._base_commit

    # ------------------------------------------------------------------
    # 树操作
    # ------------------------------------------------------------------

    def existing_paths(self) -> set[str]:
        """基线提交里已有的文件路径（用于避开重名覆盖）。"""
        if not self._base_commit:
            return set()
        # -z：路径不做 C 风格转义，NUL 分隔，中文/空格/引号都安全
        result = self._run(
            ["ls-tree", "-r", "-z", "--name-only", self._base_commit], check=False)
        if result.returncode != 0:
            return set()
        return {entry for entry in result.stdout.split("\0") if entry}

    def _load_tree(self) -> dict:
        """把基线提交的树载入成嵌套 dict：{'__entries__': {name: node}}。"""
        root: dict = {"__entries__": {}}
        if not self._base_commit:
            return root
        result = self._run(["ls-tree", "-r", "-t", "-z", self._base_commit])
        for entry in result.stdout.split("\0"):
            if not entry or "\t" not in entry:
                continue
            meta, path = entry.split("\t", 1)
            parts = meta.split()
            if len(parts) < 3:
                continue
            mode, kind, sha = parts[0], parts[1], parts[2]
            segments = path.split("/")
            node = root
            for segment in segments[:-1]:
                node = node["__entries__"].setdefault(
                    segment, {"kind": "tree", "__entries__": {}})
            leaf = segments[-1]
            node["__entries__"][leaf] = {
                "kind": "blob" if kind == "blob" else "tree",
                "mode": mode,
                "sha": sha,
                **({"__entries__": {}} if kind != "blob" else {}),
            }
        return root

    def _hash_object(self, path: Path, dest_rel: str) -> str:
        result = self._run(
            ["hash-object", "-w", f"--path={dest_rel}", "--", str(path)])
        sha = result.stdout.strip()
        if not sha:
            raise GitError(f"写入 blob 失败：{path.name}")
        return sha

    def _write_tree(self, node: dict) -> str:
        lines: list[str] = []
        for name, child in sorted(node["__entries__"].items()):
            if child.get("kind") == "tree":
                sha = self._write_tree(child)
                lines.append(f"040000 tree {sha}\t{name}")
            else:
                lines.append(f"{child.get('mode', '100644')} blob {child['sha']}\t{name}")
        if not lines:
            # 空 tree 对象
            return self._run(["mktree", "-z"], input_text="").stdout.strip()
        # -z：NUL 分隔，文件名里的制表符/换行/引号都不会破坏格式
        return self._run(["mktree", "-z"],
                         input_text="\0".join(lines) + "\0").stdout.strip()

    # ------------------------------------------------------------------
    # 提交与推送
    # ------------------------------------------------------------------

    def build_commit(self, files: Iterable[FileToCommit], message: str) -> str | None:
        """把文件写进对象库并生成一个提交；没有实际变化时返回 None。"""
        items = [item for item in files if item.source.is_file()]
        if not items:
            return None

        tree = self._load_tree()
        added = 0
        for item in items:
            segments = item.dest_rel.replace("\\", "/").split("/")
            node = tree
            for segment in segments[:-1]:
                child = node["__entries__"].get(segment)
                if child is None or child.get("kind") != "tree":
                    child = {"kind": "tree", "__entries__": {}}
                    node["__entries__"][segment] = child
                node = child
            sha = self._hash_object(item.source, item.dest_rel)
            leaf = segments[-1]
            previous = node["__entries__"].get(leaf)
            node["__entries__"][leaf] = {"kind": "blob", "mode": "100644", "sha": sha}
            added += 1
            if previous and previous.get("sha") != sha:
                self._log(f"同名文件已更新：{item.dest_rel}")

        tree_sha = self._write_tree(tree)

        if self._base_commit:
            base_tree = self._run(
                ["rev-parse", f"{self._base_commit}^{{tree}}"]).stdout.strip()
            if base_tree == tree_sha:
                self._log("没有新内容需要提交。")
                return None

        args = ["commit-tree", tree_sha]
        if self._base_commit:
            args += ["-p", self._base_commit]
        args += ["-m", message]
        commit = self._run(args).stdout.strip()
        if not commit:
            raise GitError("生成提交失败")
        self._run(["update-ref", f"refs/heads/{self.branch}", commit])
        self._log(f"已生成提交 {commit[:8]}：{added} 个文件")
        return commit

    def push(self) -> None:
        """推送当前分支；被拒绝时抛 PushRejected 交由上层重试。"""
        result = self._run(
            ["push", "--porcelain", "origin",
             f"refs/heads/{self.branch}:refs/heads/{self.branch}"],
            check=False,
            timeout=self.network_timeout,
        )
        if result.returncode == 0:
            self._log(f"已推送到 origin/{self.branch}")
            return
        if self._cancelled:
            raise GitError("同步已取消。")
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if any(marker in combined for marker in _NON_FAST_FORWARD_MARKERS):
            raise PushRejected("远端有新的提交，需要先同步再推送。")
        raise GitError(self._friendly_error(result), command=[], returncode=result.returncode,
                       stderr=result.stderr or "")

    def push_with_retry(
        self,
        files: Iterable[FileToCommit],
        message: str,
        *,
        attempts: int = 3,
        delay: float = 3.0,
    ) -> str:
        """fetch → commit → push，失败按配置重试。返回成功推送的提交号。"""
        items = [item for item in files if item.source.is_file()]
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                self.fetch()
                commit = self.build_commit(items, message)
                if commit is None:
                    return ""
                self.push()
                return commit
            except PushRejected as exc:
                last_error = exc
                self._log(f"第 {attempt} 次推送被拒绝（远端有新提交），正在重新同步……")
            except (GitError,) as exc:
                last_error = exc
                self._log(f"第 {attempt} 次推送失败：{exc}")
            if attempt < attempts:
                time.sleep(delay * attempt)
        if last_error is None:
            raise GitError("推送失败")
        raise last_error

    def verify_remote_has(self, dest_rel: str) -> bool:
        """推送后回查：远端确实包含这个文件才算成功。"""
        result = self._run(["cat-file", "-e", f"refs/heads/{self.branch}:{dest_rel}"],
                           check=False)
        return result.returncode == 0
