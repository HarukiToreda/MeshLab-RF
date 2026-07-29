param(
    [string]$Name = "MeshLabRF"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $repoRoot

python tools\create_icon.py
python -m unittest discover -s tests -v

pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $Name `
    --icon assets\meshlab.ico `
    main.py

Write-Host ""
Write-Host "Built: $repoRoot\dist\$Name.exe" -ForegroundColor Cyan
