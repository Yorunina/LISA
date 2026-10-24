$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\stop_lisa.ps1')

function Assert-LisaTest {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$python = Join-Path $script:LisaProjectRoot 'runtime\venv\Scripts\python.exe'
$sample = @(
    [pscustomobject]@{ ProcessId = 11; ParentProcessId = 0; ExecutablePath = $python; CommandLine = "`"$python`" -m lisa.worker" },
    [pscustomobject]@{ ProcessId = 12; ParentProcessId = 11; ExecutablePath = 'C:\Python313\python.exe'; CommandLine = 'python.exe -m lisa.worker' },
    [pscustomobject]@{ ProcessId = 13; ParentProcessId = 0; ExecutablePath = 'C:\other\python.exe'; CommandLine = 'python.exe -m lisa.gateway' },
    [pscustomobject]@{ ProcessId = 14; ParentProcessId = 0; ExecutablePath = $python; CommandLine = "`"$python`" -m lisa.worker_test" },
    [pscustomobject]@{ ProcessId = 15; ParentProcessId = 0; ExecutablePath = $python; CommandLine = "`"$python`" -m uvicorn lisa.app:create_app --factory" }
)
$selected = @(Select-LisaProcesses -Processes $sample -Python $python)
Assert-LisaTest (($selected.ProcessId -join ',') -eq '11,12,15') 'Project/child selection included foreign or unrelated processes.'
Assert-LisaTest (@(Select-LisaProcesses -Processes @() -Python $python).Count -eq 0) 'Empty selection failed.'

$script:current = $null
function Get-CimInstance { param($ClassName, $Filter); $script:current }
Stop-LisaProcess -Target ([pscustomobject]@{ ProcessId = 11 })

$script:current = [pscustomobject]@{ ProcessId = 11; CreationDate = 2; CommandLine = 'other' }
$blocked = $false
try {
    Stop-LisaProcess -Target ([pscustomobject]@{ ProcessId = 11; CreationDate = 1; CommandLine = 'original' })
} catch { $blocked = $_.Exception.Message -match 'identity changed' }
Assert-LisaTest $blocked 'Reused PID was not rejected.'

$script:stopped = $false
$script:waited = $false
$script:fakeProcess = [pscustomobject]@{ Id = 11 }
$script:fakeProcess | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { $script:waited = $true }
function Get-Process { param($Id, $ErrorAction); $script:fakeProcess }
function Stop-Process { param($InputObject, [switch]$Force); $script:stopped = $true }
Stop-LisaProcess -Target $script:current
Assert-LisaTest ($script:stopped -and $script:waited) 'Matching process was not stopped and awaited.'

$script:events = [Collections.Generic.List[string]]::new()
$script:queryCount = 0
$script:remaining = $false
function Get-LisaProcesses {
    $script:queryCount++
    if ($script:queryCount -eq 1 -or $script:remaining) { $sample[0] }
}
function Stop-LisaProcess { param($Target); $script:events.Add("process:$($Target.ProcessId)") }
function Invoke-LisaDependencyStop { param($Action); $script:events.Add($Action) }

Invoke-LisaStop -CheckOnly
Assert-LisaTest ($script:events.Count -eq 0) 'CheckOnly stopped services.'

$script:queryCount = 0
Invoke-LisaStop
Assert-LisaTest (($script:events -join ',') -eq 'process:11,StopQQ,Stop') 'Stop order is incorrect.'

$script:events.Clear()
$script:queryCount = 1
Invoke-LisaStop
Assert-LisaTest (($script:events -join ',') -eq 'StopQQ,Stop') 'Repeated stop did not handle an empty application list.'

$script:events.Clear()
$script:queryCount = 0
$script:remaining = $true
$blocked = $false
try { Invoke-LisaStop } catch { $blocked = $_.Exception.Message -match 'still running' }
Assert-LisaTest $blocked 'Stop did not detect an application that remained alive.'
Assert-LisaTest (($script:events -join ',') -eq 'process:11') 'Dependencies stopped despite remaining application processes.'

$lockPath = Join-Path $script:LisaProjectRoot 'runtime\startup.lock'
$heldLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $blocked = $false
    try { Invoke-LisaStop -CheckOnly } catch { $blocked = $_.Exception.Message -match 'start/stop operation' }
    Assert-LisaTest $blocked 'Concurrent operation was not blocked.'
} finally { $heldLock.Dispose() }

Write-Output 'PASS: 10 isolated stop checks; no real services stopped.'
