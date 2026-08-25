param(
  [string]$OutputDirectory = "",
  [string]$PackageName = "TikTok-GMV-Portable"
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
  Write-Host $Message -ForegroundColor Cyan
}

function Assert-ChildPath([string]$ParentPath, [string]$ChildPath) {
  $parentFull = [IO.Path]::GetFullPath($ParentPath).TrimEnd('\') + '\'
  $childFull = [IO.Path]::GetFullPath($ChildPath)
  if (-not $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe output path: $childFull"
  }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceRoot = Split-Path -Parent $scriptDir
$resolvedOutput = if ($OutputDirectory) {
  [IO.Path]::GetFullPath($OutputDirectory)
}
else {
  Join-Path $sourceRoot "dist"
}
$packageRoot = Join-Path $resolvedOutput $PackageName
$zipPath = Join-Path $resolvedOutput "$PackageName.zip"

Assert-ChildPath $resolvedOutput $packageRoot
Assert-ChildPath $resolvedOutput $zipPath

$required = @(
  "runtime\node.exe",
  "worker\gmv-worker.exe",
  "worker\_internal",
  "web\server.js",
  "web\.next",
  "web\node_modules",
  "web\public\invitations.html",
  "web\public\invitation-acceptor.js",
  "scripts\launch-portable.ps1",
  "scripts\stop-portable.ps1",
  "START_TIKTOK_AUTOMATION.bat",
  "STOP_TIKTOK_AUTOMATION.bat"
)
$missing = @(
  $required | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $sourceRoot $_))
  }
)
if ($missing.Count -gt 0) {
  throw "Portable build inputs are missing: $($missing -join ', ')"
}

Write-Step "[1/4] Preparing a clean portable folder..."
New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null
if (Test-Path -LiteralPath $packageRoot) {
  Remove-Item -LiteralPath $packageRoot -Recurse -Force
}
if (Test-Path -LiteralPath $zipPath) {
  Remove-Item -LiteralPath $zipPath -Force
}
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

Write-Step "[2/4] Copying the bundled runtimes and invitation lookup site..."
foreach ($directory in @("runtime", "web")) {
  Copy-Item -LiteralPath (Join-Path $sourceRoot $directory) -Destination $packageRoot -Recurse -Force
}

$workerDestination = Join-Path $packageRoot "worker"
New-Item -ItemType Directory -Path $workerDestination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceRoot "worker\gmv-worker.exe") -Destination $workerDestination -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot "worker\_internal") -Destination $workerDestination -Recurse -Force

$scriptsDestination = Join-Path $packageRoot "scripts"
New-Item -ItemType Directory -Path $scriptsDestination -Force | Out-Null
foreach ($scriptName in @("launch-portable.ps1", "stop-portable.ps1")) {
  Copy-Item -LiteralPath (Join-Path $sourceRoot "scripts\$scriptName") -Destination $scriptsDestination -Force
}
foreach ($fileName in @(
  "START_TIKTOK_AUTOMATION.bat",
  "STOP_TIKTOK_AUTOMATION.bat",
  "USER_GUIDE_KO.txt",
  "INVITATION_ACCEPTOR_README_KO.md"
)) {
  $sourcePath = Join-Path $sourceRoot $fileName
  if (Test-Path -LiteralPath $sourcePath) {
    Copy-Item -LiteralPath $sourcePath -Destination $packageRoot -Force
  }
}

Write-Step "[3/4] Verifying that no external Python or npm installation is required..."
$packagedRequired = @(
  "runtime\node.exe",
  "worker\gmv-worker.exe",
  "worker\_internal\python313.dll",
  "worker\_internal\playwright\driver\node.exe",
  "web\server.js",
  "web\.next\BUILD_ID",
  "web\node_modules\next\package.json",
  "web\public\invitations.html",
  "web\public\invitation-acceptor.js"
)
$packagedMissing = @(
  $packagedRequired | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $packageRoot $_))
  }
)
if ($packagedMissing.Count -gt 0) {
  throw "Portable verification failed. Missing: $($packagedMissing -join ', ')"
}

$manifest = [ordered]@{
  name = $PackageName
  format = "windows-x64-portable"
  created_at = (Get-Date).ToUniversalTime().ToString("o")
  start = "START_TIKTOK_AUTOMATION.bat"
  features = @("gmv_lookup", "invitation_lookup")
  unified_worker = "worker/gmv-worker.exe"
  worker_port = 8000
  separate_invitation_worker = $false
  bundled_python_worker = "worker/gmv-worker.exe"
  bundled_node_runtime = "runtime/node.exe"
  python_install_required = $false
  node_or_npm_install_required = $false
  system_requirement = "Windows 10/11 x64 and Chrome or Microsoft Edge"
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $packageRoot "portable-manifest.json") -Encoding utf8

Write-Step "[4/4] Creating the deployment ZIP..."
Compress-Archive -LiteralPath $packageRoot -DestinationPath $zipPath -CompressionLevel Optimal
$hash = Get-FileHash -LiteralPath $zipPath -Algorithm SHA256
$sizeMb = [math]::Round((Get-Item -LiteralPath $zipPath).Length / 1MB, 1)

Write-Host ""
Write-Host "Portable folder: $packageRoot" -ForegroundColor Green
Write-Host "Deployment ZIP: $zipPath" -ForegroundColor Green
Write-Host "ZIP size: $sizeMb MB"
Write-Host "SHA256: $($hash.Hash)"
