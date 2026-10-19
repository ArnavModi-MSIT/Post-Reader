param([switch]$ChromeOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$mutexName = if ($ChromeOnly) { 'Local\LinkedInReaderChromeBootstrap' } else { 'Local\LinkedInReaderLauncher' }
$mutex = [System.Threading.Mutex]::new($false, $mutexName)
$ownsMutex = $false
$transcribing = $false
$runCode = 1
try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) {
        Write-Host 'The reader is already running. See the existing window and logs folder.'
        exit 1
    }
    $logDir = Join-Path $PSScriptRoot 'logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $logPath = Join-Path $logDir ('run_' + (Get-Date -Format 'yyyy-MM-dd_HH-mm-ss-fff') + '.log')
    Start-Transcript -LiteralPath $logPath | Out-Null
    $transcribing = $true
    Write-Host 'LinkedIn job reader - no input prompts'
    Write-Host "Log: $logPath"

    $pythonPrefix = @()
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        $pythonExe = $venvPython
    } elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
        $pythonExe = (Get-Command python.exe).Source
    } elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $pythonExe = (Get-Command py.exe).Source
        $pythonPrefix = @('-3')
    } else {
        throw 'Python was not found. Install Python 3.10+ and enable its PATH option.'
    }
    & $pythonExe @pythonPrefix -c 'import sys; assert sys.version_info >= (3, 10); import playwright.sync_api'
    if ($LASTEXITCODE -ne 0) {
        throw 'Python 3.10+ with Playwright is required. Run: python -m pip install -r requirements.txt'
    }

    function Test-DebugChrome {
        try {
            $version = Invoke-RestMethod -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 2
            return [bool]($version.webSocketDebuggerUrl -and $version.Browser -match '(Chrome|Chromium)')
        } catch { return $false }
    }
    if (-not (Test-DebugChrome)) {
        $chromePaths = @(
            (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
            (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
        )
        $chromeExe = $chromePaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $chromeExe) { throw 'Google Chrome was not found in its standard installation folders.' }
        $profilePath = Join-Path $PSScriptRoot '.browser-profile'
        Write-Host 'Starting the dedicated Chrome profile...'
        Start-Process -FilePath $chromeExe -WindowStyle Hidden -ArgumentList @(
            '--remote-debugging-port=9222',
            '--remote-debugging-address=127.0.0.1',
            ('--user-data-dir="' + $profilePath + '"')
        ) | Out-Null
        $deadline = (Get-Date).AddSeconds(25)
        while (-not (Test-DebugChrome)) {
            if ((Get-Date) -ge $deadline) {
                throw 'Chrome debugging did not become available. Close Chrome windows using this project profile and try again.'
            }
            Start-Sleep -Milliseconds 500
        }
    } else {
        Write-Host 'Reusing Chrome on port 9222.'
    }
    $env:PYTHONUNBUFFERED = '1'
    # Continue lets native stderr reach the transcript without losing the exit code.
    $ErrorActionPreference = 'Continue'
    if ($ChromeOnly) {
        $runCode = 0
    } else {
        & $pythonExe @pythonPrefix (Join-Path $PSScriptRoot 'reader.py') 2>&1 | Out-Host
        $runCode = $LASTEXITCODE
    }
    $ErrorActionPreference = 'Stop'
    Write-Host "Reader finished with exit code $runCode. JSON files: $(Join-Path $PSScriptRoot 'data')"
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message)
    $runCode = 1
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
exit $runCode
