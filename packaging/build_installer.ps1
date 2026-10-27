# Default: reuse staged runtimes, refresh application sources, build lite installer.
param([string]$CompilerPath = '')
$ErrorActionPreference = 'Stop'
if (-not $CompilerPath) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $CompilerPath = $command.Source }
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )) {
        if (-not $CompilerPath -and (Test-Path -LiteralPath $candidate)) { $CompilerPath = $candidate }
    }
}
if (-not $CompilerPath -or -not (Test-Path -LiteralPath $CompilerPath)) {
    throw 'Inno Setup compiler not found. Supply -CompilerPath with the path to ISCC.exe.'
}
& (Join-Path $PSScriptRoot 'prune_runtime.ps1')
$guiPython = Join-Path $PSScriptRoot 'stage\runtime\gui\python.exe'
& $guiPython -B -c "from PyQt5.QtWidgets import QApplication, QFileDialog; from PyQt5.QtChart import QChart, QChartView; from PyQt5.QtSvg import QSvgRenderer; from PyQt5.QtGui import QPixmap; a=QApplication(['packaging-check', '-platform', 'offscreen']); v=QChartView(QChart()); d=QFileDialog(); p=QPixmap(32,32); assert not p.isNull(); print('Qt runtime check OK')"
if ($LASTEXITCODE -ne 0) { throw 'Qt runtime validation failed; installer build stopped.' }
& (Join-Path $PSScriptRoot 'stage_app.ps1')
& $CompilerPath (Join-Path $PSScriptRoot 'pddchat.iss')
if ($LASTEXITCODE -ne 0) { throw "Installer build failed: $LASTEXITCODE" }
