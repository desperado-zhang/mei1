param(
    [string]$ProjectDir = "",
    [string]$TaskSpace = "",
    [int]$StartPage = 0,
    [int]$EndPage = 0,
    [int]$WindowPages = 0,
    [int]$DetailPerPage = 0,
    [int]$TimeoutSeconds = 0,
    [string]$CondaEnv = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\mei1_env.ps1"

$root = Enter-Mei1Project -ProjectDir $ProjectDir
Enable-Mei1CondaEnv -EnvName $CondaEnv

$resolvedTaskSpace = Get-Mei1Setting -Value $TaskSpace -EnvName "MEI1_EGO_TASK_SPACE" -Default "35"
$resolvedStartPage = Get-Mei1IntSetting -Value $StartPage -EnvName "MEI1_FULL_START_PAGE" -Default 1
$resolvedEndPage = Get-Mei1IntSetting -Value $EndPage -EnvName "MEI1_FULL_END_PAGE" -Default 126
$resolvedWindowPages = Get-Mei1IntSetting -Value $WindowPages -EnvName "MEI1_FULL_WINDOW_PAGES" -Default 3
$resolvedDetailPerPage = Get-Mei1IntSetting -Value $DetailPerPage -EnvName "MEI1_FULL_DETAIL_PER_PAGE" -Default 2
$resolvedTimeout = Get-Mei1IntSetting -Value $TimeoutSeconds -EnvName "MEI1_EGO_TIMEOUT" -Default 240

Ensure-Mei1LogDir -ProjectDir $root | Out-Null

$fullArgs = @(
    "crawl-ego-batch",
    "--task-space", $resolvedTaskSpace,
    "--start-page", [string]$resolvedStartPage,
    "--end-page", [string]$resolvedEndPage,
    "--window-pages", [string]$resolvedWindowPages,
    "--detail-per-page", [string]$resolvedDetailPerPage,
    "--timeout", [string]$resolvedTimeout
)
Invoke-Mei1Native -Command "mei1-crawler" -Arguments $fullArgs

Invoke-Mei1Native -Command "mei1-crawler" -Arguments @("rebuild-sync-state")
