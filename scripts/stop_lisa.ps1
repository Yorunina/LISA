param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$script:LisaProjectRoot = Split-Path $PSScriptRoot -Parent

function Select-LisaProcesses {
    param([object[]]$Processes, [string]$Python)
    $ownedIds = @($Processes | Where-Object {
        $_.ExecutablePath -eq $Python -or
        ($_.CommandLine -and $_.CommandLine.IndexOf($Python, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    } | ForEach-Object { $_.ProcessId })
    $Processes | Where-Object {
        ($_.ProcessId -in $ownedIds -or $_.ParentProcessId -in $ownedIds) -and
        $_.CommandLine -match '(?:\buvicorn\s+lisa\.app:create_app|-m\s+lisa\.(?:worker|gateway))(?:\s|$)'
    }
}

function Get-LisaProcesses {
    $snapshot = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' })
    Select-LisaProcesses -Processes $snapshot -Python (Join-Path $script:LisaProjectRoot 'runtime\venv\Scripts\python.exe')
}

function Stop-LisaProcess {
    param($Target)
    # Recheck the snapshot before stopping a PID that may have exited meanwhile.
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($Target.ProcessId)"
    if (-not $current) { return }
    if ($current.CreationDate -ne $Target.CreationDate -or $current.CommandLine -ne $Target.CommandLine) {
        throw 'Process identity changed; no action taken. Run Stop-LISA again.'
    }
    $process = Get-Process -Id $current.ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -InputObject $process -Force
        $process.WaitForExit()
    }
}

function Invoke-LisaDependencyStop {
    param([string]$Action)
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $script:LisaProjectRoot 'scripts\dev.ps1') $Action
    if ($LASTEXITCODE -ne 0) { throw "Dependency action $Action failed ($LASTEXITCODE)." }
}

function Invoke-LisaStop {
    param([switch]$CheckOnly)
    $lockPath = Join-Path $script:LisaProjectRoot 'runtime\startup.lock'
    [IO.Directory]::CreateDirectory((Split-Path $lockPath -Parent)) | Out-Null
    try {
        # Exclusive sharing also blocks start_lisa.py's open handle and vice versa.
        $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch {
        throw 'Another LISA start/stop operation is running. Please try again later.'
    }
    try {
        $targets = @(Get-LisaProcesses)
        if ($CheckOnly) {
            Write-Output "Stop check: $($targets.Count) project application processes. No services stopped."
            return
        }
        Write-Output 'Stopping LISA application processes. In-progress generation may be interrupted.'
        # Stop actual Python children before their venv launcher parents.
        $python = Join-Path $script:LisaProjectRoot 'runtime\venv\Scripts\python.exe'
        $targets | Sort-Object @{Expression = { $_.ExecutablePath -eq $python }} | ForEach-Object {
            Stop-LisaProcess -Target $_
        }
        if (@(Get-LisaProcesses).Count) {
            throw 'Some application processes are still running; database left running.'
        }
        Invoke-LisaDependencyStop 'StopQQ'
        Invoke-LisaDependencyStop 'Stop'
        Write-Output 'LISA stopped. Database, works, configuration and QQ session data preserved.'
    } finally {
        $lock.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-LisaStop -CheckOnly:$CheckOnly } catch {
        Write-Error $_ -ErrorAction Continue
        exit 1
    }
}
