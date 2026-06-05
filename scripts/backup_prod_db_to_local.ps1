# Sauvegarde quotidienne de la base PROD (bettinghud.db) sur le PC local (PREPROD).
param(
    [string]$SshHost = "bettinghud",
    [string]$RemoteDb = "/opt/bettinghud/data/bettinghud.db",
    [string]$RemotePython = "/opt/bettinghud/venv/bin/python",
    [string]$BackupDir = "",
    [int]$KeepDays = 30,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$HelperScript = Join-Path $Root "scripts\remote_db_backup.py"
if (-not $BackupDir) {
    $BackupDir = Join-Path $Root "backups\prod"
}
$LogFile = Join-Path $BackupDir "backup.log"

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    if (-not $WhatIf) {
        New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    }
    Write-Host $line
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$remoteHelper = "/tmp/bettinghud_remote_db_backup.py"
$remoteTmp = "/tmp/bettinghud_backup_${stamp}.db"
$localName = "bettinghud_prod_${stamp}.db"
$localPath = Join-Path $BackupDir $localName

if (-not (Test-Path $HelperScript)) {
    Write-Error "Script helper introuvable: $HelperScript"
}

Write-Log "Debut backup PROD -> $localPath"

try {
    if ($WhatIf) {
        Write-Log "[whatif] scp helper + backup + download"
        exit 0
    }

    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

    Write-Log "Copie script backup sur le serveur..."
    & scp $HelperScript "${SshHost}:${remoteHelper}"
    if ($LASTEXITCODE -ne 0) { throw "scp helper failed ($LASTEXITCODE)" }

    Write-Log "Export SQLite sur le serveur..."
    & ssh $SshHost $RemotePython $remoteHelper $RemoteDb $remoteTmp
    if ($LASTEXITCODE -ne 0) { throw "remote backup failed ($LASTEXITCODE)" }

    Write-Log "Telechargement scp..."
    & scp "${SshHost}:${remoteTmp}" $localPath
    if ($LASTEXITCODE -ne 0) { throw "scp download failed ($LASTEXITCODE)" }

    $sizeMb = [math]::Round((Get-Item $localPath).Length / 1MB, 2)
    if ($sizeMb -lt 1) {
        throw "Fichier local suspect (${sizeMb} Mo)"
    }
    Write-Log "OK - ${sizeMb} Mo enregistre."

    & ssh $SshHost "rm" "-f" $remoteTmp $remoteHelper
    if ($LASTEXITCODE -ne 0) {
        Write-Log "Avertissement: nettoyage distant incomplet."
    }

    if ($KeepDays -gt 0) {
        $cutoff = (Get-Date).AddDays(-$KeepDays)
        Get-ChildItem -Path $BackupDir -Filter "bettinghud_prod_*.db" -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            ForEach-Object {
                Write-Log "Retention: suppression $($_.Name)"
                Remove-Item -LiteralPath $_.FullName -Force
            }
    }

    $count = @(Get-ChildItem -Path $BackupDir -Filter "bettinghud_prod_*.db" -File).Count
    Write-Log "Fin - $count fichier(s) dans $BackupDir"
}
catch {
    Write-Log "ERREUR: $($_.Exception.Message)"
    & ssh $SshHost "rm" "-f" $remoteTmp $remoteHelper 2>$null
    exit 1
}
