[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'routecraft_memory.py'

if (Get-Command python3 -ErrorAction SilentlyContinue) {
    & python3 $scriptPath @Arguments
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $scriptPath @Arguments
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $scriptPath @Arguments
} else {
    throw 'Python 3 command not found (python3, python, or py)'
}

exit $LASTEXITCODE
