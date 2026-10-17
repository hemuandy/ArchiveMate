# 打包 ArchiveMate 成免安装的绿色软件（Windows）。
#
#   pwsh -NoProfile -File build.ps1              # 完整打包 + 冒烟自检
#   pwsh -NoProfile -File build.ps1 -SkipTests   # 只打包，跳过单元测试
#   pwsh -NoProfile -File build.ps1 -Clean       # 先删掉 build/dist 再打包
#
# 产物：dist/ArchiveMate/ArchiveMate.exe，整个文件夹拷走即可用。

[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$Clean,
    [switch]$NoSmoke
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root

function Write-Step([string]$text) {
    Write-Host ''
    Write-Host "==> $text" -ForegroundColor Cyan
}

# 1. 环境检查 ---------------------------------------------------------------
Write-Step '检查打包环境'
$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) { throw '找不到 python，请先安装 Python 3.10 以上版本并加入 PATH。' }
Write-Host "python : $($python.Source)"
Write-Host "版本   : $(& python -c 'import sys; print(sys.version.split()[0])')"

& python -c 'import PyInstaller' 2>$null
if ($LASTEXITCODE -ne 0) {
    throw '缺少 PyInstaller，请先执行：python -m pip install pyinstaller'
}
Write-Host "打包器 : PyInstaller $(& python -c 'import PyInstaller; print(PyInstaller.__version__)')"

& python -c 'import PySide6' 2>$null
if ($LASTEXITCODE -ne 0) { throw '缺少 PySide6，请先执行：python -m pip install PySide6' }

# 2. 单元测试 ---------------------------------------------------------------
if (-not $SkipTests) {
    Write-Step '跑单元测试'
    $env:QT_QPA_PLATFORM = 'offscreen'
    & python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw '单元测试没通过，先修好再打包。' }
}

# 3. 图标 -------------------------------------------------------------------
Write-Step '生成应用图标'
& python tools/make_icon.py
if ($LASTEXITCODE -ne 0) { throw '图标生成失败。' }

# 4. 清理 -------------------------------------------------------------------
if ($Clean) {
    Write-Step '清理旧的构建产物'
    foreach ($dir in 'build', 'dist') {
        if (Test-Path -LiteralPath $dir) {
            Remove-Item -LiteralPath $dir -Recurse -Force
            Write-Host "已删除 $dir"
        }
    }
}

# 5. 打包 -------------------------------------------------------------------
Write-Step '开始打包（第一次会比较慢，Qt 的运行库有上百 MB）'
& python -m PyInstaller --noconfirm --clean archivemate.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 打包失败。' }

$exe = Join-Path $root 'dist\ArchiveMate\ArchiveMate.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "没有找到产物：$exe" }

$sizeMb = [math]::Round(
    (Get-ChildItem -LiteralPath (Split-Path $exe) -Recurse -File |
        Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "产物   : $exe"
Write-Host "体积   : $sizeMb MB"

# 6. 冒烟自检 ---------------------------------------------------------------
if (-not $NoSmoke) {
    $smokeHome = Join-Path $root 'build\smoketest'
    $env:QT_QPA_PLATFORM = 'offscreen'
    $env:ARCHIVEMATE_GUI_SELFTEST = '1'
    $env:ARCHIVEMATE_HOME = $smokeHome
    try {
        Write-Step '冒烟自检 1/2：离屏启动一次图形版'
        # 图形版是 windows 子系统程序：PowerShell 的 & 调用不会等它，$LASTEXITCODE 是空的；
        # 套一层 cmd /c 才能拿到真正的退出码。
        $output = & cmd /c "`"$exe`"" 2>&1
        $code = $LASTEXITCODE
        $output | ForEach-Object { Write-Host "  $_" }
        if ($code -ne 0) { throw "图形版自检失败，退出码 $code。" }
        Write-Host '  退出码 0，窗口构建正常。' -ForegroundColor Green

        Write-Step '冒烟自检 2/2：命令行版自检'
        $cli = Join-Path $root 'dist\ArchiveMate\ArchiveMate-命令行.exe'
        if (-not (Test-Path -LiteralPath $cli)) { throw "没有找到命令行版：$cli" }
        Remove-Item Env:\ARCHIVEMATE_GUI_SELFTEST -ErrorAction SilentlyContinue
        $output = & $cli doctor 2>&1
        $output | ForEach-Object { Write-Host "  $_" }
        # doctor 在「仓库未配置」时故意返回 1，所以这里只要求它把环境信息打出来
        if (-not ($output -match 'credential_backend')) {
            throw '命令行版没有输出预期的自检信息。'
        }
        Write-Host '  命令行版正常。' -ForegroundColor Green
    } finally {
        Remove-Item Env:\ARCHIVEMATE_GUI_SELFTEST -ErrorAction SilentlyContinue
        Remove-Item Env:\ARCHIVEMATE_HOME -ErrorAction SilentlyContinue
        Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    Write-Host '冒烟自检通过。' -ForegroundColor Green
}

Write-Step '完成'
Write-Host "把 dist\ArchiveMate 整个文件夹拷给别人即可使用（免安装）。"
Write-Host "首次运行会引导填写 GitHub Token 和仓库，配置就存在 exe 旁边。"
