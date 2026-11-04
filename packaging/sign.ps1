<#
.SYNOPSIS
    给 GuetShuake.exe / 安装包做代码签名。

.DESCRIPTION
    两种模式：
      selfsigned  本地生成自签名证书并签名。**不会**消除「未知发布者」提示，
                  除非用户把证书导入到「受信任的根证书颁发机构」（-TrustLocal 只在
                  当前用户下导入，方便本机测试）。
      pfx         用真实代码签名证书（.pfx）签名 —— 买了证书就走这条，
                  再配合时间戳服务器，才是真正对用户无告警的签名。

.EXAMPLE
    # 自签名（开发/测试）
    powershell -ExecutionPolicy Bypass -File packaging\sign.ps1 -Targets dist\full\GuetShuake\GuetShuake.exe

    # 用正式证书（构建后统一签名）
    powershell -ExecutionPolicy Bypass -File packaging\sign.ps1 -Mode pfx `
        -PfxPath D:\certs\my.pfx -PfxPassword ****** -Targets packaging\output\*.exe
#>
param(
    [ValidateSet('selfsigned', 'pfx')]
    [string]$Mode = 'selfsigned',
    [string]$PfxPath,
    [string]$PfxPassword,
    [string[]]$Targets,
    [switch]$TrustLocal,
    [string]$TimestampServer = 'http://timestamp.digicert.com'
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$certDir = Join-Path $here 'certs'
$devPfx = Join-Path $certDir 'guet-shuake-dev.pfx'
$devPassword = 'guet-shuake'

if (-not $Targets -or $Targets.Count -eq 0) {
    Write-Host '没有指定 -Targets，默认签 dist 与 packaging\output 下的 exe' -ForegroundColor DarkGray
    $Targets = @(
        (Join-Path $here '..\dist\full\GuetShuake\GuetShuake.exe'),
        (Join-Path $here '..\dist\lite\GuetShuake\GuetShuake.exe'),
        (Join-Path $here 'output\*.exe')
    )
}

function Resolve-Targets([string[]]$rawTargets) {
    # powershell -File 会把 "a","b" 拼成一个字符串，这里统一按逗号/分号拆开
    $patterns = @()
    foreach ($item in $rawTargets) {
        if ($null -eq $item) { continue }
        $patterns += ($item -split '[,;]')
    }
    $patterns = $patterns | ForEach-Object { $_.Trim().Trim('"').Trim("'") } | Where-Object { $_ }

    $files = @()
    foreach ($p in $patterns) {
        $matches = Get-ChildItem -Path $p -ErrorAction SilentlyContinue
        if ($matches) { $files += $matches | ForEach-Object { $_.FullName } }
    }
    return $files | Sort-Object -Unique
}

function Get-SigningCert {
    if ($Mode -eq 'pfx') {
        if (-not $PfxPath -or -not (Test-Path $PfxPath)) {
            throw "找不到证书文件：$PfxPath"
        }
        $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
        return New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($PfxPath, $PfxPassword, $flags)
    }

    # selfsigned：没有就生成一个，存成 pfx 复用（保证多次签名一致）
    New-Item -ItemType Directory -Force $certDir | Out-Null
    if (Test-Path $devPfx) {
        $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
        return New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($devPfx, $devPassword, $flags)
    }

    Write-Host '生成自签名代码签名证书（有效期 3 年）…' -ForegroundColor Cyan
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject 'CN=GUET Shuake (self-signed), O=GUET Shuake, C=CN' `
        -FriendlyName 'GUET Shuake Dev Signing' `
        -CertStoreLocation 'Cert:\CurrentUser\My' `
        -KeyUsage DigitalSignature `
        -KeyAlgorithm RSA -KeyLength 3072 `
        -NotAfter (Get-Date).AddYears(3) `
        -TextExtension @(
            '2.5.29.37={text}1.3.6.1.5.5.7.3.3',   # EKU: codeSigning
            '2.5.29.19={text}'                      # 允许做 CA 以便导入受信任根
        )
    $secure = ConvertTo-SecureString -String $devPassword -Force -AsPlainText
    Export-PfxCertificate -Cert $cert -FilePath $devPfx -Password $secure | Out-Null
    Export-Certificate -Cert $cert -FilePath (Join-Path $certDir 'guet-shuake-dev.cer') | Out-Null
    Write-Host "已生成：$devPfx（密码 $devPassword）" -ForegroundColor Green
    return Get-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)"
}

$certificate = Get-SigningCert
Write-Host "签名证书：$($certificate.Subject)" -ForegroundColor Cyan
Write-Host "指纹：$($certificate.Thumbprint)" -ForegroundColor DarkGray

if ($TrustLocal) {
    try {
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store('Root', 'CurrentUser')
        $store.Open('ReadWrite')
        $store.Add($certificate)
        $store.Close()
        Write-Host '已把证书导入「当前用户 → 受信任的根证书颁发机构」（只对本机生效）' -ForegroundColor Yellow
    }
    catch {
        Write-Warning "导入受信任根失败（可手动导入 certs\guet-shuake-dev.cer）：$_"
    }
}

$files = Resolve-Targets $Targets
if (-not $files) {
    throw '没有找到可签名的文件'
}

$failed = 0
foreach ($file in $files) {
    $name = Split-Path -Leaf $file
    $size = [math]::Round((Get-Item $file).Length / 1MB, 1)
    try {
        $sig = Set-AuthenticodeSignature -FilePath $file -Certificate $certificate `
            -HashAlgorithm SHA256 -TimestampServer $TimestampServer -ErrorAction Stop
        if ($Mode -eq 'pfx' -and $sig.Status -ne 'Valid') {
            # 正式证书签完必须是 Valid，否则说明证书/时间戳有问题，不能发
            Write-Warning ("  ✗ {0} 正式证书签名后状态为 {1}：{2}" -f $name, $sig.Status, $sig.StatusMessage)
            $failed++
        }
        elseif ($sig.Status -eq 'Valid' -or $sig.Status -eq 'UnknownError') {
            # 自签名且未导入信任根时是 UnknownError —— 签名已写入，但不是「受信任」
            Write-Host ("  ✓ {0}（{1} MB）签名写入，状态：{2}" -f $name, $size, $sig.Status) -ForegroundColor Green
        }
        else {
            Write-Warning ("  ! {0} 状态 {1}：{2}" -f $name, $sig.Status, $sig.StatusMessage)
            $failed++
        }
    }
    catch {
        Write-Warning ("  ✗ {0} 签名失败：{1}" -f $name, $_.Exception.Message)
        $failed++
    }
}

Write-Host ''
foreach ($file in $files) {
    $sig = Get-AuthenticodeSignature -FilePath $file
    Write-Host ("{0,-42} {1,-14} {2}" -f (Split-Path -Leaf $file), $sig.Status, $sig.SignerCertificate.Subject)
}

if ($Mode -eq 'selfsigned' -and -not $TrustLocal) {
    Write-Host "`n提示：自签名不会被系统信任，用户首次运行仍会看到「未知发布者」。" -ForegroundColor Yellow
    Write-Host "      要彻底消除提示需购买代码签名证书（OV/EV），或用 -Mode pfx 指定。" -ForegroundColor Yellow
}
exit $(if ($failed -gt 0) { 1 } else { 0 })
