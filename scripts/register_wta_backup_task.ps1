# Tâche planifiée Windows : rappel backup archive WTA (déclenche backup distant prod).
param(
    [string]$Time = "03:00",
    [string]$TaskName = "BettingHUD-WTA-Archive-Backup",
    [string]$SshHost = "bettinghud",
    [int]$Retain = 8
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "scripts\backup_wta_sackmann_archive.py"
$PwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
if ($PwshCmd) {
    $Pwsh = $PwshCmd.Source
} else {
    $Pwsh = (Get-Command powershell -ErrorAction SilentlyContinue).Source
}
if (-not (Test-Path $Script)) {
    Write-Error "Script introuvable: $Script"
}

$Argument = "-NoProfile -ExecutionPolicy Bypass -Command `"& python `"$Script`" --remote $SshHost --retain $Retain`""

$Action = New-ScheduledTaskAction `
    -Execute $Pwsh `
    -Argument $Argument `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $Time

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host "Tâche planifiée : $TaskName"
Write-Host "  Heure    : $Time (chaque dimanche)"
Write-Host "  Cible    : backup distant sur $SshHost"
Write-Host "  Rétention: $Retain archives"
Write-Host ""
Write-Host "Test manuel :"
Write-Host "  python `"$Script`" --remote $SshHost --retain $Retain"
Write-Host ""
Write-Host "Copie hors-site (depuis PC) :"
Write-Host "  scp -r ${SshHost}:/opt/bettinghud/data/backups/wta_sackmann/ backups\wta_sackmann_offsite\"
