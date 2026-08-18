param(
    [string]$TaskName = "Mei1IncrementalCrawler",
    [string]$ProjectDir = "",
    [string]$TaskSpace = "",
    [int]$EveryMinutes = 10,
    [string]$CondaEnv = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\mei1_env.ps1"

if ($EveryMinutes -lt 1) {
    throw "EveryMinutes must be >= 1."
}

$root = Get-Mei1ProjectDir -ProjectDir $ProjectDir
$resolvedTaskSpace = Get-Mei1Setting -Value $TaskSpace -EnvName "MEI1_EGO_TASK_SPACE" -Default "35"
$resolvedCondaEnv = Get-Mei1Setting -Value $CondaEnv -EnvName "MEI1_CONDA_ENV" -Default "mei1-crawler"
$scriptPath = Join-Path $root "scripts\windows\mei1_incremental.ps1"
if (-not (Test-Path $scriptPath)) {
    throw "Incremental script not found: $scriptPath"
}

$argumentParts = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$scriptPath`"",
    "-ProjectDir", "`"$root`"",
    "-TaskSpace", "`"$resolvedTaskSpace`"",
    "-CondaEnv", "`"$resolvedCondaEnv`""
)
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ($argumentParts -join " ") `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel LeastPrivilege

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 8)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Run Mei1 sampled incremental crawler every $EveryMinutes minutes." `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "ProjectDir: $root"
Write-Host "TaskSpace: $resolvedTaskSpace"
Write-Host "Interval: $EveryMinutes minutes"
