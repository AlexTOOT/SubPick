. "$PSScriptRoot\common.ps1"

$root = Get-ProjectRoot
$uv = Get-UvExecutable
Set-Location -LiteralPath $root

Invoke-Checked $uv @("python", "install", "3.12")
Invoke-Checked $uv @("sync", "--frozen", "--extra", "dev")

& $uv run --frozen --extra dev python --version
Write-Host "Environment ready: $root\.venv"
