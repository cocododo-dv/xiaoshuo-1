# React mainline contract E2E lane (run-smokes.mjs).
# Spins up an ISOLATED seeded backend (its own e2e sqlite, never touching the dev
# novel_system.db) plus the React app, runs smoke-phase2..7 + ai-settings (run-smokes
# reseeds between suites), then tears the whole process tree down. This is the default
# release-lane regression gate for the production frontend (frontend-react).
#
# Prereq: Playwright installed in frontend-react/ (run-smokes uses require("playwright") and
# its cached browsers). Manual run:
#   powershell -ExecutionPolicy Bypass -File scripts\verify_react_e2e.ps1
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less .ps1 as the
# system ANSI codepage, so non-ASCII comments corrupt parsing.

param(
    [int]$BackendPort = 8009,
    [int]$ReactPort = 5174,
    [int]$ReadyTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }

function Test-PortBindable {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), $Port)
        $listener.Start()
        return $true
    }
    catch { return $false }
    finally { if ($null -ne $listener) { $listener.Stop() } }
}

function Test-UrlHealthy {
    param([string]$Url)
    try { return (Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 5).StatusCode -eq 200 }
    catch { return $false }
}

function Wait-Until {
    param([string]$Label, [scriptblock]$Condition, [int]$TimeoutSeconds = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return }
        Start-Sleep -Seconds 1
    }
    throw ("Timed out waiting for {0}." -f $Label)
}

function Get-DescendantProcessIds {
    param([int[]]$RootProcessIds)
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $allIds = New-Object System.Collections.Generic.HashSet[int]
    $queue = [System.Collections.Generic.Queue[int]]::new()
    foreach ($rootId in $RootProcessIds) { if ($allIds.Add($rootId)) { $queue.Enqueue($rootId) } }
    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        foreach ($child in @($processes | Where-Object { $_.ParentProcessId -eq $current })) {
            $childId = [int]$child.ProcessId
            if ($allIds.Add($childId)) { $queue.Enqueue($childId) }
        }
    }
    return @($allIds)
}

function Stop-Tree {
    param($Process)
    if (-not $Process) { return }
    try {
        $ids = Get-DescendantProcessIds -RootProcessIds @($Process.Id)
        foreach ($processId in ($ids | Sort-Object -Descending)) {
            try { Stop-Process -Id $processId -Force -ErrorAction Stop } catch {}
        }
    }
    catch {}
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"
$backendPython = Join-Path $backendDir ".venv\Scripts\python.exe"
$reactDir = Join-Path $repoRoot "frontend-react"
$runDir = Join-Path $repoRoot ".codex-run\e2e"
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

# --- Preflight ---
if (-not (Test-PortBindable -Port $BackendPort)) { throw "Backend port $BackendPort is busy; stop whatever holds it first." }
if (-not (Test-PortBindable -Port $ReactPort)) { throw "React port $ReactPort is busy; stop whatever holds it first." }
$playwrightProbe = Join-Path $reactDir "node_modules\playwright\package.json"
if (-not (Test-Path $playwrightProbe)) {
    throw "run-smokes needs Playwright installed in frontend-react/ (cd frontend-react; npm ci). Probe missing: $playwrightProbe"
}
if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) {
    throw "Locked backend Python is missing: $backendPython. Run: cd backend; uv sync --locked --extra dev"
}

# --- Isolated runtime: dedicated e2e sqlite + in-memory vector backend ---
$dbPath = Join-Path $runDir "e2e.db"
Remove-Item $dbPath -ErrorAction SilentlyContinue   # fresh DB every run
$dbUrl = "sqlite:///" + ($dbPath -replace "\\", "/")
$configSecret = "e2e-" + ([guid]::NewGuid().ToString("N"))
$backendUrl = "http://127.0.0.1:$BackendPort"
$reactUrl = "http://127.0.0.1:$ReactPort/"
$healthUrl = "$backendUrl/ready"

# Session env: the alembic migration and run-smokes' node-spawned reseed(python) both
# inherit it, so migration / backend / per-suite reseed all share the same e2e DB.
$env:PYTHONPATH = "src"
$env:NOVEL_SYSTEM_PYTHON = $backendPython
$env:NOVEL_SYSTEM_VECTOR_BACKEND = "memory"
$env:NOVEL_SYSTEM_DATABASE_URL = $dbUrl
$env:NOVEL_SYSTEM_CONFIG_SECRET = $configSecret
$env:NOVEL_SYSTEM_LLM_ENABLED = "false"
$env:NOVEL_SYSTEM_CORS_ORIGINS = $reactUrl.TrimEnd("/")

Write-Step -Message "E2E runtime DB: $dbUrl"

# --- Migrate (a fresh DB needs no legacy style-reference backup; reseed is run-smokes' job) ---
Write-Step -Message "alembic upgrade head (e2e db)"
Push-Location $backendDir
try {
    & $backendPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed for the e2e database." }
}
finally { Pop-Location }

$backendProcess = $null
$reactProcess = $null
$smokeExit = 1
try {
    # --- Start isolated backend (no --reload, so the process tree stays simple to kill) ---
    Write-Step -Message "Starting seeded backend on $backendUrl"
    $backendCommand = '$env:PYTHONPATH = ''src''; $env:NOVEL_SYSTEM_VECTOR_BACKEND = ''memory''; $env:NOVEL_SYSTEM_DATABASE_URL = ''{0}''; $env:NOVEL_SYSTEM_CONFIG_SECRET = ''{1}''; $env:NOVEL_SYSTEM_LLM_ENABLED = ''false''; $env:NOVEL_SYSTEM_CORS_ORIGINS = ''{2}''; & ''{3}'' -m uvicorn novel_system.api.app:create_app --factory --host 127.0.0.1 --port {4} --app-dir src' -f $dbUrl, $configSecret, $reactUrl.TrimEnd("/"), ($backendPython -replace "'", "''"), $BackendPort
    $backendProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $backendCommand) -WorkingDirectory $backendDir -RedirectStandardOutput "$runDir\backend.out.log" -RedirectStandardError "$runDir\backend.err.log" -PassThru

    # --- Start React (dev server; inject the e2e backend as default API base) ---
    Write-Step -Message "Starting React app on $reactUrl"
    $reactCommand = '$env:VITE_NOVEL_SYSTEM_API_BASE = ''{0}''; npm.cmd run dev -- --host 127.0.0.1 --port {1}' -f $backendUrl, $ReactPort
    $reactProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $reactCommand) -WorkingDirectory $reactDir -RedirectStandardOutput "$runDir\react.out.log" -RedirectStandardError "$runDir\react.err.log" -PassThru

    Wait-Until -Label "backend health ($healthUrl)" -Condition { Test-UrlHealthy -Url $healthUrl } -TimeoutSeconds $ReadyTimeoutSeconds
    Wait-Until -Label "react home ($reactUrl)" -Condition { Test-UrlHealthy -Url $reactUrl } -TimeoutSeconds $ReadyTimeoutSeconds

    # --- Run contract smokes from the React package that owns Playwright. ---
    Write-Step -Message "Running contract smokes (run-smokes.mjs)"
    Push-Location $reactDir
    try {
        node scripts/run-smokes.mjs $reactUrl $backendUrl
        $smokeExit = $LASTEXITCODE
    }
    finally { Pop-Location }
}
finally {
    Write-Step -Message "Tearing down e2e services"
    Stop-Tree -Process $reactProcess
    Stop-Tree -Process $backendProcess
    Start-Sleep -Seconds 1
}

if ($smokeExit -ne 0) {
    throw ("React contract E2E (run-smokes) failed with exit code {0}. Logs under {1}." -f $smokeExit, $runDir)
}
Write-Host "React mainline contract E2E passed." -ForegroundColor Green
