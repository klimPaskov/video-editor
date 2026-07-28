[CmdletBinding()]
param(
    [ValidateSet("tiny", "tiny.en", "base", "base.en", "small", "small.en")]
    [string]$Model = "small",
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"

$models = @{
    "tiny" = @{ file = "tiny.pt"; sha256 = "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9" }
    "tiny.en" = @{ file = "tiny.en.pt"; sha256 = "d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03" }
    "base" = @{ file = "base.pt"; sha256 = "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e" }
    "base.en" = @{ file = "base.en.pt"; sha256 = "25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead" }
    "small" = @{ file = "small.pt"; sha256 = "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794" }
    "small.en" = @{ file = "small.en.pt"; sha256 = "f953ad0fd29cacd07d5a9eda5624af0f6bcf2258be67c92b79389873d91e0872" }
}

$spec = $models[$Model]
$resolvedDestination = [System.IO.Path]::GetFullPath($Destination)
$destinationDirectory = [System.IO.Path]::GetDirectoryName($resolvedDestination)
if ([string]::IsNullOrWhiteSpace($destinationDirectory)) {
    throw "Destination must include a writable directory"
}
New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null

$expectedHash = $spec.sha256
$url = "https://openaipublic.azureedge.net/main/whisper/models/$expectedHash/$($spec.file)"
if (Test-Path -LiteralPath $resolvedDestination -PathType Leaf) {
    $existingHash = (Get-FileHash -LiteralPath $resolvedDestination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($existingHash -ne $expectedHash) {
        throw "Existing model hash does not match the pinned $Model model: $existingHash"
    }
    [PSCustomObject]@{ status = "reused"; model = $Model; path = $resolvedDestination; sha256 = $existingHash; source = $url } | ConvertTo-Json -Compress
    exit 0
}

$staged = "$resolvedDestination.download-$([Guid]::NewGuid().ToString('N'))"
try {
    $curl = Get-Command -Name "curl.exe" -ErrorAction Stop
    & $curl.Source `
        --fail `
        --location `
        --proto "=https" `
        --tlsv1.2 `
        --retry 2 `
        --connect-timeout 30 `
        --max-time 1800 `
        --no-progress-meter `
        --output $staged `
        $url
    if ($LASTEXITCODE -ne 0) {
        throw "curl.exe failed while fetching the pinned $Model model (exit code $LASTEXITCODE)"
    }
    $downloadedHash = (Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($downloadedHash -ne $expectedHash) {
        throw "Downloaded model hash does not match the pinned $Model model: $downloadedHash"
    }
    Move-Item -LiteralPath $staged -Destination $resolvedDestination
    [PSCustomObject]@{ status = "downloaded"; model = $Model; path = $resolvedDestination; sha256 = $downloadedHash; source = $url } | ConvertTo-Json -Compress
}
finally {
    if (Test-Path -LiteralPath $staged -PathType Leaf) {
        Remove-Item -LiteralPath $staged -Force
    }
}
