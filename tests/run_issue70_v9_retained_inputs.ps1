param()

$ErrorActionPreference = 'Stop'
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$RetainedTest = 'tests/retained_issue70_v9_source_contract.py'
$AcceptedTestBlob = 'd77a303392a8032066106d32b5480048d4f77698'
$RequiredInputs = @(
    'docs/issue-70-calendar-candidate-v1/exchange_sessions.csv',
    'docs/issue-70-calendar-candidate-v1/calendar_provenance.json',
    'docs/issue-70-calendar-candidate-v1/candidate-publication.json',
    'docs/issue-70-calendar-candidate-v1/principal-adoption-decision.json',
    'docs/issue-70-q4-source-sample-v8/spy_trading_days.csv',
    'docs/issue-70-q4-source-sample-v8/fundamentals_provenance.json'
)

$MissingInputs = @(
    $RequiredInputs | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $RepositoryRoot $_) -PathType Leaf)
    }
)
if ($MissingInputs.Count -gt 0) {
    Write-Host ("ERROR: retained Issue 70 suite requires the exact pinned inputs; missing:`n - " + ($MissingInputs -join "`n - ")) -ForegroundColor Red
    exit 2
}

$ActualTestBlob = git -C $RepositoryRoot hash-object -- (Join-Path $RepositoryRoot $RetainedTest)
if ($LASTEXITCODE -ne 0 -or $ActualTestBlob.Trim() -ne $AcceptedTestBlob) {
    Write-Host "ERROR: retained suite differs from accepted 96afe6d test blob $AcceptedTestBlob; refusing to run." -ForegroundColor Red
    exit 2
}

Push-Location $RepositoryRoot
try {
    python -B -m pytest -q -p no:cacheprovider --no-cov $RetainedTest
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
