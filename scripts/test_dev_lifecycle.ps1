$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Invoke-CheckedScript {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath
    )

    & $FilePath
    if ($LASTEXITCODE -ne 0) {
        throw ("Command failed with exit code {0}: {1}" -f $LASTEXITCODE, $FilePath)
    }
}

function Wait-Until {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Condition,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) {
            return
        }
        Start-Sleep -Seconds 1
    }

    throw ("Timed out waiting for {0}." -f $Label)
}

function Test-UrlHealthy {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 5
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Test-PortClosed {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    ).Count -eq 0
}

function Read-RequiredText {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    Assert-True -Condition (Test-Path $Path) -Message ("Missing expected file: {0}" -f $Path)
    return (Get-Content -Path $Path -Raw).Trim()
}

function Get-PortFromUrl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url
    )

    return ([System.Uri]$Url).Port
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$mainScript = Join-Path $repoRoot "scripts\dev.ps1"
$startWrapper = Join-Path $repoRoot "start-dev.cmd"
$stopWrapper = Join-Path $repoRoot "stop-dev.cmd"
$restartWrapper = Join-Path $repoRoot "restart-dev.cmd"
$runDir = Join-Path $repoRoot ".codex-run"
$backendPidFile = Join-Path $runDir "backend.pid"
$reactPidFile = Join-Path $runDir "frontend-react.pid"
$backendUrlFile = Join-Path $runDir "backend.url"
$reactUrlFile = Join-Path $runDir "frontend-react.url"

Assert-True -Condition (Test-Path $mainScript) -Message "Missing lifecycle script: scripts/dev.ps1"
Assert-True -Condition (Test-Path $startWrapper) -Message "Missing wrapper: start-dev.cmd"
Assert-True -Condition (Test-Path $stopWrapper) -Message "Missing wrapper: stop-dev.cmd"
Assert-True -Condition (Test-Path $restartWrapper) -Message "Missing wrapper: restart-dev.cmd"

try {
    Invoke-CheckedScript -FilePath $stopWrapper

    Invoke-CheckedScript -FilePath $startWrapper
    $backendUrl = Read-RequiredText -Path $backendUrlFile
    $reactUrl = Read-RequiredText -Path $reactUrlFile
    Wait-Until -Label "backend readiness" -Condition { Test-UrlHealthy -Url "$backendUrl/ready" } -TimeoutSeconds 90
    Wait-Until -Label "React frontend home" -Condition { Test-UrlHealthy -Url $reactUrl } -TimeoutSeconds 60
    $overview = Invoke-RestMethod -UseBasicParsing -Uri "$backendUrl/api/v1/system-config/llm" -TimeoutSec 5
    Assert-True -Condition ($overview.data.runtime.secret_configured -eq $true) -Message "Dev backend must provide NOVEL_SYSTEM_CONFIG_SECRET so local API-key providers can be saved."
    Assert-True -Condition (Test-Path $backendPidFile) -Message "Missing backend PID file after start."
    Assert-True -Condition (Test-Path $reactPidFile) -Message "Missing React frontend PID file after start."

    Invoke-CheckedScript -FilePath $restartWrapper
    $backendUrl = Read-RequiredText -Path $backendUrlFile
    $reactUrl = Read-RequiredText -Path $reactUrlFile
    Wait-Until -Label "backend after restart" -Condition { Test-UrlHealthy -Url "$backendUrl/ready" } -TimeoutSeconds 90
    Wait-Until -Label "React frontend after restart" -Condition { Test-UrlHealthy -Url $reactUrl } -TimeoutSeconds 60
    $overview = Invoke-RestMethod -UseBasicParsing -Uri "$backendUrl/api/v1/system-config/llm" -TimeoutSec 5
    Assert-True -Condition ($overview.data.runtime.secret_configured -eq $true) -Message "Restarted dev backend must keep NOVEL_SYSTEM_CONFIG_SECRET configured."
    Assert-True -Condition (Test-Path $backendPidFile) -Message "Missing backend PID file after restart."
    Assert-True -Condition (Test-Path $reactPidFile) -Message "Missing React frontend PID file after restart."

    $backendPort = Get-PortFromUrl -Url $backendUrl
    $reactPort = Get-PortFromUrl -Url $reactUrl
    Invoke-CheckedScript -FilePath $stopWrapper
    Wait-Until -Label "dev service ports to close after stop" -Condition {
        (Test-PortClosed -Port $backendPort) -and (Test-PortClosed -Port $reactPort)
    } -TimeoutSeconds 30
    Assert-True -Condition (-not (Test-Path $backendPidFile)) -Message "Backend PID file still exists after stop."
    Assert-True -Condition (-not (Test-Path $reactPidFile)) -Message "React frontend PID file still exists after stop."
}
finally {
    if (Test-Path $stopWrapper) {
        try {
            & $stopWrapper | Out-Host
        }
        catch {
        }
    }
}

Write-Host "Lifecycle smoke test passed." -ForegroundColor Green
