# Remove installation/development assets and unused Qt modules.
$ErrorActionPreference = 'Stop'
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'stage\runtime'))
$targets = @()
foreach ($runtimeName in @('pipeline', 'gui')) {
    $site = Join-Path $runtimeRoot "$runtimeName\Lib\site-packages"
    if (-not (Test-Path -LiteralPath $site)) { throw "Missing runtime: $site" }
    $targets += Join-Path $site 'pip'
    $targets += Get-ChildItem -LiteralPath $site -Directory -Filter 'pip-*.dist-info' | Select-Object -ExpandProperty FullName
    $scripts = Join-Path $runtimeRoot "$runtimeName\Scripts"
    if (Test-Path -LiteralPath $scripts) {
        $targets += Get-ChildItem -LiteralPath $scripts -File -Filter 'pip*.exe' | Select-Object -ExpandProperty FullName
    }
}
$qt = Join-Path $runtimeRoot 'gui\Lib\site-packages\PyQt5'
# Current UI imports Core/Gui/Widgets/Chart. Keep supporting native modules
# used by Windows platform, SVG/image plugins and native file dialogs too.
$keepModules = @('QtCore', 'QtGui', 'QtWidgets', 'QtChart', 'QtNetwork',
    'QtPrintSupport', 'QtSvg', 'QtXml', 'QtOpenGL', 'QtDBus')
foreach ($file in (Get-ChildItem -LiteralPath $qt -File)) {
    if ($file.Extension -in @('.pyd', '.pyi') -and
        $file.BaseName -notlike 'sip*' -and $file.BaseName -notin $keepModules) {
        $targets += $file.FullName
    }
}
$keepDlls = @('Qt5Core', 'Qt5Gui', 'Qt5Widgets', 'Qt5Charts', 'Qt5Network',
    'Qt5PrintSupport', 'Qt5Svg', 'Qt5Xml', 'Qt5OpenGL', 'Qt5DBus')
foreach ($file in (Get-ChildItem -LiteralPath (Join-Path $qt 'Qt5\bin') -Filter 'Qt5*.dll')) {
    if ($file.BaseName -notin $keepDlls) { $targets += $file.FullName }
}
$keepPlugins = @('platforms', 'platformthemes', 'styles', 'imageformats',
    'iconengines', 'printsupport', 'bearer', 'generic')
foreach ($folder in (Get-ChildItem -LiteralPath (Join-Path $qt 'Qt5\plugins') -Directory)) {
    if ($folder.Name -notin $keepPlugins) { $targets += $folder.FullName }
}
foreach ($relative in @('bindings', 'Qt5\qml', 'Qt5\qsci')) {
    $targets += Join-Path $qt $relative
}
$targets += Get-ChildItem -LiteralPath $runtimeRoot -Directory -Recurse -Filter '__pycache__' | Select-Object -ExpandProperty FullName
$bytes = 0L
foreach ($target in ($targets | Sort-Object -Unique)) {
    $full = [IO.Path]::GetFullPath($target)
    if (-not $full.StartsWith($runtimeRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target outside runtime: $full"
    }
    if (-not (Test-Path -LiteralPath $full)) { continue }
    # Resolve and check every ancestor to avoid traversing a junction/symlink.
    $current = Get-Item -LiteralPath $full -Force
    while ($null -ne $current -and $current.FullName.Length -ge $runtimeRoot.Length) {
        if ($current.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point: $($current.FullName)" }
        $current = $current.Parent
    }
    $item = Get-Item -LiteralPath $full
    if ($item.PSIsContainer) {
        $children = @(Get-ChildItem -LiteralPath $full -Recurse -Force)
        if ($children | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw "Reparse point inside: $full" }
        $bytes += ($children | Where-Object { -not $_.PSIsContainer } | Measure-Object Length -Sum).Sum
    } else { $bytes += $item.Length }
    Remove-Item -LiteralPath $full -Recurse -Force
}
Write-Host ('Runtime pruning complete: {0:N1} MiB removed' -f ($bytes / 1MB))
