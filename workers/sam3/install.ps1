[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$workerRoot = (Resolve-Path $PSScriptRoot).Path
$environmentPath = if ($env:SAM3_ENV_DIR) {
    [System.IO.Path]::GetFullPath($env:SAM3_ENV_DIR)
} else {
    Join-Path $workerRoot ".venv"
}
$upstreamPath = if ($env:SAM3_UPSTREAM_DIR) {
    [System.IO.Path]::GetFullPath($env:SAM3_UPSTREAM_DIR)
} else {
    Join-Path $workerRoot "upstream"
}
$upstreamRef = $env:SAM3_REF

if (-not $upstreamRef -or $upstreamRef -notmatch '^[0-9a-fA-F]{40}$') {
    throw "Set SAM3_REF to an operator-approved 40-character upstream commit. Tags and main are not accepted."
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required to create the isolated SAM 3.1 Python 3.12 environment."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required to obtain the operator-approved upstream revision."
}
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    throw "nvidia-smi is unavailable; confirm the target CUDA GPU before continuing."
}
& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "nvidia-smi could not verify a target CUDA GPU; installation is blocked."
}

uv python install 3.12
uv venv --python 3.12 --seed $environmentPath

if (-not (Test-Path -LiteralPath (Join-Path $upstreamPath ".git"))) {
    git clone https://github.com/facebookresearch/sam3.git $upstreamPath
}
git -C $upstreamPath fetch --all --tags --prune
git -C $upstreamPath checkout --detach $upstreamRef
$resolvedCommit = (git -C $upstreamPath rev-parse HEAD).Trim()
if ($resolvedCommit -ne $upstreamRef.ToLowerInvariant()) {
    throw "Upstream checkout did not resolve to the requested immutable commit."
}

$pythonPath = Join-Path $environmentPath "Scripts\python.exe"
& $pythonPath -m pip install --upgrade pip
& $pythonPath -m pip install -e $upstreamPath

Write-Output "SAM 3 worker environment prepared at: $environmentPath"
Write-Output "Pinned upstream commit: $resolvedCommit"
Write-Output "Manual steps still required:"
Write-Output "  1. Confirm installed Python, PyTorch, CUDA, and driver compatibility."
Write-Output "  2. Request and accept official checkpoint access."
Write-Output "  3. Record the checkpoint identity and SHA-256 after an operator-controlled download."
Write-Output "  4. Run only a bounded licensed smoke test and review the masks."
