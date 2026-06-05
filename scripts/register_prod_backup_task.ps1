# Tâche planifiée Windows : backup quotidien base PROD -> PC local (backups/prod/).
param(
    [string]$Time = "05:30",
    [string]$TaskName = "BettingHUD-Prod-DB-Backup",
    [int]$KeepDays = 30
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "scripts\backup_prod_db_to_local.ps1"
$PwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
if ($PwshCmd) {
    $Pwsh = $PwshCmd.Source
} else {
    $Pwsh = (Get-Command powershell -ErrorAction SilentlyContinue).Source
}
if (-not (Test-Path $Script)) {
    Write-Error "Script introuvable: $Script"
}

$Argument = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" -KeepDays $KeepDays"

$Action = New-ScheduledTaskAction `
    -Execute $Pwsh `
    -Argument $Argument `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Daily -At $Time

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host "Tâche planifiée : $TaskName"
Write-Host "  Heure    : $Time (chaque jour)"
Write-Host "  Cible    : $Root\backups\prod\"
Write-Host "  Rétention: $KeepDays jours"
Write-Host ""
Write-Host "Test manuel :"
Write-Host "  powershell -File `"$Script`""
