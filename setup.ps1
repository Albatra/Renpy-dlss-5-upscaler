# setup.ps1 - RenPyHD bootstrap / installation de RenPyHD
#
#   1. Downloads the official "DLSS 5 Visual Enhancer v3.0" release zip (Merserk, MIT) with resume + retry,
#      verifies its size and SHA-256, extracts it to DLSS5\.
#   2. Builds RenPyHD.exe from launcher\launcher.cs with csc.exe (.NET Framework) if it is missing.
#
#   1. Télécharge la version officielle « DLSS 5 Visual Enhancer v3.0 » (Merserk, MIT) avec reprise et nouvelles
#      tentatives, vérifie sa taille et son SHA-256, l'extrait dans DLSS5\.
#   2. Compile RenPyHD.exe depuis launcher\launcher.cs avec csc.exe (.NET Framework) s'il manque.
#
# Usage:  powershell -ExecutionPolicy Bypass -File setup.ps1 [-LocalZip <path>] [-Force] [-SkipBuild] [-KeepZip]
#   -LocalZip  use an already downloaded DLSS.5.Visual.Enhancer.v3.0.zip instead of downloading (offline install)
#   -Force     re-download / re-extract even if DLSS5\ already exists
#   -SkipBuild do not build RenPyHD.exe
#   -KeepZip   keep DLSS5.zip after extraction (default: kept only if the extraction failed)
param(
    [string]$LocalZip = "",
    [switch]$Force,
    [switch]$SkipBuild,
    [switch]$KeepZip
)

$ErrorActionPreference = "Stop"
$Url = "https://github.com/Merserk/dlss5-visual-enhancer/releases/download/3.0/DLSS.5.Visual.Enhancer.v3.0.zip"
$ExpectedSize = 466919995
$ExpectedSha256 = "6F0590D81677484F4ECDFAA5C44FC2A0E1A3835D33EEFC59D656E6C3BCF35F6A"
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Zip = Join-Path $Root "DLSS5.zip"
$Dest = Join-Path $Root "DLSS5"
$Exe = Join-Path $Root "RenPyHD.exe"
$LauncherSrc = Join-Path $Root "launcher\launcher.cs"
$LauncherIco = Join-Path $Root "launcher\renpyhd.ico"

function Say([string]$en, [string]$fr) {
    Write-Host ""
    Write-Host ("[EN] " + $en) -ForegroundColor Cyan
    Write-Host ("[FR] " + $fr) -ForegroundColor DarkCyan
}
function Fail([string]$en, [string]$fr) {
    Write-Host ""
    Write-Host ("[EN] ERROR: " + $en) -ForegroundColor Red
    Write-Host ("[FR] ERREUR : " + $fr) -ForegroundColor Red
    exit 1
}
function Format-Size([long]$n) {
    if ($n -ge 1GB) { return ("{0:N2} GB" -f ($n / 1GB)) }
    if ($n -ge 1MB) { return ("{0:N1} MB" -f ($n / 1MB)) }
    return ("{0:N0} KB" -f ($n / 1KB))
}

Write-Host "=============================================================" -ForegroundColor Green
Write-Host " RenPyHD setup - DLSS 5 Neural Rendering for Ren'Py games" -ForegroundColor Green
Write-Host " RenPyHD - installation" -ForegroundColor Green
Write-Host "=============================================================" -ForegroundColor Green
Write-Host ("Folder / Dossier : " + $Root)

# ----------------------------------------------------------------------------
# 1. DLSS 5 Visual Enhancer
# ----------------------------------------------------------------------------
$toolReady = (Test-Path (Join-Path $Dest "app.py")) -and (Test-Path (Join-Path $Dest "bin\python-3.13.15-embed-amd64\python.exe"))
if ($toolReady -and -not $Force) {
    Say "DLSS5\ is already installed (use -Force to reinstall)." "DLSS5\ est déjà installé (utilisez -Force pour réinstaller)."
} else {
    # ---- 1a. get the zip -----------------------------------------------------
    $needDownload = $true
    if ($LocalZip) {
        if (-not (Test-Path $LocalZip)) { Fail "local zip not found: $LocalZip" "zip local introuvable : $LocalZip" }
        Say "Using the local zip: $LocalZip" "Utilisation du zip local : $LocalZip"
        if ((Resolve-Path $LocalZip).Path -ne $Zip) { Copy-Item -LiteralPath $LocalZip -Destination $Zip -Force }
        $needDownload = $false
    } elseif ((Test-Path $Zip) -and (Get-Item $Zip).Length -eq $ExpectedSize) {
        Say "DLSS5.zip is already present and complete." "DLSS5.zip est déjà présent et complet."
        $needDownload = $false
    }

    if ($needDownload) {
        Say ("Downloading the DLSS 5 Visual Enhancer v3.0 (" + (Format-Size $ExpectedSize) + ") from GitHub. This can take several minutes; the download resumes if interrupted.") `
            ("Téléchargement du DLSS 5 Visual Enhancer v3.0 (" + (Format-Size $ExpectedSize) + ") depuis GitHub. Cela peut prendre plusieurs minutes ; le téléchargement reprend s'il est interrompu.")
        Write-Host $Url
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $part = "$Zip.part"
        $attempt = 0
        $maxAttempts = 10
        $ok = $false
        while (-not $ok -and $attempt -lt $maxAttempts) {
            $attempt++
            $have = 0
            if (Test-Path $part) { $have = (Get-Item $part).Length }
            if ($have -gt $ExpectedSize) { Remove-Item $part -Force; $have = 0 }
            if ($have -eq $ExpectedSize) { $ok = $true; break }
            try {
                $req = [System.Net.HttpWebRequest]::Create($Url)
                $req.UserAgent = "RenPyHD-setup/1.0"
                $req.Timeout = 60000
                $req.ReadWriteTimeout = 60000
                $req.AllowAutoRedirect = $true
                if ($have -gt 0) { $req.AddRange([long]$have) }
                $resp = $req.GetResponse()
                $status = [int]$resp.StatusCode
                if ($have -gt 0 -and $status -ne 206) {
                    # server ignored the range: start over
                    $resp.Close(); Remove-Item $part -Force -ErrorAction SilentlyContinue; $have = 0
                    $req = [System.Net.HttpWebRequest]::Create($Url); $req.UserAgent = "RenPyHD-setup/1.0"; $req.Timeout = 60000; $req.ReadWriteTimeout = 60000
                    $resp = $req.GetResponse()
                }
                $total = $have + $resp.ContentLength
                $in = $resp.GetResponseStream()
                $out = [System.IO.File]::Open($part, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write)
                try {
                    $buf = New-Object byte[] (1MB)
                    $done = $have
                    $lastShown = [DateTime]::Now
                    $t0 = [DateTime]::Now
                    $startBytes = $have
                    while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                        $out.Write($buf, 0, $n)
                        $done += $n
                        if (([DateTime]::Now - $lastShown).TotalSeconds -ge 1) {
                            $elapsed = ([DateTime]::Now - $t0).TotalSeconds
                            $speed = if ($elapsed -gt 0) { ($done - $startBytes) / $elapsed } else { 0 }
                            $pct = if ($total -gt 0) { 100.0 * $done / $total } else { 0 }
                            Write-Progress -Activity "DLSS 5 Visual Enhancer v3.0" -Status ("{0} / {1}  ({2:N1} %)  {3}/s  attempt {4}" -f (Format-Size $done), (Format-Size $total), $pct, (Format-Size ([long]$speed)), $attempt) -PercentComplete ([Math]::Min(100, $pct))
                            $lastShown = [DateTime]::Now
                        }
                    }
                } finally {
                    $out.Close(); $in.Close(); $resp.Close()
                }
                Write-Progress -Activity "DLSS 5 Visual Enhancer v3.0" -Completed
                if ((Get-Item $part).Length -eq $ExpectedSize) { $ok = $true }
                elseif ((Get-Item $part).Length -gt $ExpectedSize) { Remove-Item $part -Force }
            } catch {
                Write-Host ("  attempt {0}/{1} failed / tentative échouée : {2}" -f $attempt, $maxAttempts, $_.Exception.Message) -ForegroundColor Yellow
                Start-Sleep -Seconds ([Math]::Min(30, 3 * $attempt))
            }
        }
        if (-not $ok) {
            Fail "download failed after $maxAttempts attempts. Run setup.bat again to resume, or download the zip manually from the URL above and run: setup.bat -LocalZip <file>" `
                 "téléchargement échoué après $maxAttempts tentatives. Relancez setup.bat pour reprendre, ou téléchargez le zip à la main depuis l'URL ci-dessus puis lancez : setup.bat -LocalZip <fichier>"
        }
        Move-Item -LiteralPath $part -Destination $Zip -Force
    }

    # ---- 1b. verify --------------------------------------------------------
    Say "Verifying the archive (size + SHA-256)..." "Vérification de l'archive (taille + SHA-256)..."
    $len = (Get-Item $Zip).Length
    if ($len -ne $ExpectedSize) {
        Remove-Item $Zip -Force
        Fail "unexpected size ($len bytes, expected $ExpectedSize). The file was deleted: run setup.bat again." "taille inattendue ($len octets, attendu $ExpectedSize). Le fichier a été supprimé : relancez setup.bat."
    }
    $hash = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($hash -ne $ExpectedSha256) {
        Remove-Item $Zip -Force
        Fail "SHA-256 mismatch ($hash). The file was deleted: run setup.bat again." "SHA-256 incorrect ($hash). Le fichier a été supprimé : relancez setup.bat."
    }
    Write-Host ("  SHA-256 OK: " + $hash)

    # ---- 1c. extract -------------------------------------------------------
    Say "Extracting to DLSS5\ (about 1.2 GB, a few minutes)..." "Extraction dans DLSS5\ (environ 1,2 Go, quelques minutes)..."
    if (Test-Path $Dest) { Remove-Item -LiteralPath $Dest -Recurse -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Dest)
    foreach ($d in @("jobs", "logs", "outputs")) { New-Item -ItemType Directory -Force -Path (Join-Path $Dest $d) | Out-Null }
    if (-not (Test-Path (Join-Path $Dest "app.py"))) { Fail "extraction produced no DLSS5\app.py" "l'extraction n'a pas produit DLSS5\app.py" }
    if (-not $KeepZip) { Remove-Item $Zip -Force -ErrorAction SilentlyContinue }
    Say "DLSS 5 Visual Enhancer installed in DLSS5\." "DLSS 5 Visual Enhancer installé dans DLSS5\."
}

# ----------------------------------------------------------------------------
# 2. RenPyHD.exe (launcher)
# ----------------------------------------------------------------------------
if ($SkipBuild) {
    Say "Launcher build skipped (-SkipBuild); use run.bat." "Compilation du lanceur ignorée (-SkipBuild) ; utilisez run.bat."
} elseif ((Test-Path $Exe) -and -not $Force) {
    Say "RenPyHD.exe already present." "RenPyHD.exe déjà présent."
} else {
    $csc = $null
    foreach ($c in @("$env:windir\Microsoft.NET\Framework64\v4.0.30319\csc.exe", "$env:windir\Microsoft.NET\Framework\v4.0.30319\csc.exe")) {
        if (Test-Path $c) { $csc = $c; break }
    }
    if (-not $csc) {
        Say "csc.exe (.NET Framework 4) not found: RenPyHD.exe was not built. Use run.bat to start RenPyHD (same thing, without the app window icon)." `
            "csc.exe (.NET Framework 4) introuvable : RenPyHD.exe n'a pas été compilé. Utilisez run.bat pour démarrer RenPyHD (même chose, sans l'icône)."
    } else {
        Say "Building RenPyHD.exe with csc.exe..." "Compilation de RenPyHD.exe avec csc.exe..."
        $cscArgs = @("/nologo", "/target:exe", "/optimize+", "/out:$Exe", "/reference:System.Windows.Forms.dll", "/reference:System.dll")
        if (Test-Path $LauncherIco) { $cscArgs += "/win32icon:$LauncherIco" }
        $cscArgs += $LauncherSrc
        & $csc @cscArgs
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Exe)) {
            Say "Build failed: use run.bat to start RenPyHD." "Compilation échouée : utilisez run.bat pour démarrer RenPyHD."
        } else {
            Say "RenPyHD.exe built." "RenPyHD.exe compilé."
        }
    }
}

# ----------------------------------------------------------------------------
# 3. Python packages for the Unity tool (UnityPy + texture codecs), installed into the embedded Python
# ----------------------------------------------------------------------------
$Py = Join-Path $Dest "bin\python-3.13.15-embed-amd64\python.exe"
if (Test-Path $Py) {
    $env:PYTHONNOUSERSITE = "1"
    & $Py -c "import UnityPy, etcpak, texture2ddecoder, astc_encoder" 2>$null
    if ($LASTEXITCODE -eq 0 -and -not $Force) {
        Say "UnityPy already installed (Unity tool)." "UnityPy déjà installé (outil Unity)."
    } else {
        Say "Installing UnityPy and the texture codecs (Unity tool, about 8 MB)..." "Installation d'UnityPy et des codecs de textures (outil Unity, environ 8 Mo)..."
        & $Py -m pip install --only-binary=:all: --disable-pip-version-check --quiet "UnityPy>=1.25,<2" "etcpak>=0.9.15" "texture2ddecoder>=1.0.6" "astc-encoder-py>=0.1.12"
        if ($LASTEXITCODE -ne 0) {
            Say "UnityPy install failed: the Unity tool will be unavailable (run setup.bat again with a network connection)." `
                "Installation d'UnityPy échouée : l'outil Unity sera indisponible (relancez setup.bat avec une connexion réseau)."
        } else {
            Say "UnityPy installed." "UnityPy installé."
        }
    }
}

Write-Host ""
Write-Host "=============================================================" -ForegroundColor Green
Write-Host " Done. Start RenPyHD with RenPyHD.exe (or run.bat)." -ForegroundColor Green
Write-Host " Terminé. Lancez RenPyHD avec RenPyHD.exe (ou run.bat)." -ForegroundColor Green
Write-Host "=============================================================" -ForegroundColor Green
exit 0
