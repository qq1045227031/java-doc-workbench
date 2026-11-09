$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { python -m venv .venv; if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment' } }
& $taskPython -m pip install -r requirements.txt 'pyinstaller==6.16.0'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $taskPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed, build stopped' }
& $taskPython -m PyInstaller --noconfirm --clean --onefile --console --name JavaDescriptionWorkbench --add-data 'static;static' --collect-all tree_sitter_java app.py
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '使用说明.md') -Destination (Join-Path $PSScriptRoot 'dist\使用说明.md') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD_PARTY_NOTICES.txt') -Destination (Join-Path $PSScriptRoot 'dist\THIRD_PARTY_NOTICES.txt') -Force
& $taskPython scripts/collect_licenses.py dist/licenses
if ($LASTEXITCODE -ne 0) { throw 'License collection failed' }
Compress-Archive -LiteralPath @((Join-Path $PSScriptRoot 'dist\JavaDescriptionWorkbench.exe'), (Join-Path $PSScriptRoot 'dist\使用说明.md'), (Join-Path $PSScriptRoot 'dist\THIRD_PARTY_NOTICES.txt'), (Join-Path $PSScriptRoot 'dist\licenses')) -DestinationPath (Join-Path $PSScriptRoot 'dist\JavaDescriptionWorkbench-Windows-x64.zip') -Force
Write-Output 'Build complete: dist\JavaDescriptionWorkbench-Windows-x64.zip'
& $taskPython scripts/pack_source.py
if ($LASTEXITCODE -ne 0) { throw 'Source ZIP packaging failed' }
