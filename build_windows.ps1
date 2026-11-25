$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
$stageRoot = $null

try {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        & python -m venv (Join-Path $root ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Could not create the Python build environment." }
    }

    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Could not update pip." }
    & $python -m pip install -r (Join-Path $root "requirements.txt") pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Could not install the application/build dependencies." }

    $versionText = Get-Content -LiteralPath (Join-Path $root "__init__.py") -Raw
    $versionMatch = [regex]::Match($versionText, '__version__\s*=\s*"([^"]+)"')
    if (-not $versionMatch.Success) { throw "Could not read the app version from __init__.py." }
    $appName = "VideoExpressAutoCreator-$($versionMatch.Groups[1].Value)-windows-x64"

    $stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("veac-build-" + [guid]::NewGuid().ToString("N"))
    $packageRoot = Join-Path $stageRoot "videoexpress_autocreator"
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

    Get-ChildItem -LiteralPath $root -Filter "*.py" -File |
        Where-Object { $_.Name -notlike "test_*" } |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $packageRoot }
    Copy-Item -LiteralPath (Join-Path $root "static") -Destination $packageRoot -Recurse

    $entryPoint = Join-Path $stageRoot "launcher.py"
    Set-Content -LiteralPath $entryPoint -Encoding ASCII -Value @"
from videoexpress_autocreator.main import main

main()
"@

    $distPath = Join-Path $root "dist"
    $workPath = Join-Path $root "build\pyinstaller"
    $staticData = "$(Join-Path $root 'static');videoexpress_autocreator\static"
    New-Item -ItemType Directory -Path $distPath -Force | Out-Null
    New-Item -ItemType Directory -Path $workPath -Force | Out-Null

    & $python -m PyInstaller --noconfirm --clean --onefile --console `
        --name $appName `
        --paths $stageRoot `
        --add-data $staticData `
        --collect-all flask `
        --collect-all playwright `
        --collect-all imageio_ffmpeg `
        --collect-all numpy `
        --distpath $distPath `
        --workpath $workPath `
        --specpath $stageRoot `
        $entryPoint
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed to build the Windows executable." }

    Write-Output "Built: $(Join-Path $distPath "$appName.exe")"
}
finally {
    if ($stageRoot -and (Test-Path -LiteralPath $stageRoot)) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
    Pop-Location
}
