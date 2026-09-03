# build_release.ps1 - builds release\RenPyHD-v<version>-win64.zip (app, launcher, setup, docs - WITHOUT DLSS5\).
# Usage: powershell -ExecutionPolicy Bypass -File build_release.ps1
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
$Name = "RenPyHD-v$Version-win64"
$Release = Join-Path $Root "release"
$Stage = Join-Path $Release $Name
$ZipPath = Join-Path $Release "$Name.zip"

if (-not (Test-Path (Join-Path $Root "RenPyHD.exe"))) {
    Write-Host "RenPyHD.exe missing: building it..." -ForegroundColor Yellow
    cmd /c (Join-Path $Root "launcher\build_launcher.bat")
    if (-not (Test-Path (Join-Path $Root "RenPyHD.exe"))) { throw "RenPyHD.exe could not be built" }
}

if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "app\i18n"), (Join-Path $Stage "launcher"), (Join-Path $Stage "tools"), (Join-Path $Stage "docs\screenshots") | Out-Null

$appFiles = @("renpy_hd_app.py", "renpy_hd_core.py", "renpy_hd_tools.py", "renpy_hd_i18n.py", "zz_dlss_hd.rpy", "README.md")
foreach ($f in $appFiles) { Copy-Item (Join-Path $Root "app\$f") (Join-Path $Stage "app\$f") }
Copy-Item (Join-Path $Root "app\i18n\*.json") (Join-Path $Stage "app\i18n\")
Copy-Item (Join-Path $Root "launcher\launcher.cs"), (Join-Path $Root "launcher\build_launcher.bat") (Join-Path $Stage "launcher\")
if (Test-Path (Join-Path $Root "launcher\renpyhd.ico")) { Copy-Item (Join-Path $Root "launcher\renpyhd.ico") (Join-Path $Stage "launcher\") }
Copy-Item (Join-Path $Root "tools\README.md") (Join-Path $Stage "tools\")
Copy-Item (Join-Path $Root "docs\screenshots\*") (Join-Path $Stage "docs\screenshots\")
foreach ($f in @("RenPyHD.exe", "setup.bat", "setup.ps1", "run.bat", "README.md", "README.en.md", "LICENSE", "THIRD_PARTY.md", "CHANGELOG.md", "VERSION")) {
    Copy-Item (Join-Path $Root $f) (Join-Path $Stage $f)
}

if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $ZipPath -CompressionLevel Optimal
Remove-Item $Stage -Recurse -Force
$size = (Get-Item $ZipPath).Length
Write-Host ("Release built: {0} ({1:N1} MB)" -f $ZipPath, ($size / 1MB)) -ForegroundColor Green
Write-Host "Users still run setup.bat once to download the DLSS 5 Visual Enhancer (not included)."
