[CmdletBinding()]
param(
    [string]$Root,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
if (-not $Root) {
    $Root = Join-Path $PSScriptRoot 'examples\minimal-group'
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python 3.11 or newer is required for the portable group doctor.'
}
$previous = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $PSScriptRoot 'src'
try {
    $arguments = @('-m', 'private_ai_company', 'doctor', '--root', $Root)
    if ($Json) { $arguments += '--json' }
    & $python.Source @arguments
    exit $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $previous
}
