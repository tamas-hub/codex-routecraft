param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RouteCraftArgs
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python 3.11 or later was not found on PATH.'
}
& $python.Source (Join-Path $PSScriptRoot 'routecraft.py') @RouteCraftArgs
exit $LASTEXITCODE
