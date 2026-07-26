Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:UV_LINK_MODE = "copy"

function Get-ProjectRoot {
    return (Split-Path -Parent $PSScriptRoot)
}

function Get-UvExecutable {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $scoopUv = Join-Path $env:USERPROFILE "scoop\shims\uv.exe"
    if (Test-Path -LiteralPath $scoopUv) {
        return $scoopUv
    }
    throw "uv was not found. Install it with: scoop install uv"
}

function Get-GitExecutable {
    $command = Get-Command git -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $scoopGit = Join-Path $env:USERPROFILE "scoop\apps\git\current\bin\git.exe"
    if (Test-Path -LiteralPath $scoopGit) {
        return $scoopGit
    }
    throw "git was not found."
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE"
    }
}
