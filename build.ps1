param(
    [string]$Name = "MeshLabRF"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $repoRoot

python tools\create_icon.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed with exit code $LASTEXITCODE" }
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed with exit code $LASTEXITCODE" }

pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $Name `
    --icon assets\meshlab.ico `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "Built: $repoRoot\dist\$Name.exe" -ForegroundColor Cyan
