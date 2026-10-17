# ArchiveMate 归巢

把学校下发的文件（作文模板、课件、PDF、Word…）**自动分类归档并推送到 GitHub 私有仓库**的桌面软件。
装完就能用，不需要懂 git。

- 释放本地磁盘空间：推送成功后自动把文件挪进「归档」文件夹，而不是删掉
- 云端统一管理、可检索；Git 天然保留版本历史
- 隐私优先：私有仓库 + Token 存系统密钥链，不落明文

面向使用者的操作手册见 **[docs/使用说明.md](docs/使用说明.md)**，本文件面向开发和维护。

![主界面](docs/screenshot.png)

## 快速开始

使用者：解压整个 `ArchiveMate` 文件夹（**不要只拿 exe**，运行库在 `_internal/` 里）→ 双击 `ArchiveMate.exe`
→ 跟着三步引导填 Token、选仓库、选监控文件夹。

开发者：

```powershell
python -m pip install PySide6            # 运行时依赖只有这一个（Python 3.10+）

python -m archivemate                    # 图形界面
python -m archivemate --cli doctor       # 命令行排障

$env:QT_QPA_PLATFORM = 'offscreen'       # 无头跑 GUI 测试
python -m unittest discover -s tests

pwsh -NoProfile -File build.ps1          # 打包免安装成品
```

## 功能

| 能力 | 实现 |
| --- | --- |
| 多个监控文件夹（可增删，默认预填「下载」） | `watcher.py` / `service.py` |
| 拖拽上传，与自动监控走同一条处理流程 | `gui/widgets.py: DropArea` |
| 目标文件类型列表（可增删） | `config.py` / `gui/widgets.py: ListEditor` |
| 按日期 + 类型分类，如 `2026/10/ppt/数学课件.pptx` | `classify.py` |
| 手动同步 + 定时同步（关闭 / 每天 0 点 / 每周一 0 点 / 每月 1 号 0 点） | `schedule.py` / `gui/main_window.py` |
| 推送成功后归档（移动而非删除），「清空归档」需二次确认 | `sync.py: _archive_item` |
| 默认走 GitHub REST 接口建提交，不下载仓库已有文件 | `api_backend.py` |
| 同名文件覆盖，不再堆 `xxx (2).pdf` | `sync.py` |
| 单文件 50 MiB 提示 / 90 MiB 阻止；单次推送 ≤ 2 GiB 自动分批 | `fsutil.py` / `sync.py` |
| 代理、HTTPS 证书后端可切换、证书问题自动自愈 | `git_backend.py` / `tls_certs.py` |
| 网络等待上限（停滞 60s / 硬超时 30min）与「取消同步」 | `git_backend.py: run_command` |
| 便携式配置（存在 exe 旁边，可随软件拷贝） | `app_paths.py` |
| Token 存系统密钥链，失败只降级为「本次手动输入」 | `credentials.py` |
| 首次使用引导（可跳过） | `gui/dialogs.py: OnboardingDialog` |

## 目录结构

```
archivemate/
├─ app_paths.py      便携式路径：配置/状态/日志/缓存放哪
├─ config.py         配置数据类、读写、清洗
├─ schedule.py       定时同步的「下一次 0 点」计算
├─ credentials.py    跨平台密钥链（Windows 走 ctypes 调 advapi32）
├─ classify.py       日期 + 类型 → 仓库内相对路径
├─ fsutil.py         体积阈值、稳定性判定、安全移动
├─ api_backend.py    默认推送后端：GitHub REST，不下载仓库已有文件
├─ git_backend.py    bare 仓库 + 底层管道（hash-object/mktree/commit-tree/push）
├─ github_api.py     纯 urllib 的 GitHub REST 客户端（含代理）
├─ service.py        业务门面：监控 + 队列 + 同步 + 事件流（GUI / CLI 共用）
├─ sync.py           同步引擎：校验 → 刷新 → 提交 → 推送 → 归档
├─ tracker.py        队列 / 历史状态持久化；watcher.py 轮询监控；cli.py 命令行
└─ gui/              PySide6 界面（main_window / dialogs / widgets / theme）
tests/               核心逻辑、在线后端（用假 HTTP 层）、离屏界面
packaging/           打包后的两个入口（图形版 / 命令行版）
archivemate.spec     PyInstaller 配置；build.ps1 一键打包
```

## 几个关键决定

- **零第三方运行时依赖**（除 GUI 的 PySide6）。需求原建议 watchdog / GitPython / PyGithub，
  这些都只是 `git` 命令行的薄包装或事件式监控——后者在 Windows 上对 OneDrive、网络盘并不可靠。
  自己写 ~200 行轮询反而更稳，也避开了 PyInstaller 带 C 扩展的坑。
- **本地缓存是 bare 仓库**，不 checkout 工作区：用户点「清空归档」后，磁盘上不该再多一份文件。
- **默认用 GitHub REST 接口建提交**（`blobs → trees(base_tree) → commits → refs`）。
  实测 `git fetch` 一个 49 MB 的仓库在目标网络里 15 分钟都下不完，而 REST 只传**这次新增的文件**，
  读远端状态 2.6 秒。`git` 后端保留给超 45 MiB 的大文件和已配好凭据助手的机器，两者接口一致。
  （顺带记个坑：`git fetch --filter=blob:none` + `git push` 走不通，推送前的连通性检查要求本地对象齐全。）
- **同名文件覆盖**：仓库里同路径就是替换，保持「仓库里永远是最新那份」。
  例外是同一批里两个待推送文件撞名——那必须错开，否则会静默丢一个。
- **Token 绝不出现在命令行**：用 `GIT_CONFIG_VALUE_0=http.extraheader` 注入
  `Authorization: Basic base64(...)`，再配 `GIT_TERMINAL_PROMPT=0` / `GCM_INTERACTIVE=Never`。
- **git 子进程一律不用管道**：`subprocess` 的 timeout 在孙进程握着写端时会永久阻塞，
  所以输出重定向到临时文件、只 `wait(timeout=)`，到点用 `taskkill /F /T` 收掉进程树。
- **界面与后台解耦**：后台线程只往 `queue.Queue` 塞事件，主窗口 200ms 轮询刷新；
  CLI 用同一套 `ArchiveMateService`，两条入口行为一致。

## 测试

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -m unittest discover -s tests      # Ran 170 tests ... OK
```

覆盖重点：分类与体积规则、配置读写与钳位、端到端流水线（`LocalOnlyBackend` 把「远端」指向本地
bare 仓库，blob/tree/commit/归档全部真实执行）、在线后端（假 HTTP 层）、
定时时刻计算（跨月、跨年、周一 0 点边界）、
凭据卫生（Token 不进命令行/配置/日志，源码无硬编码）、密钥链降级、
传输层报错文案、子进程不挂死、离屏界面全流程，以及**界面调用 GitHub 客户端时的参数签名**
（`autospec`，防的是真实用户踩过的 `takes 2 positional arguments but 3 were given`）。

已知缺口：macOS / Linux 的密钥链分支只写了实现没上机验证。

## 打包

```powershell
pwsh -NoProfile -File build.ps1      # 加 -SkipTests / -Clean / -NoSmoke
```

依次：检查环境 → 跑测试 → 生成图标 → `pyinstaller archivemate.spec` → 离屏启动真实产物冒烟自检。

产物在 `dist/ArchiveMate/`：`ArchiveMate.exe`（图形版）、`ArchiveMate-命令行.exe`（带控制台，
跑 `doctor` / `status` / `sync`）、`_internal/`（Qt 与 Python 运行库），
外加 `LICENSE`、`README.md`、`使用说明.md`。用 `onedir` 而非 `onefile`：
启动快，而且便携模式要往 exe 同级目录写配置，`onefile` 的 exe 常躺在不可写的「下载」目录里。

macOS 的 `.app` 的 `BUNDLE` 段已写好（`sys.platform == "darwin"` 时生效），但未在真机验证。

## 许可

采用 **[PolyForm Noncommercial License 1.0.0](LICENSE)**，完整条款见根目录 `LICENSE`。

- ✅ 免费：个人使用（学生归档自己的课件、笔记、作业），可自行修改源码；学校等非营利机构同样适用。
- ❌ 禁止任何形式的商业用途：不能卖、不能预装进付费商品、不能作为商业服务的一部分。
- ❌ 禁止倒卖：不得转售、打包出售或在收费渠道分发。
- ⚠️ 分发时必须保留 `LICENSE` 与 `Required Notice:` 版权声明。

需要商业授权请另行联系作者。软件按「现状」提供，不附带任何担保。

> Required Notice: Copyright 2026 hemuandy (https://github.com/hemuandy)
