param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8787,
    [string]$DbPath = "$PSScriptRoot\data\virtual_domain.sqlite3",
    [string]$ModelConfig = "$PSScriptRoot\config\model.json",
    [string]$ModelId = "",
    [ValidateSet("standalone", "astrbot")][string]$BotMode = "standalone",
    [string]$BotToken = "",
    [string]$ConnectorConfig = "",
    [string]$TlsCertFile = "",
    [string]$TlsKeyFile = "",
    [string]$TlsClientCaFile = "",
    [string]$TlsClientCrlFile = "",
    [string]$PythonPath = "",
    [switch]$AllowNoModel
)

$ErrorActionPreference = "Stop"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
$serverRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$env:PYTHONPATH = Join-Path $serverRoot "src"

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
    $localPython = Join-Path $serverRoot ".venv\Scripts\python.exe"
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
    if (-not $python) { throw "没有找到 Python。请安装 Python 3.11 或更新版本，然后重新启动。" }
}

$pythonVersion = & $python @pythonPrefix -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0) { throw "无法启动 Python：$python" }
$versionParts = $pythonVersion.Trim().Split('.')
if ([int]$versionParts[0] -lt 3 -or ([int]$versionParts[0] -eq 3 -and [int]$versionParts[1] -lt 11)) {
    throw "虚拟域服务端需要 Python 3.11 或更新版本，当前检测到 $pythonVersion。"
}

if ([bool]$TlsCertFile -ne [bool]$TlsKeyFile) { throw "HTTPS 证书与私钥必须同时配置。" }
if (($TlsClientCaFile -or $TlsClientCrlFile) -and -not $TlsCertFile) { throw "双向 TLS 需要先配置服务端证书和私钥。" }
if ($TlsClientCrlFile -and -not $TlsClientCaFile) { throw "客户端证书吊销检查需要配置 TlsClientCaFile。" }
foreach ($tlsPath in @($TlsCertFile, $TlsKeyFile, $TlsClientCaFile, $TlsClientCrlFile) | Where-Object { $_ }) {
    if (-not (Test-Path -LiteralPath $tlsPath -PathType Leaf)) { throw "找不到 TLS 证书文件：$tlsPath" }
}

$databasePath = [System.IO.Path]::GetFullPath($DbPath)
$databaseDirectory = Split-Path -Parent $databasePath
if (-not $databaseDirectory) { $databaseDirectory = $serverRoot }
if ($databaseDirectory) { New-Item -ItemType Directory -Path $databaseDirectory -Force | Out-Null }
$profileStore = Join-Path $databaseDirectory "model-profiles.enc"
$hasEncryptedProfiles = Test-Path -LiteralPath $profileStore -PathType Leaf
$hasModel = Test-Path -LiteralPath $ModelConfig -PathType Leaf
if (-not $hasModel -and -not $hasEncryptedProfiles -and -not $AllowNoModel) {
    throw "缺少模型配置：既没有找到 $ModelConfig，也没有找到加密模型配置 $profileStore。首次使用请创建 config/model.json；配置保存到创作界面后，可由加密配置启动。仅开发事件模式可传入 -AllowNoModel。"
}
if ($BotMode -eq "astrbot") {
    if ($ConnectorConfig -and -not (Test-Path -LiteralPath $ConnectorConfig -PathType Leaf)) {
        throw "找不到 AstrBot 连接配置：$ConnectorConfig"
    }
    if (-not $ConnectorConfig -and -not $BotToken -and -not $env:VD_CONNECTOR_TOKEN) {
        throw "AstrBot 模式需要 -ConnectorConfig、-BotToken 或 VD_CONNECTOR_TOKEN。"
    }
}

$arguments = @("-m", "virtual_domain_server.server", "--host", $BindHost, "--port", "$Port", "--db", $databasePath, "--bot-mode", $BotMode)
if ($hasModel) {
    $arguments += @("--model-config", [System.IO.Path]::GetFullPath($ModelConfig))
    if ($ModelId) { $arguments += @("--model-id", $ModelId) }
} elseif (-not $hasEncryptedProfiles) {
    Write-Warning "将以无模型事件模式启动，不能生成世界剧情或 Bot 回复。"
}
if ($BotToken) { $arguments += @("--bot-token", $BotToken) }
if ($ConnectorConfig) { $arguments += @("--connector-config", [System.IO.Path]::GetFullPath($ConnectorConfig)) }
if ($TlsCertFile) { $arguments += @("--tls-certfile", [System.IO.Path]::GetFullPath($TlsCertFile)) }
if ($TlsKeyFile) { $arguments += @("--tls-keyfile", [System.IO.Path]::GetFullPath($TlsKeyFile)) }
if ($TlsClientCaFile) { $arguments += @("--tls-client-ca", [System.IO.Path]::GetFullPath($TlsClientCaFile)) }
if ($TlsClientCrlFile) { $arguments += @("--tls-client-crl", [System.IO.Path]::GetFullPath($TlsClientCrlFile)) }

Set-Location -LiteralPath $serverRoot
$scheme = if ($TlsCertFile) { "https" } else { "http" }
Write-Host ("虚拟域服务端启动中：{0}://{1}:{2}（Python {3}，Bot 模式：{4}）" -f $scheme, $BindHost, $Port, $pythonVersion, $BotMode) -ForegroundColor Cyan
Write-Host "SQLite 数据库：$databasePath"
Write-Host "按 Ctrl+C 停止服务。首次启动时请留意管理员初始化码与创作配对码。" -ForegroundColor Yellow
& $python @pythonPrefix @arguments
exit $LASTEXITCODE
