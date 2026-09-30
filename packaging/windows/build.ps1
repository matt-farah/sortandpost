# Build dist\sortandpost\sortandpost.exe + the installer and a portable zip (V2 P6).
#
#   pwsh packaging\windows\build.ps1
#
# Needs: Windows x64, the repo venv with the `desktop` extra (`uv sync --extra
# desktop`), network access for the one-time exiftool download, and Inno Setup 6
# for the installer (`winget install JRSoftware.InnoSetup`; without it only the
# zip is made).
#
# Unsigned: the first launch shows SmartScreen's "Windows protected your PC"
# (More info -> Run anyway). Code signing is out of scope, as on macOS.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Py = if ($env:PYTHON) { $env:PYTHON } else { "$Repo\.venv\Scripts\python.exe" }
$Work = "$Repo\build\windows"
$Dist = "$Repo\dist"

$ExiftoolVersion = '13.59'
$ExiftoolSha256 = '44b512b25af500724ba579d0a53c8fc5851628b692dd5e5d94ae4a15c2cba9ec'  # exiftool.org/checksums.txt
$ExiftoolUrl = "https://sourceforge.net/projects/exiftool/files/exiftool-${ExiftoolVersion}_64.zip/download"

if (-not [Environment]::Is64BitOperatingSystem) { throw 'build on 64-bit Windows' }
if (Get-Command uv -ErrorAction SilentlyContinue) {
    # --inexact: keep whatever else the dev venv has (pytest, ruff, ...).
    Push-Location $Repo; try { uv sync --extra desktop --inexact; if ($LASTEXITCODE) { throw 'uv sync failed' } } finally { Pop-Location }
}
& $Py -c 'import PyInstaller, pystray, torch, open_clip'
if ($LASTEXITCODE) { throw "missing desktop build deps: install the 'desktop' extra into $Py's env" }
$Version = (& $Py -c 'import sortandpost; print(sortandpost.__version__)').Trim()
Write-Host "==> sortandpost $Version"

New-Item -ItemType Directory -Force "$Work\downloads", $Dist | Out-Null

# -- exiftool (the standalone Windows build: exiftool.exe + exiftool_files\) -- #
$Zip = "$Work\downloads\exiftool-${ExiftoolVersion}_64.zip"
if (-not (Test-Path $Zip)) {
    Write-Host "==> downloading exiftool $ExiftoolVersion"
    curl.exe -sSfL -o "$Zip.part" $ExiftoolUrl
    if ($LASTEXITCODE) { throw 'exiftool download failed' }
    Move-Item -Force "$Zip.part" $Zip
}
if ((Get-FileHash -Algorithm SHA256 $Zip).Hash -ne $ExiftoolSha256) {
    throw "exiftool checksum mismatch -- delete $Zip and retry"
}
Remove-Item -Recurse -Force "$Work\exiftool-src", "$Work\exiftool" -ErrorAction SilentlyContinue
Expand-Archive $Zip "$Work\exiftool-src"
$Src = "$Work\exiftool-src\exiftool-${ExiftoolVersion}_64"
New-Item -ItemType Directory "$Work\exiftool" | Out-Null
# Shipped as `exiftool(-k).exe` (the -k pauses for a keypress); renamed, it doesn't.
Copy-Item "$Src\exiftool(-k).exe" "$Work\exiftool\exiftool.exe"
Copy-Item -Recurse "$Src\exiftool_files" "$Work\exiftool\exiftool_files"
$Ver = (& "$Work\exiftool\exiftool.exe" -ver).Trim()  # sanity: runs standalone
if ($Ver -ne $ExiftoolVersion) { throw "unpacked exiftool reports $Ver" }

# -- icon ------------------------------------------------------------------ #
& $Py "$Repo\packaging\windows\make_icon.py" $Work | Out-Null
if ($LASTEXITCODE) { throw 'icon render failed' }

# -- bundle ---------------------------------------------------------------- #
Write-Host '==> PyInstaller (a few minutes: torch is big)'
Remove-Item -Recurse -Force "$Dist\sortandpost" -ErrorAction SilentlyContinue
$env:SP_EXIFTOOL_DIR = "$Work\exiftool"
$env:SP_ICO = "$Work\sortandpost.ico"
& $Py -m PyInstaller --noconfirm --log-level WARN --distpath $Dist --workpath "$Work\pyinstaller" `
    "$Repo\packaging\windows\sortandpost.spec"
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
$App = "$Dist\sortandpost"

$Bundled = Get-ChildItem -Recurse -Path $App -Filter exiftool.exe | Select-Object -First 1
if (-not $Bundled) { throw 'bundled exiftool missing' }
Write-Host "==> bundled exiftool $((& $Bundled.FullName -ver).Trim())"

# -- portable zip ---------------------------------------------------------- #
$Portable = "$Dist\sortandpost-$Version-win-x64.zip"
Remove-Item -Force $Portable -ErrorAction SilentlyContinue
Compress-Archive -Path $App -DestinationPath $Portable -CompressionLevel Optimal

# -- installer ------------------------------------------------------------- #
$Iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue)?.Source,
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($Iscc) {
    Write-Host '==> Inno Setup'
    & $Iscc /Q "/DAppVersion=$Version" "/DSourceDir=$App" "/DIconFile=$Work\sortandpost.ico" `
        "/DOutputDir=$Dist" "$Repo\packaging\windows\sortandpost.iss"
    if ($LASTEXITCODE) { throw 'Inno Setup failed' }
} else {
    Write-Warning 'Inno Setup 6 not found (winget install JRSoftware.InnoSetup): no installer, zip only'
}

function Show-Size($Path) {
    $bytes = if ((Get-Item $Path).PSIsContainer) {
        (Get-ChildItem -Recurse -File $Path | Measure-Object Length -Sum).Sum
    } else { (Get-Item $Path).Length }
    Write-Host ('==> {0,6:N0} MB  {1}' -f ($bytes / 1MB), $Path)
}
Show-Size $App
Show-Size $Portable
$Setup = "$Dist\sortandpost-$Version-setup.exe"
if (Test-Path $Setup) { Show-Size $Setup }
