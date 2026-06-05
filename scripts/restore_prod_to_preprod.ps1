# Restore PROD data to local PREPROD (React / Streamlit tests).
# DB: latest backups/prod/bettinghud_prod_*.db (or -FetchRemote for live scp)
# Live snapshot + prematch CSV: scp from PROD server
param(
    [string]$SshHost = "bettinghud",
    [string]$RemoteRoot = "/opt/bettinghud",
    [switch]$FetchRemoteDb,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $Root "data"
$CacheDir = Join-Path $DataDir "cache"
$ScrapedDir = Join-Path $DataDir "scraped"
$PreprodBackupDir = Join-Path $Root "backups\preprod"
$ProdBackupDir = Join-Path $Root "backups\prod"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"

function Write-Step([string]$Message) {
    Write-Host "[restore] $Message"
}

New-Item -ItemType Directory -Force -Path $PreprodBackupDir, $CacheDir, $ScrapedDir | Out-Null

$localDb = Join-Path $DataDir "bettinghud.db"
if (Test-Path $localDb) {
    $bak = Join-Path $PreprodBackupDir "bettinghud_preprod_${stamp}.db"
    Write-Step "Backup local DB -> $bak"
    if (-not $WhatIf) { Copy-Item -LiteralPath $localDb -Destination $bak -Force }
}

$prodDb = $null
if ($FetchRemoteDb) {
    $prodDb = Join-Path $PreprodBackupDir "bettinghud_prod_remote_${stamp}.db"
    Write-Step "Download PROD DB from $SshHost..."
    if (-not $WhatIf) {
        & scp "${SshHost}:${RemoteRoot}/data/bettinghud.db" $prodDb
        if ($LASTEXITCODE -ne 0) { throw "scp DB failed ($LASTEXITCODE)" }
    }
} else {
    $latest = Get-ChildItem -Path $ProdBackupDir -Filter "bettinghud_prod_*.db" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latest) { throw "No PROD backup in $ProdBackupDir" }
    $prodDb = $latest.FullName
    Write-Step "PROD DB file: $($latest.Name)"
}

if (-not $WhatIf) {
    Copy-Item -LiteralPath $prodDb -Destination $localDb -Force
    $sizeMb = [math]::Round((Get-Item $localDb).Length / 1MB, 1)
    Write-Step "Active DB: data/bettinghud.db (${sizeMb} MB)"
}

$snapshotFiles = @(
    "live_matches_snapshot.joblib",
    "live_matches_snapshot.joblib.meta.json",
    "live_matches_snapshot.full.joblib",
    "live_matches_snapshot.full.joblib.meta.json",
    "live_matches_nextday.full.joblib",
    "live_matches_nextday.full.joblib.meta.json"
)
foreach ($name in $snapshotFiles) {
    $remote = "${SshHost}:${RemoteRoot}/data/cache/$name"
    $local = Join-Path $CacheDir $name
    Write-Step "Snapshot: $name"
    if (-not $WhatIf) {
        & scp $remote $local
        if ($LASTEXITCODE -ne 0) { throw "scp $name failed ($LASTEXITCODE)" }
    }
}

Write-Step "Latest prematch CSV..."
if ($WhatIf) {
    Write-Step "[whatif] skip prematch scp"
} else {
    $remoteCsv = & ssh $SshHost "ls -t ${RemoteRoot}/data/scraped/prematch_odds_*.csv 2>/dev/null | head -1"
    if ($LASTEXITCODE -ne 0 -or -not $remoteCsv) { throw "Cannot list prematch CSV on PROD" }
    $remoteCsv = $remoteCsv.Trim()
    $csvName = Split-Path $remoteCsv -Leaf
    & scp "${SshHost}:$remoteCsv" (Join-Path $ScrapedDir $csvName)
    if ($LASTEXITCODE -ne 0) { throw "scp prematch failed ($LASTEXITCODE)" }
    Write-Step "Prematch: data/scraped/$csvName"
}

$remoteUsers = "${SshHost}:${RemoteRoot}/data/web_users.json"
$localUsers = Join-Path $DataDir "web_users.json"
if (-not $WhatIf) {
    & scp $remoteUsers $localUsers 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Step "web_users.json copied from PROD"
    } else {
        Write-Step "web_users.json not on PROD - local login unchanged"
    }
}

Write-Step "Done. Restart BettingHUD-Web API and refresh React."
