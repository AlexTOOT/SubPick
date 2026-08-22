param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$RemotePath,
    [Parameter(Mandatory = $true)][string]$MediaPath,
    [string]$IdentityFile = "",
    [int]$Port = 22,
    [string]$ComposeService = "subpick",
    [string]$HealthUrl = "http://127.0.0.1:19035/api/v1/health"
)

. "$PSScriptRoot\common.ps1"

foreach ($value in @($HostName, $User, $RemotePath, $MediaPath, $ComposeService, $HealthUrl)) {
    if ($value -notmatch "^[A-Za-z0-9_./:-]+$") {
        throw "Unsafe deployment parameter: $value"
    }
}

$root = Get-ProjectRoot
$git = Get-GitExecutable
$ssh = (Get-Command ssh -ErrorAction Stop).Source
$scp = (Get-Command scp -ErrorAction Stop).Source
$gitMode = if (Test-Path -LiteralPath (Join-Path $root ".codex-git")) {
    @("--git-dir=.codex-git", "--work-tree=.")
} else {
    @()
}
Set-Location -LiteralPath $root

$status = & $git @gitMode status --porcelain
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read Git status."
}
if ($status) {
    throw "The working tree is not clean. Commit the deployment snapshot first."
}

$commit = (& $git @gitMode rev-parse --short=7 HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $commit -notmatch "^[0-9a-f]{7}$") {
    throw "Unable to resolve the deployment commit."
}

$temporaryDirectory = Join-Path $root ".tmp"
New-Item -ItemType Directory -Force -Path $temporaryDirectory | Out-Null
$archive = Join-Path $temporaryDirectory "subtitle-sidecar-$commit.tar"
Invoke-Checked $git ($gitMode + @("archive", "--format=tar", "--output=$archive", "HEAD"))

$sshArguments = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", "$Port")
$scpArguments = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-P", "$Port")
if ($IdentityFile) {
    $resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
    $sshArguments += @("-i", $resolvedIdentity)
    $scpArguments += @("-i", $resolvedIdentity)
}
$target = "$User@$HostName"
$remoteArchive = "/tmp/subtitle-sidecar-$commit.tar"

Invoke-Checked $scp ($scpArguments + @($archive, "${target}:$remoteArchive"))

$remoteScript = @'
set -eu
ROOT="__ROOT__"
STAGE="/tmp/subtitle-sidecar-stage-__COMMIT__"
ARCHIVE="/tmp/subtitle-sidecar-__COMMIT__.tar"
test "$(readlink -f "$ROOT")" = "$ROOT"
test ! -e "$STAGE"
mkdir -p "$STAGE"
tar -xf "$ARCHIVE" -C "$STAGE"
test -f "$STAGE/uv.lock"
test -f "$STAGE/Dockerfile"
test -f "$ROOT/compose.yaml"
docker build -t "ghcr.io/alextoot/subpick:__COMMIT__" "$STAGE"

ROLLBACK_IMAGE="ghcr.io/alextoot/subpick:rollback-__COMMIT__"
HAS_ROLLBACK=0
if docker image inspect ghcr.io/alextoot/subpick:latest >/dev/null 2>&1; then
    docker tag ghcr.io/alextoot/subpick:latest "$ROLLBACK_IMAGE"
    HAS_ROLLBACK=1
fi

rollback_deployment() {
    if [ "$HAS_ROLLBACK" -ne 1 ]; then
        return
    fi
    echo "Deployment health check failed; restoring the previous image." >&2
    docker tag "$ROLLBACK_IMAGE" ghcr.io/alextoot/subpick:latest
    cd "$ROOT"
    docker compose up -d --no-build --force-recreate __SERVICE__
    for rollback_attempt in $(seq 1 15); do
        if curl -fsS __HEALTH__ >/dev/null 2>&1; then
            echo "Previous image restored successfully." >&2
            return
        fi
        sleep 2
    done
    echo "Previous image was restored but did not become healthy." >&2
}

docker tag "ghcr.io/alextoot/subpick:__COMMIT__" ghcr.io/alextoot/subpick:latest
cd "$ROOT"
if ! docker compose up -d --no-build --force-recreate __SERVICE__; then
    rollback_deployment
    exit 1
fi
for attempt in $(seq 1 30); do
    if curl -fsS __HEALTH__ >/dev/null 2>&1; then
        docker compose ps
        if [ "$HAS_ROLLBACK" -eq 1 ]; then
            docker image rm "$ROLLBACK_IMAGE" >/dev/null 2>&1 || true
        fi
        case "$STAGE" in
            /tmp/subtitle-sidecar-stage-[0-9a-f]*) rm -rf -- "$STAGE" ;;
            *) exit 1 ;;
        esac
        rm -f -- "$ARCHIVE"
        exit 0
    fi
    sleep 2
done
docker logs --tail 120 __SERVICE__
rollback_deployment
exit 1
'@
$remoteScript = $remoteScript.Replace("__ROOT__", $RemotePath)
$remoteScript = $remoteScript.Replace("__COMMIT__", $commit)
$remoteScript = $remoteScript.Replace("__SERVICE__", $ComposeService)
$remoteScript = $remoteScript.Replace("__HEALTH__", $HealthUrl)
$remoteScript = $remoteScript.Replace("`r", "")
$remoteScript | & $ssh @sshArguments $target "bash -s"
if ($LASTEXITCODE -ne 0) {
    throw "Remote deployment script exited with code $LASTEXITCODE"
}
Write-Host "Deployed commit $commit to ${target}:$RemotePath"
