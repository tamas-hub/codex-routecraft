param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RouteCraftArgs
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & $py.Source -3 (Join-Path $PSScriptRoot 'app\routecraft.py') @RouteCraftArgs
    exit $LASTEXITCODE
}
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python 3.11 or later was not found on PATH.'
}
& $python.Source (Join-Path $PSScriptRoot 'app\routecraft.py') @RouteCraftArgs
exit $LASTEXITCODE
