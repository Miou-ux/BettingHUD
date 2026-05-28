# Crée une tâche planifiée Windows : scrape + snapshot Live à 07:00 (lun–dim).
param(
    [string]$Time = "07:00",
    [string]$TaskName = "BettingHUD-Morning-Live"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root "venv\Scripts\python.exe"
$Script = Join-Path $Root "scripts\morning_live_pipeline.py"

if (-not (Test-Path $Python)) {
    Write-Error "Python venv introuvable: $Python"
}
if (-not (Test-Path $Script)) {
    Write-Error "Script introuvable: $Script"
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Script`"" `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Daily -At $Time

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host "Tâche planifiée créée : $TaskName à $Time chaque jour."
Write-Host "Test manuel : $Python `"$Script`""
