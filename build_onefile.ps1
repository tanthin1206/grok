param(
    [switch]$InstallDeps,
    [switch]$NoClean
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$specPath = Join-Path $root 'grok_onefile.spec'
$exePath = Join-Path $root 'dist\RUN_GROK.exe'

function Resolve-PythonCommand {
    $candidates = @('py -3', 'py', 'python')
    foreach ($candidate in $candidates) {
        try {
            & cmd.exe /c "$candidate --version" *> $null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }
    throw 'Khong tim thay Python launcher (py/python) trong PATH.'
}

if (-not (Test-Path $specPath)) {
    throw "Khong tim thay file spec: $specPath"
}

$pythonCmd = Resolve-PythonCommand
Write-Host "[BUILD] Python command: $pythonCmd"
Write-Host "[BUILD] Spec file: $specPath"

if ($InstallDeps) {
    Write-Host '[BUILD] Cai dependencies...'
    & cmd.exe /c "$pythonCmd -m pip install --upgrade pip"
    if ($LASTEXITCODE -ne 0) { throw 'Khong the nang cap pip.' }

    if (Test-Path (Join-Path $root 'requirements.txt')) {
        & cmd.exe /c "$pythonCmd -m pip install -r requirements.txt"
        if ($LASTEXITCODE -ne 0) { throw 'Khong the cai requirements.txt.' }
    }

    if (Test-Path (Join-Path $root 'requirements-build.txt')) {
        & cmd.exe /c "$pythonCmd -m pip install -r requirements-build.txt"
        if ($LASTEXITCODE -ne 0) { throw 'Khong the cai requirements-build.txt.' }
    }
}
else {
    & cmd.exe /c "$pythonCmd -m pip install --upgrade pyinstaller pyinstaller-hooks-contrib"
    if ($LASTEXITCODE -ne 0) { throw 'Khong the cai PyInstaller.' }
}

if (-not $NoClean) {
    Write-Host '[BUILD] Xoa thu muc build/dist cu...'
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root 'build')
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root 'dist')
}

Write-Host '[BUILD] Tao onefile exe...'
& cmd.exe /c "$pythonCmd -m PyInstaller --noconfirm --clean --distpath dist --workpath build grok_onefile.spec"
if ($LASTEXITCODE -ne 0) { throw 'Build PyInstaller that bai. Kiem tra log phia tren de biet module thieu hoac option sai.' }

if (Test-Path $exePath) {
    Write-Host "[DONE] Build thanh cong: $exePath"
}
else {
    throw 'Build xong nhung khong tim thay file exe trong dist.'
}
