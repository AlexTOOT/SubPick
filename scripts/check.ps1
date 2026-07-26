. "$PSScriptRoot\common.ps1"

$root = Get-ProjectRoot
$uv = Get-UvExecutable
Set-Location -LiteralPath $root

Invoke-Checked $uv @("sync", "--frozen", "--extra", "dev")
Invoke-Checked $uv @("run", "--frozen", "--extra", "dev", "ruff", "check", "src", "tests")

& "$PSScriptRoot\test.ps1"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
