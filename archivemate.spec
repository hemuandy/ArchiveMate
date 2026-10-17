# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 ArchiveMate 打成免安装的绿色软件。

产出 dist/ArchiveMate/：
    ArchiveMate.exe          图形界面（双击就用）
    ArchiveMate-命令行.exe    命令行排障（doctor / status / sync）
    _internal/               运行库，不用管

为什么用 onedir 而不是 onefile：
  * 启动快很多（onefile 每次运行都要把上百 MB 解压到临时目录）；
  * 便携模式要往 exe 同级目录写 config.json / state.json / logs，
    onefile 的 exe 同级目录常常是不可写的下载目录，不合适。

用法见 build.ps1，或直接：
    pyinstaller --noconfirm --clean archivemate.spec
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve()   # SPECPATH 就是本文件所在目录
sys.path.insert(0, str(ROOT))

ICON = ROOT / "assets" / "archivemate.ico"

# 这些包装了但用不到，显式排除可以少几十 MB
EXCLUDES = [
    "tkinter", "unittest", "pydoc_data", "test", "lib2to3",
    "numpy", "cv2", "PIL", "matplotlib", "scipy", "pandas",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtPdf", "PySide6.QtPositioning", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio", "PySide6.QtSql", "PySide6.QtStateMachine",
    "PySide6.QtSvgWidgets", "PySide6.QtTest", "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtNetworkAuth",
    "PySide6.QtVirtualKeyboard", "PySide6.QtConcurrent",
    # 项目自己不用，只是打包时顺手带上会变大
    "setuptools", "pip", "wheel",
]

# ---------------------------------------------------------------- 图形界面版
gui_analysis = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # 这些是延迟导入的，PyInstaller 的静态分析看不到
        "archivemate.gui.main_window",
        "archivemate.gui.dialogs",
        "archivemate.gui.widgets",
        "archivemate.gui.theme",
        "archivemate.cli",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

gui_pyz = PYZ(gui_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="ArchiveMate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 图形程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

# ---------------------------------------------------------------- 命令行版
# 图形的 exe 没有控制台，print 出来的东西会被直接丢掉，所以排障用的命令行
# 必须单独出一个带控制台的 exe，放在同一个文件夹里共用同一份 config.json。
cli_analysis = Analysis(
    [str(ROOT / "packaging" / "cli_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["archivemate.cli"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES + ["PySide6"],   # 命令行版完全不需要 Qt
    noarchive=False,
    optimize=0,
)

cli_pyz = PYZ(cli_analysis.pure)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="ArchiveMate-命令行",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

# ---------------------------------------------------------------- 合并输出
coll = COLLECT(
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ArchiveMate",
)

# macOS 上再包一层 .app。Windows 走到这里就结束了。
# 注意：这一段没有在真机上验证过（开发机是 Windows）。
if sys.platform == "darwin":
    ICNS = ROOT / "assets" / "archivemate.icns"
    app = BUNDLE(
        coll,
        name="ArchiveMate.app",
        icon=str(ICNS) if ICNS.exists() else None,
        bundle_identifier="com.archivemate.app",
        info_plist={
            "CFBundleName": "ArchiveMate",
            "CFBundleDisplayName": "ArchiveMate 归巢",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.15",
        },
    )
