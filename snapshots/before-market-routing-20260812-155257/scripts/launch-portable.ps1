param(
  [switch]$SourceMode,
  [switch]$NoBrowser,
  [string]$RuntimeRoot = ""
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
  Write-Host $Message -ForegroundColor Cyan
}

function Test-Http([string]$Uri, [scriptblock]$Validator) {
  try {
    $response = Invoke-RestMethod -Uri $Uri -TimeoutSec 2
    return [bool](& $Validator $response)
  }
  catch {
    return $false
  }
}

function Test-WebSite([string]$ExpectedNodeEnv) {
  $healthReady = Test-Http "http://127.0.0.1:3000/api/health" {
    param($body)
    $body.ok -eq $true -and
      $body.worker -eq "connected" -and
      $body.web_build -eq "portable-v1" -and
      $body.node_env -eq $ExpectedNodeEnv
  }
  if (-not $healthReady) {
    return $false
  }

  try {
    $page = Invoke-WebRequest `
      -UseBasicParsing `
      -Uri "http://127.0.0.1:3000/" `
      -TimeoutSec 5
    return $page.StatusCode -eq 200
  }
  catch {
    return $false
  }
}

function Stop-TrackedProcess([string]$PidPath) {
  if (-not (Test-Path -LiteralPath $PidPath)) {
    return
  }

  $storedPid = Get-Content -LiteralPath $PidPath -ErrorAction SilentlyContinue |
    Select-Object -First 1
  $parsedPid = 0
  if ([int]::TryParse([string]$storedPid, [ref]$parsedPid)) {
    # Next's development CLI creates a child server process. Stop descendants first so a
    # stale child cannot keep port 3000 and continue serving a build-error screen.
    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $pendingParents = @($parsedPid)
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
    foreach ($processId in @($descendants) + @($parsedPid)) {
      Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
  }
  Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
}

function Start-HiddenProcess(
  [string]$FilePath,
  [string[]]$ArgumentList,
  [string]$WorkingDirectory,
  [string]$PidPath,
  [string]$LogPrefix
) {
  $stdoutPath = "$LogPrefix.out.log"
  $stderrPath = "$LogPrefix.error.log"
  $startParameters = @{
    FilePath = $FilePath
    WorkingDirectory = $WorkingDirectory
    WindowStyle = "Hidden"
    RedirectStandardOutput = $stdoutPath
    RedirectStandardError = $stderrPath
    PassThru = $true
  }
  if ($ArgumentList.Count -gt 0) {
    # Windows PowerShell joins ArgumentList values into one command line. Preserve paths that
    # contain spaces by adding the quotes that Start-Process otherwise strips.
    $startParameters.ArgumentList = @(
      $ArgumentList | ForEach-Object {
        if ($_ -match '[\s"]') {
          '"' + ($_ -replace '"', '\"') + '"'
        }
        else {
          $_
        }
      }
    )
  }
  $process = Start-Process @startParameters
  Set-Content -LiteralPath $PidPath -Value $process.Id -Encoding ascii
  return $process
}

try {
  $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
  $root = Split-Path -Parent $scriptDir
  $resolvedRuntimeRoot = if ($RuntimeRoot) {
    [IO.Path]::GetFullPath($RuntimeRoot)
  }
  elseif ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "TikTokGMV"
  }
  else {
    Join-Path $root "storage"
  }
  $profileRoot = Join-Path $resolvedRuntimeRoot "profiles"
  $jobRoot = Join-Path $resolvedRuntimeRoot "jobs"
  $runRoot = Join-Path $resolvedRuntimeRoot "runtime"
  $logRoot = Join-Path $resolvedRuntimeRoot "logs"

  foreach ($path in @($profileRoot, $jobRoot, $runRoot, $logRoot)) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
  }

  # Preserve login/history when upgrading from an older source-folder launcher.
  $legacyProfiles = Join-Path $root "worker\storage\profiles"
  $legacyJobs = Join-Path $root "worker\storage\jobs"
  if ((-not (Test-Path -LiteralPath (Join-Path $profileRoot "DEFAULT"))) -and
      (Test-Path -LiteralPath (Join-Path $legacyProfiles "DEFAULT"))) {
    Copy-Item -LiteralPath $legacyProfiles -Destination $profileRoot -Recurse -Force
  }
  if ((-not (Get-ChildItem -LiteralPath $jobRoot -Force -ErrorAction SilentlyContinue)) -and
      (Test-Path -LiteralPath $legacyJobs)) {
    Copy-Item -Path (Join-Path $legacyJobs "*") -Destination $jobRoot -Recurse -Force
  }

  $env:GMV_PROFILE_ROOT = $profileRoot
  $env:GMV_STORAGE_ROOT = $jobRoot
  $env:GMV_HOST = "127.0.0.1"
  $env:GMV_PORT = "8000"
  # One GMV job owns one TikTok page/window. Separate jobs still run concurrently because each
  # job receives an independent disposable browser profile in Worker v10.
  $env:GMV_DEFAULT_CONCURRENCY = "1"
  $env:GMV_MAX_CONCURRENCY = "1"
  $env:WORKER_BASE_URL = "http://127.0.0.1:8000"
  $env:HOSTNAME = "127.0.0.1"
  $env:PORT = "3000"

  $workerPidPath = Join-Path $runRoot "worker.pid"
  $webPidPath = Join-Path $runRoot "web.pid"
  $workerHealth = "http://127.0.0.1:8000/health"
  # Jobs now run from disposable per-job profile snapshots, so multiple site tabs can
  # start independent lookups without sharing the persistent browser profile lock.
  $buildId = "job-profile-clones-v10"
  $expectedNodeEnv = if ($SourceMode) { "development" } else { "production" }

  Write-Step "[1/3] Checking the GMV automation engine..."
  $workerReady = Test-Http $workerHealth {
    param($body)
    $body.ok -eq $true -and $body.build -eq $buildId
  }

  if (-not $workerReady) {
    Stop-TrackedProcess $workerPidPath

    if ($SourceMode) {
      $workerDir = Join-Path $root "worker"
      $workerExe = Join-Path $workerDir ".venv\Scripts\python.exe"
      $workerArgs = @("-m", "gmv.worker_main")
    }
    else {
      $workerDir = Join-Path $root "worker"
      $workerExe = Join-Path $workerDir "gmv-worker.exe"
      $workerArgs = @()
    }

    if (-not (Test-Path -LiteralPath $workerExe)) {
      throw "The GMV automation engine was not found: $workerExe"
    }
    Start-HiddenProcess `
      -FilePath $workerExe `
      -ArgumentList $workerArgs `
      -WorkingDirectory $workerDir `
      -PidPath $workerPidPath `
      -LogPrefix (Join-Path $logRoot "worker") | Out-Null

    $workerReady = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
      Start-Sleep -Milliseconds 500
      if (Test-Http $workerHealth {
        param($body)
        $body.ok -eq $true -and $body.build -eq $buildId
      }) {
        $workerReady = $true
        break
      }
    }
    if (-not $workerReady) {
      throw "The GMV automation engine did not start. Logs: $logRoot"
    }
  }

  Write-Step "[2/3] Starting the GMV site..."
  $webReady = Test-WebSite $expectedNodeEnv

  if (-not $webReady) {
    Stop-TrackedProcess $webPidPath

    if ($SourceMode) {
      $env:NODE_ENV = "development"
      $webDir = Join-Path $root "apps\web"
      $nodeCandidates = @(
        (Join-Path $webDir "node.exe"),
        (Join-Path $root "runtime\node.exe"),
        (Join-Path $root "worker\.venv\Lib\site-packages\playwright\driver\node.exe")
      )
      $nextCli = Join-Path $webDir "node_modules\next\dist\bin\next"
      $webExe = $nodeCandidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
      if (-not $webExe) {
        $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
        if ($nodeCommand) {
          $webExe = $nodeCommand.Source
        }
      }
      $webArgs = @($nextCli, "dev")
    }
    else {
      $env:NODE_ENV = "production"
      $webDir = Join-Path $root "web"
      $webExe = Join-Path $root "runtime\node.exe"
      $webArgs = @((Join-Path $webDir "server.js"))
    }

    if (-not $webExe -or -not (Test-Path -LiteralPath $webExe)) {
      throw "The bundled web runtime was not found."
    }
    Start-HiddenProcess `
      -FilePath $webExe `
      -ArgumentList $webArgs `
      -WorkingDirectory $webDir `
      -PidPath $webPidPath `
      -LogPrefix (Join-Path $logRoot "web") | Out-Null

    $webReady = $false
    for ($attempt = 0; $attempt -lt 120; $attempt++) {
      Start-Sleep -Milliseconds 500
      if (Test-WebSite $expectedNodeEnv) {
        $webReady = $true
        break
      }
    }
    if (-not $webReady) {
      throw "The GMV site did not start. Logs: $logRoot"
    }
  }

  Write-Step "[3/3] Opening the site in your browser..."
  if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:3000"
  }
  Write-Host ""
  Write-Host "Ready. You may close this window." -ForegroundColor Green
  Start-Sleep -Seconds 2
  exit 0
}
catch {
  Write-Host ""
  Write-Host "The GMV site could not be opened." -ForegroundColor Red
  Write-Host $_.Exception.Message -ForegroundColor Red
  Write-Host ""
  Write-Host "Please send this screen to the person responsible for the GMV site."
  Read-Host "Press Enter to close"
  exit 1
}
