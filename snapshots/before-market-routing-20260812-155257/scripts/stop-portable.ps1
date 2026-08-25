param(
  [string]$RuntimeRoot = ""
)

$ErrorActionPreference = "Stop"

$resolvedRuntimeRoot = if ($RuntimeRoot) {
  [IO.Path]::GetFullPath($RuntimeRoot)
}
elseif ($env:LOCALAPPDATA) {
  Join-Path $env:LOCALAPPDATA "TikTokGMV"
}
else {
  Join-Path (Split-Path -Parent $PSScriptRoot) "storage"
}
$runRoot = Join-Path $resolvedRuntimeRoot "runtime"

function Stop-ProcessTree([int]$RootProcessId) {
  $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
  $pendingParents = @($RootProcessId)
  $descendants = @()
  while ($pendingParents.Count -gt 0) {
    $parentId = $pendingParents[0]
    $pendingParents = @($pendingParents | Select-Object -Skip 1)
    $children = @(
      $allProcesses |
        Where-Object { $_.ParentProcessId -eq $parentId } |
        Select-Object -ExpandProperty ProcessId
    )
    foreach ($childId in $children) {
      if ($descendants -notcontains $childId) {
        $descendants += $childId
        $pendingParents += $childId
      }
    }
  }
  [array]::Reverse($descendants)
  foreach ($processId in @($descendants) + @($RootProcessId)) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
  }
}

foreach ($name in @("web", "worker")) {
  $pidPath = Join-Path $runRoot "$name.pid"
  if (-not (Test-Path -LiteralPath $pidPath)) {
    continue
  }
  $storedPid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue |
    Select-Object -First 1
  $parsedPid = 0
  if ([int]::TryParse([string]$storedPid, [ref]$parsedPid)) {
    Stop-ProcessTree $parsedPid
  }
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

Write-Host "The GMV site has been stopped."
Start-Sleep -Seconds 2
