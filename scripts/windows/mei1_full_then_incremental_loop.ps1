param(
    [string]$ProjectDir = "",
    [string]$TaskSpace = "",
    [int]$IntervalSeconds = 0,
    [string]$CondaEnv = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\mei1_env.ps1"

$root = Enter-Mei1Project -ProjectDir $ProjectDir
$resolvedTaskSpace = Get-Mei1Setting -Value $TaskSpace -EnvName "MEI1_EGO_TASK_SPACE" -Default "35"
$resolvedInterval = Get-Mei1IntSetting -Value $IntervalSeconds -EnvName "MEI1_INCREMENTAL_INTERVAL_SECONDS" -Default 600

& "$PSScriptRoot\mei1_full.ps1" `
    -ProjectDir $root `
    -TaskSpace $resolvedTaskSpace `
    -CondaEnv $CondaEnv

while ($true) {
    Start-Sleep -Seconds $resolvedInterval
    & "$PSScriptRoot\mei1_incremental.ps1" `
        -ProjectDir $root `
        -TaskSpace $resolvedTaskSpace `
        -CondaEnv $CondaEnv
}
