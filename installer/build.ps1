[CmdletBinding()]
param(
    [string]$Configuration = "Release",
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
$here = (Resolve-Path $PSScriptRoot).Path
$output = [IO.Path]::GetFullPath((Join-Path $here $OutputDirectory))

dotnet publish (Join-Path $here "VideoEditInstaller.csproj") `
    --configuration $Configuration `
    --runtime win-x64 `
    --self-contained true `
    --output $output `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:DebugType=None

$published = Join-Path $output "VideoEditInstaller.exe"
if (-not (Test-Path -LiteralPath $published -PathType Leaf)) {
    throw "Installer was not produced at $published"
}

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $published).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $output "VideoEditInstaller.sha256") -Encoding ascii -Value "$hash  VideoEditInstaller.exe"
Write-Host "Built $published"
Write-Host "SHA-256 $hash"
