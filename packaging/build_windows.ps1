$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
python -m pip install ".[dev]" "pyinstaller>=6.10,<7"
python -m PyInstaller --noconfirm --clean packaging/SymbolPayroll.spec

if (-not (Test-Path "dist/SymbolPayroll/SymbolPayroll.exe")) {
    throw "PyInstaller did not produce dist/SymbolPayroll/SymbolPayroll.exe"
}

Write-Host "Portable application built: dist/SymbolPayroll"
