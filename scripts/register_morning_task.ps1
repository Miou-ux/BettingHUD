# Tâches planifiées Windows (PREPROD) : build 02:00 + publications 05:00 (heure locale).
param(
    [string]$BuildTime = "02:00",
    [string]$PublishTime = "05:00",
    [string]$BuildTaskName = "BettingHUD-Morning-Build",
    [string]$PublishTaskName = "BettingHUD-Morning-Publish"
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

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

function Register-DailyTask {
    param(
        [string]$Name,
        [string]$At,
        [string]$Arguments
    )
    $Action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument $Arguments `
        -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -Daily -At $At
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Force | Out-Null
    Write-Host "Tâche créée : $Name à $At"
}

Register-DailyTask -Name $BuildTaskName -At $BuildTime -Arguments "`"$Script`" --build-only"
Register-DailyTask -Name $PublishTaskName -At $PublishTime -Arguments "`"$Script`" --morning-publish"

Write-Host ""
Write-Host "Test manuel :"
Write-Host "  Build     : $Python `"$Script`" --build-only"
Write-Host "  Publish   : $Python `"$Script`" --morning-publish"
