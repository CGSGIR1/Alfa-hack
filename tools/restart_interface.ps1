param([switch]$CheckOnly, [switch]$StopOnly)
$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python environment is missing. See README.md.' }
# Windows venv may redirect execution to its base Python process.
$taskBasePython = & $taskPython -c 'import sys; print(sys._base_executable)'
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect project Python.' }
$taskPort = 8765
if ($env:APP_PORT) {
    $taskPort = [int]$env:APP_PORT
} elseif (Test-Path -LiteralPath (Join-Path $taskRoot '.env')) {
    foreach ($taskLine in [System.IO.File]::ReadAllLines((Join-Path $taskRoot '.env'))) {
        if ($taskLine -match '^\s*APP_PORT\s*=\s*["'']?(\d+)["'']?\s*$') { $taskPort = [int]$Matches[1] }
    }
}
if ($taskPort -lt 1 -or $taskPort -gt 65535) { throw 'Invalid APP_PORT.' }
$taskListeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object LocalPort -eq $taskPort)
if ($taskListeners.Count) {
    $taskOwners = @($taskListeners.OwningProcess | Select-Object -Unique)
    if ($taskOwners.Count -ne 1) { throw 'Port has multiple owners; no process was stopped.' }
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskOwners[0])"
    $taskConfig = Get-Content -LiteralPath (Join-Path $taskRoot 'config.json') -Raw | ConvertFrom-Json
    $taskDatabase = [System.IO.Path]::GetFullPath((Join-Path $taskRoot $taskConfig.database))
    $taskState = Invoke-RestMethod "http://127.0.0.1:$taskPort/api/state" -TimeoutSec 10
    if (-not $taskState.integration.database -or
        [System.IO.Path]::GetFullPath($taskState.integration.database) -ne $taskDatabase -or
        $taskProcess.ExecutablePath -notin @($taskPython, $taskBasePython.Trim()) -or
        $taskProcess.CommandLine -notmatch '(app\.py\s+serve|tools[\\/]launch_interface\.py)') {
        throw 'Port belongs to another application; no process was stopped.'
    }
    if ($CheckOnly) { Write-Output "Verified project server on port $taskPort. No restart performed."; exit 0 }
    $taskResumeAutomatic = $taskState.integration.auto_poll -eq $true
    $taskHeaders = @{ 'X-Requested-With' = 'news-analytics-ui' }
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$taskPort/api/auto-poll" -Headers $taskHeaders -ContentType 'application/json' -Body '{"enabled":false}' | Out-Null
        Write-Output 'Waiting for the current model request to finish and save its result...'
        $taskDeadline = [DateTime]::UtcNow.AddMinutes(15)
        do {
            $taskState = Invoke-RestMethod "http://127.0.0.1:$taskPort/api/state" -TimeoutSec 10
            $taskWorking = $taskState.active_job.status -eq 'running' -or $taskState.integration.collection.status -eq 'running'
            if (-not $taskWorking) { break }
            if ([DateTime]::UtcNow -gt $taskDeadline) { throw 'Current work has not finished. No process was stopped.' }
            Start-Sleep -Seconds 1
        } while ($true)
        Stop-Process -Id $taskProcess.ProcessId
    } catch {
        if ($taskResumeAutomatic) {
            try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$taskPort/api/auto-poll" -Headers $taskHeaders -ContentType 'application/json' -Body '{"enabled":true}' | Out-Null } catch { Write-Warning 'Could not restore automatic processing.' }
        }
        throw
    }
    # Restore the launch preference only after the old process has stopped.
    # Restoring through its API before stopping could start another paid call.
    if ($taskResumeAutomatic) {
        & $taskPython -B -c 'import app; from src.integration.settings import load_env,save_auto_poll; load_env(); save_auto_poll(True)'
        if ($LASTEXITCODE -ne 0) { throw 'Could not restore automatic processing preference.' }
    }
    for ($taskAttempt = 0; $taskAttempt -lt 25; $taskAttempt++) {
        $taskStillListening = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object LocalPort -eq $taskPort)
        if (-not $taskStillListening.Count) { break }
        Start-Sleep -Milliseconds 200
    }
    if ($taskStillListening.Count) { throw 'Port did not become available. Try again.' }
} elseif ($CheckOnly) {
    Write-Output "Server is not running on port $taskPort."
    exit 0
}
if ($StopOnly) { Write-Output 'Server is stopped.'; exit 0 }
Write-Output 'Starting server. Keep this window open; press Ctrl+C here to stop it.'
& $taskPython -B (Join-Path $taskRoot 'tools\launch_interface.py')
exit $LASTEXITCODE
