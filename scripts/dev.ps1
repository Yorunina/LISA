param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Setup', 'Start', 'Stop', 'Check', 'StartQQ', 'StopQQ', 'CheckQQ')]
    [string]$Action
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot 'runtime\venv\Scripts\python.exe'
$manager = Join-Path $PSScriptRoot 'dev_env.py'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

Push-Location $projectRoot
try {
    switch ($Action) {
        'Setup' {
            & "$PSScriptRoot\uv.ps1" sync --locked
            if ($LASTEXITCODE -ne 0) { throw 'Python dependency sync failed.' }
            Invoke-Checked 'npm.cmd' @('ci', '--cache', 'runtime/cache/npm')
            Invoke-Checked $python @('scripts/download_native.py')
            Invoke-Checked "$PSScriptRoot\build-pgvector.cmd" @()
            Invoke-Checked $python @($manager, 'init')
            Invoke-Checked 'npx.cmd' @('playwright', 'install', 'chromium')
        }
        'Start' {
            Invoke-Checked $python @($manager, 'init')
            Invoke-Checked $python @($manager, 'db-start')
            Invoke-Checked $python @($manager, 'db-init')
        }
        'Stop' { Invoke-Checked $python @($manager, 'db-stop') }
        'Check' { Invoke-Checked $python @($manager, 'check') }
        'CheckQQ' { Invoke-Checked $python @($manager, 'qq-check') }
        { $_ -in 'StartQQ', 'StopQQ' } {
            $napcatRoot = Join-Path $projectRoot 'runtime\tools\napcat'
            $node = Join-Path $napcatRoot 'node.exe'
            $pidFile = Join-Path $projectRoot 'runtime\napcat.pid'
            $existing = $null
            if (Test-Path -LiteralPath $pidFile) {
                $existing = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile)) -ErrorAction SilentlyContinue
                if ($existing -and $existing.Path -ne $node) {
                    throw 'Recorded NapCat PID belongs to a different executable; inspect runtime/napcat.pid.'
                }
            }
            if ($Action -eq 'StopQQ') {
                if ($existing) { Stop-Process -Id $existing.Id }
                if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
                Write-Output 'NapCat stopped; session data preserved.'
            } elseif ($existing) {
                Write-Output 'NapCat is already running.'
            } else {
                Invoke-Checked $python @($manager, 'init')
                $previousWorkDir = $env:NAPCAT_WORKDIR
                try {
                    $env:NAPCAT_WORKDIR = Join-Path $projectRoot 'runtime\napcat'
                    $nodeArgs = @('index.js')
                    $accountLine = Get-Content -LiteralPath 'runtime/.env' -Encoding UTF8 | Where-Object { $_ -match '^LISA_QQ_ACCOUNT=\d+$' } | Select-Object -First 1
                    if ($accountLine) { $nodeArgs += @('-q', $accountLine.Split('=', 2)[1]) }
                    $napcatProcessId = & $python -c 'import subprocess,sys; print(subprocess.Popen(sys.argv[2:], cwd=sys.argv[1], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW).pid)' $napcatRoot $node @nodeArgs
                    if ($LASTEXITCODE -ne 0) { throw 'NapCat could not be started.' }
                    Set-Content -LiteralPath $pidFile -Value $napcatProcessId -Encoding ASCII
                    Write-Output 'NapCat started. Open http://127.0.0.1:6099/webui and use the token in runtime/.env.'
                } finally { $env:NAPCAT_WORKDIR = $previousWorkDir }
            }
        }
    }
} finally { Pop-Location }
