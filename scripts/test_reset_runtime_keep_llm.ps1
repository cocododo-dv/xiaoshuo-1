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

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Script,
        [string[]]$ArgumentList = @()
    )

    $Script | python - @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw ("Python command failed with exit code {0}." -f $LASTEXITCODE)
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$resetScript = Join-Path $repoRoot "scripts\reset_runtime_keep_llm.ps1"
$devScript = Join-Path $repoRoot "scripts\dev.ps1"
$resetWrapper = Join-Path $repoRoot "reset-runtime-keep-llm.cmd"

Assert-True -Condition (Test-Path $resetScript) -Message "Missing reset script."
Assert-True -Condition (Test-Path $devScript) -Message "Missing dev lifecycle script."
Assert-True -Condition (Test-Path $resetWrapper) -Message "Missing one-click reset wrapper."

$devSource = Get-Content -LiteralPath $devScript -Raw
# Demo data is retired: the production launchers seed no demo works and need no skip marker / switch.
Assert-True -Condition (-not $devSource.Contains("seed_demo")) -Message "dev.ps1 must not seed retired demo data."
Assert-True -Condition (-not $devSource.Contains("skip-demo-seed")) -Message "dev.ps1 must not reference the retired seed-skip marker."

$wrapperSource = Get-Content -LiteralPath $resetWrapper -Raw
Assert-True -Condition ($wrapperSource.Contains("scripts\reset_runtime_keep_llm.ps1")) -Message "One-click wrapper must call the reset script."
Assert-True -Condition ($wrapperSource.Contains("-StopServices")) -Message "One-click wrapper must stop repo services before resetting."
Assert-True -Condition ($wrapperSource.Contains("-ExecutionPolicy Bypass")) -Message "One-click wrapper must be runnable on default Windows PowerShell policy."
Assert-True -Condition ($wrapperSource.Contains("exit /b %exit_code%")) -Message "One-click wrapper must return the reset script exit code."

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("novel-reset-test-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot | Out-Null

try {
    $backendDir = Join-Path $tempRoot "backend"
    $frontendDir = Join-Path $tempRoot "frontend"
    New-Item -ItemType Directory -Path $backendDir | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $backendDir ".pytest_cache") | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $backendDir ".vector_store") | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $tempRoot ".playwright-cli") | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $tempRoot ".codex-run") | Out-Null
    Set-Content -LiteralPath (Join-Path $tempRoot ".codex-run\backend.pid") -Value "12345"
    Set-Content -LiteralPath (Join-Path $tempRoot ".codex-run\backend.err.log") -Value "old backend log"
    New-Item -ItemType Directory -Path (Join-Path $frontendDir "dist") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $frontendDir "test-results") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $tempRoot "docs") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $tempRoot "docs\reference-learning-qa-2026-04-20.md") -Value "generated qa"
    New-Item -ItemType Directory -Path (Join-Path $tempRoot "backend\src\novel_system\__pycache__") -Force | Out-Null

    $dbPath = Join-Path $backendDir "novel_system.db"
    $setupDb = @'
import sqlite3
import sys

db = sys.argv[1]
conn = sqlite3.connect(db)
conn.executescript(
    """
    create table alembic_version (version_num text primary key);
    create table chapter_goals (chapter_id text primary key, title text);
    create table scene_cards (scene_id text primary key, chapter_id text);
    create table review_items (review_id text primary key, status text);
    create table banned_rule_clusters (banned_rule_cluster_id text primary key, status text);
    create table reference_books (book_id text primary key, title text);
    create table reference_findings (finding_id text primary key, status text);
    create table llm_calls (llm_call_id text primary key, provider text);
    create table system_config_snapshots (
        snapshot_id text primary key,
        category text not null,
        version integer not null,
        yaml_raw text not null,
        parsed_json text not null,
        validation_json text not null,
        status text not null,
        active_flag integer not null,
        created_by text,
        created_at text,
        activated_at text
    );
    create table system_secrets (
        secret_id text primary key,
        encrypted_value text not null,
        value_hint text,
        secret_type text not null default 'generic',
        metadata_json text not null default '{}',
        expires_at text,
        updated_by text,
        updated_at text
    );
    insert into alembic_version values ('20260419_0011');
    insert into chapter_goals values ('CH_KEEP_OUT', 'runtime data');
    insert into scene_cards values ('SC_KEEP_OUT', 'CH_KEEP_OUT');
    insert into review_items values ('review_keep_out', 'pending');
    insert into banned_rule_clusters values ('BAN_KEEP_OUT', 'active');
    insert into reference_books values ('refbook_generated', 'generated reference');
    insert into reference_findings values ('finding_keep_out', 'pending');
    insert into llm_calls values ('llm_generated', 'local');
    insert into system_config_snapshots values ('api_active', 'api', 1, 'llm: {}', '{"llm":{"enabled":true}}', '{"ok":true}', 'active', 1, 'tester', 'now', 'now');
    insert into system_config_snapshots values ('models_active', 'models', 1, 'nodes: {}', '{"nodes":{}}', '{"ok":true}', 'active', 1, 'tester', 'now', 'now');
    insert into system_config_snapshots values ('prompts_draft', 'prompts', 1, 'prompt: x', '{"prompt":"x"}', '{"ok":true}', 'draft', 0, 'tester', 'now', null);
    insert into system_secrets values ('llm_api_key', 'encrypted-legacy', 'sk-***', 'api_key', '{"provider_id":"legacy"}', null, 'tester', 'now');
    insert into system_secrets values ('llm_provider:local_qwen:api_key', 'encrypted-provider', 'qwen-***', 'api_key', '{"provider_id":"local_qwen"}', null, 'tester', 'now');
    insert into system_secrets values ('smtp_password', 'encrypted-smtp', 'smtp-***', 'generic', '{}', null, 'tester', 'now');
    """
)
conn.commit()
'@
    Invoke-Python -Script $setupDb -ArgumentList @($dbPath)

    & $resetScript -RepoRoot $tempRoot -DatabasePath $dbPath -SkipServiceCheck -NoVacuum | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw ("Reset script failed with exit code {0}." -f $LASTEXITCODE)
    }

    $inspectDb = @'
import json
import sqlite3
import sys

db = sys.argv[1]
conn = sqlite3.connect(db)

def scalar(sql):
    return conn.execute(sql).fetchone()[0]

payload = {
    "chapter_count": scalar("select count(*) from chapter_goals"),
    "scene_count": scalar("select count(*) from scene_cards"),
    "review_count": scalar("select count(*) from review_items"),
    "banned_count": scalar("select count(*) from banned_rule_clusters"),
    "reference_count": scalar("select count(*) from reference_books"),
    "reference_finding_count": scalar("select count(*) from reference_findings"),
    "llm_call_count": scalar("select count(*) from llm_calls"),
    "config_categories": [row[0] for row in conn.execute("select category from system_config_snapshots order by category")],
    "secret_ids": [row[0] for row in conn.execute("select secret_id from system_secrets order by secret_id")],
    "alembic_count": scalar("select count(*) from alembic_version"),
}
print(json.dumps(payload, ensure_ascii=False))
'@
    $json = $inspectDb | python - $dbPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to inspect reset database."
    }
    $state = $json | ConvertFrom-Json

    Assert-True -Condition ($state.chapter_count -eq 0) -Message "Runtime chapter rows were not cleared."
    Assert-True -Condition ($state.scene_count -eq 0) -Message "Scene rows were not cleared."
    Assert-True -Condition ($state.review_count -eq 0) -Message "Review rows were not cleared."
    Assert-True -Condition ($state.banned_count -eq 0) -Message "Knowledge rows were not cleared."
    Assert-True -Condition ($state.reference_count -eq 0) -Message "Reference rows were not cleared."
    Assert-True -Condition ($state.reference_finding_count -eq 0) -Message "Reference finding rows were not cleared."
    Assert-True -Condition ($state.llm_call_count -eq 0) -Message "LLM call evidence rows were not cleared."
    Assert-True -Condition (@($state.config_categories) -join "," -eq "api,models") -Message "Only api/models config snapshots should remain."
    Assert-True -Condition (@($state.secret_ids) -contains "llm_api_key") -Message "Legacy LLM secret was not preserved."
    Assert-True -Condition (@($state.secret_ids) -contains "llm_provider:local_qwen:api_key") -Message "Provider LLM secret was not preserved."
    Assert-True -Condition (-not (@($state.secret_ids) -contains "smtp_password")) -Message "Non-LLM secret was not cleared."
    Assert-True -Condition ($state.alembic_count -eq 1) -Message "Alembic version row should be preserved."

    # Demo seed is retired: reset no longer writes the skip marker (the production launchers never seed demo data).
    Assert-True -Condition (-not (Test-Path -LiteralPath (Join-Path $tempRoot ".codex-run\skip-demo-seed"))) -Message "Retired seed-skip marker must no longer be created."
    foreach ($relativePath in @(".playwright-cli", ".codex-run\backend.pid", ".codex-run\backend.err.log", "backend\.pytest_cache", "backend\.vector_store", "frontend\dist", "frontend\test-results", "docs\reference-learning-qa-2026-04-20.md", "backend\src\novel_system\__pycache__")) {
        $absolutePath = Join-Path $tempRoot $relativePath
        Assert-True -Condition (-not (Test-Path -LiteralPath $absolutePath)) -Message ("Generated path was not removed: {0}" -f $relativePath)
    }
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Runtime reset keep-LLM test passed." -ForegroundColor Green
