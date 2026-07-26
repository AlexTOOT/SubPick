. "$PSScriptRoot\common.ps1"

$root = Get-ProjectRoot
$uv = Get-UvExecutable
Set-Location -LiteralPath $root

Invoke-Checked $uv @("sync", "--frozen", "--extra", "dev")
& $uv run --frozen --extra dev python -m uvicorn subtitle_sidecar.main:create_app `
    --factory --host 127.0.0.1 --port 19035 --no-access-log
exit $LASTEXITCODE
