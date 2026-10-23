<#
.SYNOPSIS
    把主项目源码 + gui/app 源码（不含本机密钥）暂存到 packaging\stage\app\，
    产出安装程序打包时要用的 {app}\app\ 目录内容。

.DESCRIPTION
    只拷贝运行时真正需要的文件（白名单方式，不是"拷贝全部再排除开发环境
    专用文件"）——这样以后仓库里新增什么开发用的文件（比如新的调试脚本、
    IDE 配置），不会被这份打包脚本不小心带进安装包，不需要每次都记得回来
    维护排除列表。

    根目录文件清单来自对 main.py 完整依赖闭包的实测追踪（import 图一路
    grep 到底），不是凭印象列的——包含容易漏掉的 calculate_demo_angle.py
    （captcha.py 的旋转验证码自动破解要用，main.py 自己看不出直接依赖它，
    是通过 captcha.py 间接引入的）。

    gui/app/ 整个子树按目录拷贝（用 robocopy，原生支持排除 __pycache__，
    比 PowerShell Copy-Item -Recurse 手写排除逻辑更可靠）。

    刻意 **不** 拷贝 gui\config.json——那份是开发环境专用的（指向 ..\.venv\
    相对路径），装到同事机器上要用的是 Inno Setup 安装脚本在安装时生成的
    版本（见 packaging\pddchat.iss 的 [Code] 段）。因为这里只选择性拷贝
    gui\app\ 这一个子目录，config.json（在 gui\ 下、不在 gui\app\ 下）天然
    不会被带进来，不需要额外写排除逻辑。

.NOTES
    不复制本机 .env 或 API 密钥。升级安装保留目标目录中已有的 .env；
    全新安装需单独配置 API 密钥。

.EXAMPLE
    .\packaging\stage_app.ps1
#>

$ErrorActionPreference = "Stop"

$PackagingDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $PackagingDir
$StageDir = Join-Path $PackagingDir "stage"
$AppStageDir = Join-Path $StageDir "app"

# ---- 主项目根目录文件清单（main.py 完整 import 依赖闭包，实测 grep 确认）----
$RootFiles = @(
    "main.py"
    "db.py"
    "scheduler_core.py"
    "shop_browser.py"
    "memory_monitor.py"
    "memory_worker.py"
    "paths.py"
    "stealth.py"
    "browser_settings.py"
    "pdd_chat.py"
    "appeal_submit.py"
    "aftersale_appeal.py"
    "check_appeal_results.py"
    "check_aftersale_appeal_results.py"
    "aftersale_result_settings.py"
    "result_query_settings.py"
    "check_reconsideration_results.py"
    "reconsideration.py"
    "appeal_classifier.py"
    "appeal_style.py"
    "appeal_wording.py"
    "chat_evidence.py"
    "captcha.py"
    "captcha_diagnostics.py"
    "qwen_client.py"
    "login.py"
    "blockers.py"
    "run_appeal_batch.py"
    "screenshots.py"
    "calculate_demo_angle.py"
    "migrate_json_to_db.py"
)

Write-Host "==== 清理并重建暂存目录：$AppStageDir ===="
if (Test-Path $AppStageDir) {
    Remove-Item -Recurse -Force $AppStageDir
}
New-Item -ItemType Directory -Force -Path $AppStageDir | Out-Null

Write-Host "==== 拷贝主项目根目录文件 ===="
foreach ($file in $RootFiles) {
    $src = Join-Path $RepoRoot $file
    if (-not (Test-Path $src)) {
        throw "找不到预期的源文件：$src —— 文件清单可能跟仓库现状不同步了，需要人工核对 stage_app.ps1 里的 `$RootFiles。"
    }
    Copy-Item -Path $src -Destination (Join-Path $AppStageDir $file) -Force
    Write-Host "  拷贝：$file"
}

Write-Host "==== 拷贝 gui/app 源码子树 ===="
$guiAppSrc = Join-Path $RepoRoot "gui\app"
$guiAppDest = Join-Path $AppStageDir "gui\app"
if (-not (Test-Path $guiAppSrc)) {
    throw "找不到 gui\app 源码目录：$guiAppSrc"
}
# robocopy 的退出码 0-7 都表示成功（不同数字表示不同的"发生了什么"，比如
# 1=复制了文件、2=有多余文件……只有 >= 8 才是真正的错误），不能直接用
# $LASTEXITCODE -ne 0 判断失败。
robocopy $guiAppSrc $guiAppDest /E /XD __pycache__ /XF "*.pyc" | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "robocopy 拷贝 gui/app 失败，退出码 $LASTEXITCODE"
}
Write-Host "  拷贝完成：gui\app\ -> $guiAppDest"

Write-Host "==== 排除本机 .env 和 API 密钥；升级沿用目标目录已有配置 ===="

Write-Host "==== 清理可能残留的 state\ / __pycache__\（防御性兜底） ===="
# 正常情况下这一步不会清出任何东西——上面的拷贝步骤本来就不会产生这些
# 目录。但如果有人（包括开发者自己做冒烟测试）曾经在 packaging\stage\app\
# 这份暂存目录里直接跑过 main.py/import 过 app 包，会在这里留下真实的
# state\pddchat.db（哪怕只是空库）和 .pyc 缓存——这些如果不清掉，下次编译
# 会被 [Files] 的通配符原样打进安装包，违反"同事从零开始、不带任何历史
# 数据"的要求（真出过这个问题，不是假设性的防御）。每次编译前都应该重跑
# 一遍这个脚本，这一步就是保证"重跑一遍"确实等于"干净"。
Get-ChildItem $AppStageDir -Recurse -Directory -Force |
    Where-Object { $_.Name -eq "state" -or $_.Name -eq "__pycache__" } |
    ForEach-Object {
        Write-Host "  清理残留目录：$($_.FullName)"
        Remove-Item -Recurse -Force $_.FullName
    }

Write-Host ""
Write-Host "应用源码暂存完成：$AppStageDir"
Write-Host "下一步：先跑 packaging\build_runtime.ps1（如果还没跑过），"
Write-Host "然后用 Inno Setup 编译 packaging\pddchat.iss。"
