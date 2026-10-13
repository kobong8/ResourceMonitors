param(
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    & $Python -m venv .venv-build
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    $BuildPython = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"
    & $BuildPython -X utf8 -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
    & $BuildPython -m PyInstaller --noconfirm --clean --onefile --console --name ResourceMonitorAgent --hidden-import pynvml --distpath dist --workpath build agent.py
    if ($LASTEXITCODE -ne 0) { throw "EXE build failed" }
    # Never overwrite a deployed configuration containing real credentials.
    if (-not (Test-Path -LiteralPath "dist\config.yaml")) {
        Copy-Item -LiteralPath "config.yaml" -Destination "dist\config.yaml"
    }
    & ".\dist\ResourceMonitorAgent.exe" --help
    if ($LASTEXITCODE -ne 0) { throw "EXE smoke test failed" }
    Write-Host "Built: $PSScriptRoot\dist\ResourceMonitorAgent.exe"
    Write-Host "Edit dist\config.yaml before deploying."
} finally {
    Pop-Location
}
