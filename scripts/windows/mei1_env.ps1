Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-Mei1ProjectDir {
    param([string]$ProjectDir = "")

    if ($ProjectDir) {
        return (Resolve-Path $ProjectDir).Path
    }
    if ($env:MEI1_PROJECT_DIR) {
        return (Resolve-Path $env:MEI1_PROJECT_DIR).Path
    }
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Get-Mei1Setting {
    param(
        [string]$Value = "",
        [string]$EnvName,
        [string]$Default
    )

    if ($Value) {
        return $Value
    }
    $envValue = [Environment]::GetEnvironmentVariable($EnvName)
    if ($envValue) {
        return $envValue
    }
    return $Default
}

function Get-Mei1IntSetting {
    param(
        [int]$Value = 0,
        [string]$EnvName,
        [int]$Default
    )

    if ($Value -gt 0) {
        return $Value
    }
    $envValue = [Environment]::GetEnvironmentVariable($EnvName)
    if ($envValue) {
        return [int]$envValue
    }
    return $Default
}

function Find-Mei1Conda {
    if ($env:CONDA_EXE -and (Test-Path $env:CONDA_EXE)) {
        return $env:CONDA_EXE
    }

    $command = Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "$env:USERPROFILE\miniconda3\Scripts\conda.exe",
        "$env:USERPROFILE\Miniconda3\Scripts\conda.exe",
        "$env:USERPROFILE\anaconda3\Scripts\conda.exe",
        "$env:LOCALAPPDATA\miniconda3\Scripts\conda.exe",
        "$env:LOCALAPPDATA\Miniconda3\Scripts\conda.exe",
        "C:\ProgramData\miniconda3\Scripts\conda.exe",
        "C:\ProgramData\Miniconda3\Scripts\conda.exe",
        "C:\ProgramData\Anaconda3\Scripts\conda.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    throw "Cannot find conda.exe. Set CONDA_EXE or add Miniconda to PATH."
}

function Enable-Mei1CondaEnv {
    param([string]$EnvName = "")

    $resolvedEnvName = Get-Mei1Setting -Value $EnvName -EnvName "MEI1_CONDA_ENV" -Default "mei1-crawler"
    $condaExe = Find-Mei1Conda
    (& $condaExe "shell.powershell" "hook") | Out-String | Invoke-Expression
    conda activate $resolvedEnvName
}

function Enter-Mei1Project {
    param([string]$ProjectDir = "")

    $resolvedProjectDir = Get-Mei1ProjectDir -ProjectDir $ProjectDir
    Set-Location $resolvedProjectDir
    return $resolvedProjectDir
}

function Ensure-Mei1LogDir {
    param([string]$ProjectDir)

    $logDir = Join-Path $ProjectDir "logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    return $logDir
}

function Invoke-Mei1Native {
    param(
        [string]$Command,
        [string[]]$Arguments,
        [string]$LogPath = ""
    )

    if ($LogPath) {
        & $Command @Arguments 2>&1 | Tee-Object -FilePath $LogPath -Append
    } else {
        & $Command @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}
