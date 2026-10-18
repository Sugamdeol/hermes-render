# Run from the cloned repository. Requires Python 3, Git and Docker Desktop (Linux containers).
[CmdletBinding()]
param(
    [ValidateSet('start','logs','status','backup','stop','password')]
    [string]$Command = 'start',
    [ValidateRange(1,65535)][int]$Port = 10000
)
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'run-local.py'
if (-not (Test-Path -LiteralPath $launcher)) { throw 'Download or clone the complete hermes-render repository first.' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and enable Linux containers.'
}
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $launcher $Command --port $Port
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $launcher $Command --port $Port
} else { throw 'Install Python 3 from https://www.python.org/downloads/windows/ with PATH enabled.' }
exit $LASTEXITCODE
