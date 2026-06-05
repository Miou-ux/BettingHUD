# Tache planifiee Windows : archive cotes closing TE (repli si daemon arrete).
param(
    [string]$Time = "04:00",
    [string]$TaskName = "BettingHUD-Closing-Odds"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root "venv\Scripts\python.exe"
$Script = Join-Path $Root "scripts\closing_odds_archive.py"

if (-not (Test-Path $Python)) {
    Write-Error "Python venv introuvable: $Python"
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m scripts.closing_odds_archive --once" `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
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

Write-Host "Tache planifiee : $TaskName a $Time (python -m scripts.closing_odds_archive --once)"
