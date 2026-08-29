param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RouteCraftCoreArgs
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$scriptPath = Join-Path $PSScriptRoot 'routecraft-core.py'
if (Get-Command python3 -ErrorAction SilentlyContinue) { & python3 -X utf8 $scriptPath @RouteCraftCoreArgs }
elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -X utf8 $scriptPath @RouteCraftCoreArgs }
elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -X utf8 $scriptPath @RouteCraftCoreArgs }
else { throw 'Python 3 command not found (python3, python, or py)' }
exit $LASTEXITCODE
