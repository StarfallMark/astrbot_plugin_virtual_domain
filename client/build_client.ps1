param(
    [switch]$OneDirectory,
    [string]$PythonPath = "",
    [string]$OutputRoot = "$PSScriptRoot\dist",
    [string]$BuildRoot = "$PSScriptRoot\build\release"
)

$ErrorActionPreference = "Stop"
$clientRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
if (-not [System.IO.Path]::IsPathRooted($OutputRoot)) { $OutputRoot = Join-Path $clientRoot $OutputRoot }
if (-not [System.IO.Path]::IsPathRooted($BuildRoot)) { $BuildRoot = Join-Path $clientRoot $BuildRoot }
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
$env:PYINSTALLER_CONFIG_DIR = Join-Path $BuildRoot "pyinstaller-config"

$pythonPrefix = @()
$python = $null
if ($PythonPath) {
    if (Test-Path -LiteralPath $PythonPath -PathType Leaf) {
        $python = (Resolve-Path -LiteralPath $PythonPath).Path
    } else {
        $command = Get-Command $PythonPath -ErrorAction SilentlyContinue
        if ($command) { $python = $command.Source }
    }
    if (-not $python) { throw "找不到 Python：$PythonPath" }
} else {
    $localPython = Join-Path $clientRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $localPython) {
        $python = $localPython
    } else {
        foreach ($candidate in @("python", "python3")) {
            $command = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($command) { $python = $command.Source; break }
        }
        if (-not $python) {
            $launcher = Get-Command "py" -ErrorAction SilentlyContinue
            if ($launcher) { $python = $launcher.Source; $pythonPrefix = @("-3") }
        }
    }
    if (-not $python) { throw "没有找到 Python。请安装 Python 3.11 或更新版本。" }
}

& $python @pythonPrefix -c "import PyInstaller, PySide6"
if ($LASTEXITCODE -ne 0) {
    throw '打包依赖尚未安装。请在 client 目录执行：pip install -e ".[packaging]"'
}
$mode = if ($OneDirectory) { "--onedir" } else { "--onefile" }
& $python @pythonPrefix -m PyInstaller --noconfirm --clean --windowed $mode `
    --name VirtualDomain `
    --workpath (Join-Path $BuildRoot "work") `
    --specpath (Join-Path $BuildRoot "spec") `
    --distpath $OutputRoot `
    --paths (Join-Path $clientRoot "src") `
    (Join-Path $clientRoot "src\virtual_domain_client\app.py")
if ($LASTEXITCODE -ne 0) { throw "Client build failed ($LASTEXITCODE)." }
$exePath = Join-Path $OutputRoot "VirtualDomain.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) { throw "打包命令结束，但没有生成 $exePath" }
Write-Host "客户端已生成：$exePath" -ForegroundColor Green
Write-Host "构建模式：$(if ($OneDirectory) { '目录版' } else { '单文件版，可直接双击' })"
