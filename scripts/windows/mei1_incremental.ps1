param(
    [string]$ProjectDir = "",
    [string]$TaskSpace = "",
    [int]$StartPage = 0,
    [int]$Pages = 0,
    [int]$WindowPages = 0,
    [int]$DetailBatchSize = 0,
    [int]$TimeoutSeconds = 0,
    [string]$CondaEnv = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\mei1_env.ps1"

$root = Enter-Mei1Project -ProjectDir $ProjectDir
Enable-Mei1CondaEnv -EnvName $CondaEnv

$resolvedTaskSpace = Get-Mei1Setting -Value $TaskSpace -EnvName "MEI1_EGO_TASK_SPACE" -Default "35"
$resolvedStartPage = Get-Mei1IntSetting -Value $StartPage -EnvName "MEI1_INCREMENTAL_START_PAGE" -Default 1
$resolvedPages = Get-Mei1IntSetting -Value $Pages -EnvName "MEI1_INCREMENTAL_PAGES" -Default 3
$resolvedWindowPages = Get-Mei1IntSetting -Value $WindowPages -EnvName "MEI1_INCREMENTAL_WINDOW_PAGES" -Default 3
$resolvedDetailBatchSize = Get-Mei1IntSetting -Value $DetailBatchSize -EnvName "MEI1_DETAIL_BATCH_SIZE" -Default 10
$resolvedTimeout = Get-Mei1IntSetting -Value $TimeoutSeconds -EnvName "MEI1_EGO_TIMEOUT" -Default 240

$logDir = Ensure-Mei1LogDir -ProjectDir $root
$logPath = Join-Path $logDir "incremental.log"
"[$(Get-Date -Format o)] Starting incremental crawl" | Add-Content -Path $logPath -Encoding UTF8

$incrementalArgs = @(
    "crawl-ego-incremental",
    "--task-space", $resolvedTaskSpace,
    "--start-page", [string]$resolvedStartPage,
    "--pages", [string]$resolvedPages,
    "--window-pages", [string]$resolvedWindowPages,
    "--detail-batch-size", [string]$resolvedDetailBatchSize,
    "--timeout", [string]$resolvedTimeout
)
Invoke-Mei1Native -Command "mei1-crawler" -Arguments $incrementalArgs -LogPath $logPath

"[$(Get-Date -Format o)] Incremental crawl finished" | Add-Content -Path $logPath -Encoding UTF8
