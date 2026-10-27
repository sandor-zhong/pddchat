<#
.SYNOPSIS
    构建 pddchat 打包用的两份 embeddable Python 运行时（主流程 3.12 + GUI 3.11），
    并把各自项目 uv.lock 锁定的精确依赖版本装进去。

.DESCRIPTION
    产出：
        packaging\stage\runtime\pipeline\   embeddable Python 3.12 + 主项目依赖
        packaging\stage\runtime\gui\        embeddable Python 3.11 + GUI 依赖

    不在这一步跑 `playwright install chromium`——那个由 Inno Setup 安装脚本的
    安装后步骤负责（见 packaging\pddchat.iss），保持这份构建产物体积小、
    不用因为浏览器版本更新就重新打包这一大坨依赖。

    依赖版本从**已提交的 uv.lock 精确导出**（uv export），不是按
    pyproject.toml 里的版本范围重新解析——保证打包进去的版本跟开发者本机
    已经测试过的版本完全一致，不会因为打包时机不同而装出不同的依赖版本。

.NOTES
    前置条件：
    - 本机已经装好 uv，且分别在仓库根目录和 gui\ 下跑过一次 `uv sync`
      （`uv export` 依赖 uv.lock 已经跟 pyproject.toml 同步，没 sync 过会报错）。
    - 本机能访问 https://www.python.org 和 https://bootstrap.pypa.io
      （下载 embeddable Python 和 get-pip.py，只在首次构建/版本变化时需要，
      结果缓存在 packaging\downloads\ 下，不用每次都重新下载）。

.EXAMPLE
    .\packaging\build_runtime.ps1
#>

param([ValidateSet("chromium", "firefox")][string]$BrowserEngine = "chromium")

$ErrorActionPreference = "Stop"

# ---- 配置：以后升级 Python 补丁版本只需要改这两行 ----
$PipelinePyVersion = "3.12.10"  # 对应 pyproject.toml 的 requires-python = ">=3.12"
$GuiPyVersion = "3.11.9"        # 必须跟 gui\.python-version 保持一致

$PackagingDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $PackagingDir
$DownloadsDir = Join-Path $PackagingDir "downloads"
$StageDir = Join-Path $PackagingDir "stage"
$RuntimeDir = Join-Path $StageDir "runtime"

New-Item -ItemType Directory -Force -Path $DownloadsDir | Out-Null
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

function Get-CachedDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$FileName
    )
    $path = Join-Path $DownloadsDir $FileName
    if (Test-Path $path) {
        Write-Host "已缓存，跳过下载：$FileName"
    } else {
        Write-Host "下载：$Url"
        Invoke-WebRequest -Uri $Url -OutFile $path
    }
    return $path
}

function Install-EmbeddableRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Version,           # 例如 "3.12.10"
        [Parameter(Mandatory = $true)][string]$TargetDir,          # 例如 …\runtime\pipeline
        [Parameter(Mandatory = $true)][string]$RequirementsFile,
        # 相对 $TargetDir 自身的一条额外 sys.path 目录（比如 "..\..\app"）。
        # embeddable Python 只要存在 ._pth 文件，就会完全接管 sys.path 的
        # 构造——不会像普通 Python 那样自动把"脚本自身所在目录"（直接跑
        # 脚本时）或"当前工作目录"（-m 模块方式时）加进去。装好之后不管是
        # `pipeline\python.exe app\main.py` 还是 `gui\python.exe -m app.main`，
        # 都是靠这一条手动写进 ._pth 的相对路径才能找到 app\ 下的源码，
        # 不能指望 Python 自己猜（实测确认过这个坑，见本文件末尾的说明）。
        [string]$ExtraPathEntry = ""
    )

    Write-Host ""
    Write-Host "==== 构建运行时：Python $Version -> $TargetDir ===="

    if (Test-Path $TargetDir) {
        Remove-Item -Recurse -Force $TargetDir
    }
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

    $zipName = "python-$Version-embed-amd64.zip"
    $zipPath = Get-CachedDownload -Url "https://www.python.org/ftp/python/$Version/$zipName" -FileName $zipName
    Expand-Archive -Path $zipPath -DestinationPath $TargetDir -Force

    # ._pth 文件名跟解释器 DLL 同名规则：3.12.10 -> python312._pth
    $verParts = $Version.Split(".")
    $majorMinor = "$($verParts[0])$($verParts[1])"
    $pthFile = Join-Path $TargetDir "python$majorMinor._pth"
    if (-not (Test-Path $pthFile)) {
        throw "没找到预期的 ._pth 文件：$pthFile —— embeddable zip 内部结构可能变了，需要人工确认一下解压后的目录内容。"
    }

    # embeddable 发行版默认关掉 site 模块、sys.path 只有解释器目录本身，
    # 不加这几行的话 pip 装进 Lib\site-packages 的包对 Python 完全不可见
    # （这是 embeddable Python 的已知限制，不是这次构建脚本引入的坑）。
    $pthLines = @(
        "python$majorMinor.zip"
        "."
        "Lib\site-packages"
    )
    if ($ExtraPathEntry) {
        $pthLines += $ExtraPathEntry
    }
    $pthLines += @("", "import site")
    $pthLines | Set-Content -Path $pthFile -Encoding ASCII

    $pythonExe = Join-Path $TargetDir "python.exe"

    # 引导 pip（embeddable 发行版不自带 pip）
    $getPipPath = Get-CachedDownload -Url "https://bootstrap.pypa.io/get-pip.py" -FileName "get-pip.py"
    & $pythonExe $getPipPath --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip.py 执行失败：$TargetDir" }

    & $pythonExe -m pip install --no-warn-script-location -r $RequirementsFile
    if ($LASTEXITCODE -ne 0) { throw "pip install 依赖失败：$TargetDir（依赖清单：$RequirementsFile）" }

    Write-Host "==== 完成：$TargetDir ===="
}

# ---- 1. 从 uv.lock 精确导出依赖清单 ----
# --no-emit-project：只导出第三方依赖，不把 pddchat/pddchat-gui 本身当一个
# 要 pip install 的包——项目源码是 stage_app.ps1 直接拷文件过去的，不走 pip。

Write-Host "==== 导出主项目依赖清单（uv export） ===="
$pipelineRequirements = Join-Path $StageDir "requirements-pipeline.txt"
Push-Location $RepoRoot
try {
    uv export --no-dev --no-hashes --no-emit-project --format requirements.txt -o $pipelineRequirements
    if ($LASTEXITCODE -ne 0) { throw "uv export 失败（主项目）——确认已经在仓库根目录跑过 uv sync。" }
} finally {
    Pop-Location
}

Write-Host "==== 导出 GUI 依赖清单（uv export） ===="
$guiRequirements = Join-Path $StageDir "requirements-gui.txt"
Push-Location (Join-Path $RepoRoot "gui")
try {
    uv export --no-dev --no-hashes --no-emit-project --format requirements.txt -o $guiRequirements
    if ($LASTEXITCODE -ne 0) { throw "uv export 失败（gui/）——确认已经在 gui/ 目录跑过 uv sync。" }
} finally {
    Pop-Location
}

# ---- 2. 构建两份运行时 ----
# ExtraPathEntry 是相对各自 runtime\<pipeline|gui>\ 目录自身算的、装好之后
# 固定不变的相对路径（分发包内部目录结构是构建时就定死的，不依赖运行时
# cwd）：
#   runtime\pipeline\ -> ..\..\app        （main.py/db.py 等主项目源码所在）
#   runtime\gui\      -> ..\..\app\gui    （gui 的 app 包所在，-m app.main 要用）
Install-EmbeddableRuntime -Version $PipelinePyVersion `
    -TargetDir (Join-Path $RuntimeDir "pipeline") `
    -RequirementsFile $pipelineRequirements `
    -ExtraPathEntry "..\..\app"

Install-EmbeddableRuntime -Version $GuiPyVersion `
    -TargetDir (Join-Path $RuntimeDir "gui") `
    -RequirementsFile $guiRequirements `
    -ExtraPathEntry "..\..\app\gui"

# ---- 3. 下载 Chromium 浏览器二进制，随包一起带走 ----
# 同事那台电脑要做到"装完不用联网、双击就能用"，浏览器就不能是安装时
# 现下载的——改成在这台构建机器上（本来就要联网下载 embeddable Python）
# 顺手也下载一次 Chromium，把它放进 packaging\stage\browsers\，交给
# pddchat.iss 一起打进安装包。paths.py 里已经加了"存在 browsers\ 目录就
# 把 PLAYWRIGHT_BROWSERS_PATH 指过去"的逻辑，这里生成的目录结构要跟那边
# 的预期对上——用 PLAYWRIGHT_BROWSERS_PATH 环境变量控制 `playwright
# install` 下载到哪，这样下载出来的目录结构（chromium-<版本号>\...）
# 跟运行时查找时用的是同一套 Playwright 自己的内部规则，不需要我们自己
# 猜测/拼接子目录名。
Write-Host ""
Write-Host "==== 下载 $BrowserEngine 浏览器二进制（随包打入安装包）===="
$browserFolder = if ($BrowserEngine -eq "firefox") { "firefox_browsers" } else { "browsers" }
$browsersDir = Join-Path $StageDir $browserFolder
if (Test-Path -LiteralPath $browsersDir) {
    $resolvedBrowserDir = (Resolve-Path -LiteralPath $browsersDir).Path
    $expectedBrowserDir = [IO.Path]::GetFullPath((Join-Path $StageDir $browserFolder))
    if ($resolvedBrowserDir -ne $expectedBrowserDir -or
        ((Get-Item -LiteralPath $resolvedBrowserDir).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "浏览器暂存目录路径不符合预期，停止清理。"
    }
    Remove-Item -LiteralPath $resolvedBrowserDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $browsersDir | Out-Null

$pipelinePythonExe = Join-Path $RuntimeDir "pipeline\python.exe"
$previousBrowsersPathEnv = $env:PLAYWRIGHT_BROWSERS_PATH
try {
    $env:PLAYWRIGHT_BROWSERS_PATH = $browsersDir
    & $pipelinePythonExe -m playwright install $BrowserEngine
    if ($LASTEXITCODE -ne 0) { throw "playwright install $BrowserEngine 失败" }
} finally {
    # 还原这个进程自己的环境变量，不影响脚本运行完之后这台机器上其它地方
    # （比如开发者自己平时用的那份主项目 venv）的行为。
    if ($null -eq $previousBrowsersPathEnv) {
        Remove-Item Env:\PLAYWRIGHT_BROWSERS_PATH -ErrorAction SilentlyContinue
    } else {
        $env:PLAYWRIGHT_BROWSERS_PATH = $previousBrowsersPathEnv
    }
}
$browsersSizeMb = (Get-ChildItem $browsersDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("==== 完成：$browsersDir （约 {0:N0} MB） ====" -f $browsersSizeMb)

# ---- 4. 下载 Visual C++ Redistributable（随包带走，装的时候静默装一次）----
# Chromium 自己的二进制（chrome-win64\chrome.exe）不带 vcruntime140.dll /
# vcruntime140_1.dll / msvcp140.dll / ucrtbase.dll 这些 Visual C++ 运行库
# ——Google 官方 Chromium 构建假定操作系统本身已经有（实测确认过：这次
# 下载出来的 chrome-win64\ 目录里翻了一遍，压根没有这几个文件）。开发机
# 上装过各种开发工具，早就有这些 DLL 了，从来没暴露过这个问题；全新机器
# 、尤其是没装过任何开发工具/大型软件的机器（比如只跑控制面板类软件的
# 服务器型主机）很可能缺，缺了之后 Playwright 启动 Chromium 会在
# launch_persistent_context 深处报错，这份报错文本目前还会被
# DEPENDENCIES_VALIDATED 这个陈旧标记文件整个盖住（见 pddchat.iss 里
# stage\browsers\* 那条 [Files] 的 Excludes 说明），同事那边最终只会
# 看到一个语焉不详的"退出码 120"。
#
# 修法：把官方 VC++ Redistributable 安装包也下载下来随包带走，安装阶段
# 静默装一次（见 pddchat.iss 的 [Run] 段）——不管目标机器缺不缺，装完之后
# Chromium 依赖的这几个系统级 DLL 都一定会有。
#
# 用的是 aka.ms 官方永久重定向短链（微软自己在"下载最新支持的 Visual C++
# 可再发行组件包"支持页面上公开发布、长期维护的入口，不是钉死某个具体
# 版本号的直链）——⚠ 这里对这个链接当前确实指向 x64 版本可再发行组件包
# 有较高把握但没有在本机验证过，第一次跑这个脚本、或者这个链接哪天突然
# 失效时，务必确认下载下来的文件确实是一个正常大小（十几 MB 量级）的
# PE 可执行文件，不是重定向失败后收到的一个 HTML 错误页——必要时去
# https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist
# 核实当前应该用的确切链接。
Write-Host ""
Write-Host "==== 下载 Visual C++ Redistributable（随包打入安装包，安装时静默装一次）===="
$vcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
$vcRedistCachedPath = Get-CachedDownload -Url $vcRedistUrl -FileName "VC_redist.x64.exe"
$vcRedistStagePath = Join-Path $StageDir "VC_redist.x64.exe"
Copy-Item -Path $vcRedistCachedPath -Destination $vcRedistStagePath -Force
Write-Host "==== 完成：$vcRedistStagePath ===="

$guiPythonExe = Join-Path $RuntimeDir "gui\python.exe"
& (Join-Path $PSScriptRoot 'prune_runtime.ps1')
$smokeTestScript = Join-Path $StageDir "_pyqt5_smoke_test.py"  # 生成在 stage\ 下（已 gitignore），不污染 packaging\ 里要跟踪进版本库的脚本文件
'from PyQt5.QtWidgets import QApplication' | Set-Content -Path $smokeTestScript -Encoding ASCII
'QApplication([])' | Add-Content -Path $smokeTestScript -Encoding ASCII
"print('PyQt5 OK')" | Add-Content -Path $smokeTestScript -Encoding ASCII

Write-Host ""
Write-Host "运行时构建完成：$RuntimeDir"
Write-Host "建议现在就冒烟测试一下 GUI 那份能不能正常 import PyQt5"
Write-Host "（embeddable Python 下 Qt 插件加载是这次打包方案里唯一没有实测验证过的环节）："
Write-Host "  $guiPythonExe $smokeTestScript"
Write-Host ""
Write-Host "下一步：跑 packaging\stage_app.ps1 暂存应用源码。"
