param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArguments = @()
)

. "$PSScriptRoot\common.ps1"

$root = Get-ProjectRoot
$uv = Get-UvExecutable
$tempRoot = Join-Path $root ".tmp"
$baseTemp = Join-Path $root ".tmp\pytest-$PID"
Set-Location -LiteralPath $root
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

$arguments = @(
    "run", "--frozen", "--extra", "dev", "python", "-m", "pytest",
    "-q", "-p", "no:cacheprovider", "--basetemp", $baseTemp
) + $PytestArguments

& $uv @arguments
$exitCode = $LASTEXITCODE
if (Test-Path -LiteralPath $baseTemp) {
    Remove-Item -LiteralPath $baseTemp -Recurse -Force -ErrorAction SilentlyContinue
}
exit $exitCode
