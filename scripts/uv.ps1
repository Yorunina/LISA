# Keep uv's project environment and download cache inside the local runtime tree.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
$previousCache = $env:UV_CACHE_DIR
Push-Location $projectRoot
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot 'runtime\venv'
    $env:UV_CACHE_DIR = Join-Path $projectRoot 'runtime\cache\uv'
    & uv @args
    $uvExitCode = $LASTEXITCODE
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
    $env:UV_CACHE_DIR = $previousCache
    Pop-Location
}
exit $uvExitCode
