. "$PSScriptRoot\common.ps1"

$root = Get-ProjectRoot
$uv = Get-UvExecutable
Set-Location -LiteralPath $root

Invoke-Checked $uv @("lock", "--upgrade")
Invoke-Checked $uv @("sync", "--extra", "dev")
Write-Host "Dependencies updated. Review and commit pyproject.toml and uv.lock together."
