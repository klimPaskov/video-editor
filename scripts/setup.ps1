[CmdletBinding()]
param(
    [switch]$SkipWhisper
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command -Name $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH. Install it and run setup again."
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path
Set-Location -LiteralPath $repoRoot

Require-Command "uv"
Require-Command "node"
Require-Command "npm"
Require-Command "ffmpeg"
Require-Command "ffprobe"

$nodeVersion = (& node --version).Trim()
if ($nodeVersion -notmatch '^v(?<major>\d+)') {
    throw "Could not read the Node.js version from '$nodeVersion'."
}
if ([int]$Matches.major -lt 22) {
    throw "Node.js 22 or newer is required; found $nodeVersion."
}

$uvArgs = @("sync", "--extra", "dev")
if (-not $SkipWhisper) {
    $uvArgs += @("--extra", "whisper")
}
& uv @uvArgs
if ($LASTEXITCODE -ne 0) {
    throw "uv dependency installation failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath ".env" -PathType Leaf)) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "Created .env from .env.example."
} else {
    Write-Host "Kept existing .env."
}

Push-Location -LiteralPath "remotion"
try {
    & npm ci
    if ($LASTEXITCODE -ne 0) {
        throw "Remotion dependency installation failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

Write-Host "Setup complete. Next: run 'uv run videoedit doctor --json'."
if ($SkipWhisper) {
    Write-Host "Whisper extra was skipped; run this script again without -SkipWhisper before transcribing."
} else {
    Write-Host "Whisper model download is explicit; see README.md for the hash-verified helper."
}
