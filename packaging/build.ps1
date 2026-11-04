<#
.SYNOPSIS
    构建 GUET 畅课助手（PyInstaller + Inno Setup）。

.DESCRIPTION
    两个变体：
      full = 内置 Chromium（约 150MB 安装包，装完即用，不依赖系统浏览器）
      lite = 不带内核（约 40MB，用系统自带的 Edge，Windows 10/11 默认都有）

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Variant lite
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Variant full -Version 1.0.1
#>
param(
    [ValidateSet('full', 'lite', 'both')]
    [string]$Variant = 'both',
    [string]$Version = '1.0.0'
)

$ErrorActionPreference = 'Stop'
$packaging = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $packaging
Push-Location $root
try {
    $iscc = $null
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Inno Setup 6\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )
    foreach ($c in $candidates) { if (Test-Path $c) { $iscc = $c; break } }
    if (-not $iscc) {
        # 有些机器 Inno 装在别处（比如用户目录被迁到 F:），从注册表里找
        $keys = @(
            'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
            'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
        )
        foreach ($k in $keys) {
            Get-ItemProperty $k -ErrorAction SilentlyContinue |
                Where-Object { $_.DisplayName -like '*Inno Setup*' -and $_.InstallLocation } |
                ForEach-Object {
                    $p = Join-Path $_.InstallLocation 'ISCC.exe'
                    if (-not $iscc -and (Test-Path $p)) { $iscc = $p }
                }
        }
    }
    if (-not $iscc) {
        $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    Write-Host "Inno Setup: $iscc" -ForegroundColor DarkGray

    $targets = if ($Variant -eq 'both') { @('full', 'lite') } else { @($Variant) }

    foreach ($v in $targets) {
        Write-Host "`n===== 构建 $v 版 =====" -ForegroundColor Cyan
        $env:GUET_BUNDLE_BROWSER = if ($v -eq 'full') { '1' } else { '0' }

        if ($v -eq 'full' -and -not (Test-Path (Join-Path $packaging 'browsers\chromium-*'))) {
            Write-Warning "packaging\browsers 里没有 Chromium，full 版将不含内核（等同 lite）"
        }

        python -m PyInstaller --noconfirm --clean `
            --distpath "dist\$v" --workpath "build\$v" `
            (Join-Path $packaging 'guet_shuake.spec')
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（$v）" }

        $exe = "dist\$v\GuetShuake\GuetShuake.exe"
        if (-not (Test-Path $exe)) { throw "没有生成 $exe" }
        $size = (Get-ChildItem "dist\$v\GuetShuake" -Recurse | Measure-Object Length -Sum).Sum / 1MB
        Write-Host ("  产物：{0}（{1:N0} MB）" -f $exe, $size) -ForegroundColor Green

        if ($iscc) {
            & $iscc "/DVariant=$v" "/DAppVersion=$Version" (Join-Path $packaging 'installer.iss')
            if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败（$v）" }
        } else {
            Write-Warning "没找到 ISCC.exe，跳过安装包；可用 dist\$v\GuetShuake 目录直接分发"
        }
    }

    Write-Host "`n完成，安装包在 packaging\output：" -ForegroundColor Cyan
    Get-ChildItem (Join-Path $packaging 'output') -Filter *.exe -ErrorAction SilentlyContinue |
        Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } } |
        Format-Table -AutoSize
}
finally {
    Pop-Location
}
