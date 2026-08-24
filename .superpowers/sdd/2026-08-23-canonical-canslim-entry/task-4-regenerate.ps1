<#
Prepare and execute only after the active replay has completed.

This script deliberately has no defaults for the source worktree or output
root.  It creates a new output root, copies the exact archive transaction
with Copy-Item (never a hardlink), and relies on the existing-complete-archive
branch in fetch_sec_pit_fundamentals.py.  That branch verifies local archives
and does not construct a requests session.  The current fetch CLI has no
explicit --offline/--reuse-local switch.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SourceWorktree,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SecUserAgent
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ExpectedArchives = @{
    'submissions.zip' = @{
        sha256 = '928d67221c6e6183bc343e7234c1391448c15cd1dd644d36b425db2f99ba4350'
        byte_length = 1559612838
        zip_entry_count = 987520
        zip_uncompressed_bytes = 5721893028
    }
    'companyfacts.zip' = @{
        sha256 = 'd7b4b3c5f2fe014a203bdaef2197d2cba5683f434e965fc9bced1023a43c82ca'
        byte_length = 1407131132
        zip_entry_count = 20266
        zip_uncompressed_bytes = 19277465283
    }
}
$ExpectedCounts = @{
    security_master = 568
    security_master_exclusions = 39
    fundamentals = 142538
    fundamentals_audit = 142538
    xom = 209
    xom_quarterly = 71
    xom_annual = 30
    xom_balance = 108
}
$NormalizedFiles = @(
    'security_master.csv',
    'security_master_exclusions.csv',
    'fundamentals.csv',
    'fundamentals_audit.csv',
    'fundamentals_provenance.json',
    'fundamentals_coverage.json'
)
$ProvenanceHashFields = [ordered]@{
    'security_master.csv' = 'security_master_sha256'
    'security_master_exclusions.csv' = 'security_master_exclusions_sha256'
    'fundamentals.csv' = 'fundamentals_sha256'
    'fundamentals_audit.csv' = 'fundamentals_audit_sha256'
    'fundamentals_coverage.json' = 'fundamentals_coverage_sha256'
}

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Actual,
        [Parameter(Mandatory = $true)]
        [object]$Expected,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )
    if ($Actual -cne $Expected) {
        throw "$Label differs: expected [$Expected], actual [$Actual]"
    }
}

function Get-RequiredDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )
    Assert-True ([System.IO.Path]::IsPathRooted($Path)) "$Label must be an absolute path"
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    Assert-True $item.PSIsContainer "$Label must be a directory: $Path"
    Assert-True ((($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0)) "$Label cannot be a reparse point: $Path"
    return $item.FullName
}

function Get-RequiredFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )
    Assert-True ([System.IO.Path]::IsPathRooted($Path)) "$Label must be an absolute path"
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    Assert-True (-not $item.PSIsContainer) "$Label must be a file: $Path"
    Assert-True ((($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0)) "$Label cannot be a reparse point: $Path"
    return $item.FullName
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Invoke-GitText {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$Worktree,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $output = @(& $Executable -C $Worktree @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $text = (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
    if ($exitCode -ne 0) {
        throw "$Label failed with exit code ${exitCode}: $text"
    }
    return $text
}

function Get-CorrectionGitSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$Worktree
    )
    $head = (Invoke-GitText $Executable $Worktree 'correction-worktree HEAD read' @(
        'rev-parse', '--verify', 'HEAD'
    )).ToLowerInvariant()
    Assert-True ($head -match '\A[0-9a-f]{40,64}\z') "correction worktree returned an invalid Git HEAD: $head"
    $status = Invoke-GitText $Executable $Worktree 'correction-worktree status read' @(
        'status', '--porcelain=v1', '--untracked-files=all'
    )
    return [pscustomobject]@{
        head = $head
        status = $status
    }
}

function Get-CleanCorrectionGitSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$Worktree,
        [Parameter(Mandatory = $true)][string]$Phase
    )
    $snapshot = Get-CorrectionGitSnapshot $Executable $Worktree
    Assert-True ([string]::IsNullOrWhiteSpace($snapshot.status)) "correction worktree must be clean $Phase"
    return $snapshot
}

function Get-CorrectionSourceHashes {
    param(
        [Parameter(Mandatory = $true)][string]$FetchScript,
        [Parameter(Mandatory = $true)][string]$BundleScript,
        [Parameter(Mandatory = $true)][string]$VerifyScript,
        [Parameter(Mandatory = $true)][string]$DriverScript
    )
    return [ordered]@{
        fetch_sec_pit_fundamentals_py_sha256 = Get-Sha256 $FetchScript
        build_pit_bundle_py_sha256 = Get-Sha256 $BundleScript
        verify_pit_bundle_py_sha256 = Get-Sha256 $VerifyScript
        regeneration_driver_ps1_sha256 = Get-Sha256 $DriverScript
    }
}

function Assert-CorrectionProvenanceUnchanged {
    param(
        [Parameter(Mandatory = $true)][string]$GitExecutable,
        [Parameter(Mandatory = $true)][string]$Worktree,
        [Parameter(Mandatory = $true)][object]$LaunchGit,
        [Parameter(Mandatory = $true)][object]$LaunchHashes,
        [Parameter(Mandatory = $true)][string]$FetchScript,
        [Parameter(Mandatory = $true)][string]$BundleScript,
        [Parameter(Mandatory = $true)][string]$VerifyScript,
        [Parameter(Mandatory = $true)][string]$DriverScript
    )
    $currentGit = Get-CleanCorrectionGitSnapshot $GitExecutable $Worktree 'after generation and before audit publication'
    Assert-Equal $currentGit.head $LaunchGit.head 'correction-worktree Git HEAD after generation'
    $currentHashes = Get-CorrectionSourceHashes $FetchScript $BundleScript $VerifyScript $DriverScript
    foreach ($field in $LaunchHashes.Keys) {
        Assert-Equal $currentHashes[$field] $LaunchHashes[$field] "correction source $field after generation"
    }
}

function Read-JsonObject {
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        return (Get-Content -LiteralPath $Path -Raw -Encoding utf8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw "invalid JSON at ${Path}: $($_.Exception.Message)"
    }
}

function Get-RequiredJsonProperty {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        throw "$Label lacks required property: $Name"
    }
    return $property.Value
}

function Assert-ExactJsonProperties {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $actual = @($Object.PSObject.Properties.Name | Sort-Object)
    $expectedSorted = @($Expected | Sort-Object)
    Assert-Equal ($actual -join "`0") ($expectedSorted -join "`0") "$Label property names"
}

function Assert-ArchiveManifest {
    param([Parameter(Mandatory = $true)][string]$Path)
    $manifest = Read-JsonObject $Path
    Assert-Equal (Get-RequiredJsonProperty $manifest 'schema_version' 'archive provenance') 1 'archive provenance schema version'
    $archives = Get-RequiredJsonProperty $manifest 'archives' 'archive provenance'
    Assert-ExactJsonProperties $archives @('submissions.zip', 'companyfacts.zip') 'archive provenance archives'
    foreach ($name in @('submissions.zip', 'companyfacts.zip')) {
        $entry = Get-RequiredJsonProperty $archives $name 'archive provenance archives'
        $expected = $ExpectedArchives[$name]
        foreach ($field in @('sha256', 'byte_length', 'zip_entry_count', 'zip_uncompressed_bytes')) {
            Assert-Equal (Get-RequiredJsonProperty $entry $field "archive provenance $name") $expected[$field] "archive provenance $name $field"
        }
        Assert-Equal (Get-RequiredJsonProperty $entry 'max_json_member_bytes' "archive provenance $name") 536870912 "archive provenance $name max_json_member_bytes"
    }
    return $manifest
}

function Test-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Parent
    )
    $normalizedChild = [System.IO.Path]::GetFullPath($Child).TrimEnd([char[]]@('\', '/'))
    $normalizedParent = [System.IO.Path]::GetFullPath($Parent).TrimEnd([char[]]@('\', '/'))
    return $normalizedChild.Equals($normalizedParent, [System.StringComparison]::OrdinalIgnoreCase) -or
        $normalizedChild.StartsWith("$normalizedParent\", [System.StringComparison]::OrdinalIgnoreCase)
}

function Invoke-CheckedPython {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Write-CreateOnlyUtf8 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($Text)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

$correctionWorktree = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path
$gitExecutable = (Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$correctionGitAtLaunch = Get-CleanCorrectionGitSnapshot $gitExecutable $correctionWorktree 'at launch'
$correctionWorktreeParent = Get-RequiredDirectory (Split-Path -Path $correctionWorktree -Parent) 'correction-worktree parent'
$expectedSourceWorktree = Get-RequiredDirectory (Join-Path $correctionWorktreeParent 'five-year-public-pit') 'expected sibling source worktree'
$sourceWorktreePath = Get-RequiredDirectory $SourceWorktree 'source worktree'
$pythonPath = Get-RequiredFile $PythonExe 'agent virtual-environment Python executable'
Assert-True ($sourceWorktreePath.Equals($expectedSourceWorktree, [System.StringComparison]::OrdinalIgnoreCase)) "source worktree must be the exact sibling: $expectedSourceWorktree"

Assert-True ([System.IO.Path]::IsPathRooted($OutputRoot)) 'output root must be an absolute path'
$outputRootPath = [System.IO.Path]::GetFullPath($OutputRoot).TrimEnd([char[]]@('\', '/'))
$canonicalWorktree = [System.IO.Path]::GetFullPath((Join-Path (Split-Path -Path $correctionWorktree -Parent) 'canonical-canslim-entry'))
Assert-True (-not (Test-PathWithin $outputRootPath $sourceWorktreePath)) 'output root cannot be inside the read-only source worktree'
Assert-True (-not (Test-PathWithin $outputRootPath $canonicalWorktree)) 'output root cannot be inside the active canonical worktree'
Assert-True (-not (Test-Path -LiteralPath $outputRootPath -PathType Any)) "refusing existing output root: $outputRootPath"
$outputParent = Get-RequiredDirectory (Split-Path -Path $outputRootPath -Parent) 'output-root parent'

$sourceSecDir = Get-RequiredDirectory (Join-Path $sourceWorktreePath '.artifacts\sec-pit') 'pinned source SEC directory'
$sourceExportsDir = Get-RequiredDirectory (Join-Path $sourceWorktreePath 'exports\pit') 'pinned source PIT export directory'
$sourceArchives = @{}
foreach ($name in @('submissions.zip', 'companyfacts.zip', 'sec_archives_provenance.json')) {
    $sourceArchives[$name] = Get-RequiredFile (Join-Path $sourceSecDir $name) "pinned source $name"
}
$preservedNormalizedPaths = [ordered]@{}
foreach ($name in $NormalizedFiles) {
    $preservedNormalizedPaths[$name] = Get-RequiredFile (Join-Path $sourceSecDir $name) "preserved normalized $name"
}
$preservedMarkerPath = Get-RequiredFile (Join-Path $sourceSecDir 'fundamentals_publication.json') 'preserved fundamentals publication marker'
$membershipCsv = Get-RequiredFile (Join-Path $sourceExportsDir 'membership.csv') 'membership CSV'
$securityNamesCsv = Get-RequiredFile (Join-Path $sourceExportsDir 'security_names.csv') 'security names CSV'
$spyTradingDaysCsv = Get-RequiredFile (Join-Path $sourceExportsDir 'spy_trading_days.csv') 'SPY trading-days CSV'
$pricesCsv = Get-RequiredFile (Join-Path $sourceExportsDir 'prices.csv') 'prices CSV'
$membershipProvenance = Get-RequiredFile (Join-Path $sourceExportsDir 'membership_provenance.json') 'membership provenance'
$pricesProvenance = Get-RequiredFile (Join-Path $sourceExportsDir 'prices_provenance.json') 'prices provenance'
$identityManifest = Get-RequiredFile (Join-Path $correctionWorktree 'config\pit_price_identity_map.csv') 'correction-worktree identity manifest'
$fetchScript = Get-RequiredFile (Join-Path $correctionWorktree 'fetch_sec_pit_fundamentals.py') 'correction-worktree SEC normalizer'
$bundleScript = Get-RequiredFile (Join-Path $correctionWorktree 'build_pit_bundle.py') 'correction-worktree bundle builder'
$verifyScript = Get-RequiredFile (Join-Path $correctionWorktree 'verify_pit_bundle.py') 'correction-worktree bundle verifier'
$regenerationDriverScript = Get-RequiredFile $PSCommandPath 'correction-worktree regeneration driver'
$correctionSourceHashesAtLaunch = Get-CorrectionSourceHashes `
    $fetchScript $bundleScript $verifyScript $regenerationDriverScript

$sourceArchiveProvenanceHash = Get-Sha256 $sourceArchives['sec_archives_provenance.json']
[void](Assert-ArchiveManifest $sourceArchives['sec_archives_provenance.json'])
$sourceArchiveHashesBefore = @{}
foreach ($name in @('submissions.zip', 'companyfacts.zip')) {
    $sourceArchiveHashesBefore[$name] = Get-Sha256 $sourceArchives[$name]
    Assert-Equal $sourceArchiveHashesBefore[$name] $ExpectedArchives[$name]['sha256'] "pinned source $name SHA-256 before copy"
}
$sourceInputHashes = @{
    'membership_csv_sha256' = Get-Sha256 $membershipCsv
    'security_names_csv_sha256' = Get-Sha256 $securityNamesCsv
    'spy_trading_days_csv_sha256' = Get-Sha256 $spyTradingDaysCsv
    'identity_manifest_csv_sha256' = Get-Sha256 $identityManifest
    'submissions_archive_sha256' = $ExpectedArchives['submissions.zip']['sha256']
    'companyfacts_archive_sha256' = $ExpectedArchives['companyfacts.zip']['sha256']
}

# The preserved outputs are comparison truth only after their own marker and
# provenance prove an exact, complete publication bound to these pinned inputs.
$preservedMarker = Read-JsonObject $preservedMarkerPath
Assert-Equal (Get-RequiredJsonProperty $preservedMarker 'schema_version' 'preserved publication marker') 1 'preserved publication marker schema version'
Assert-Equal (Get-RequiredJsonProperty $preservedMarker 'status' 'preserved publication marker') 'complete' 'preserved publication marker status'
$preservedMarkerFiles = Get-RequiredJsonProperty $preservedMarker 'files' 'preserved publication marker'
Assert-ExactJsonProperties $preservedMarkerFiles $NormalizedFiles 'preserved publication marker files'
$preservedNormalizedHashes = [ordered]@{}
foreach ($name in $NormalizedFiles) {
    $preservedNormalizedHashes[$name] = Get-Sha256 $preservedNormalizedPaths[$name]
    Assert-Equal (Get-RequiredJsonProperty $preservedMarkerFiles $name 'preserved publication marker files') $preservedNormalizedHashes[$name] "preserved publication marker hash for $name"
}
$preservedProvenance = Read-JsonObject $preservedNormalizedPaths['fundamentals_provenance.json']
Assert-Equal (Get-RequiredJsonProperty $preservedProvenance 'schema_version' 'preserved fundamentals provenance') 1 'preserved fundamentals provenance schema version'
Assert-Equal (Get-RequiredJsonProperty $preservedProvenance 'source' 'preserved fundamentals provenance') 'SEC EDGAR official bulk archives' 'preserved fundamentals provenance source'
Assert-Equal (Get-RequiredJsonProperty $preservedProvenance 'start_date' 'preserved fundamentals provenance') '2020-01-01' 'preserved fundamentals provenance start date'
Assert-Equal (Get-RequiredJsonProperty $preservedProvenance 'end_date' 'preserved fundamentals provenance') '2025-12-31' 'preserved fundamentals provenance end date'
foreach ($name in $ProvenanceHashFields.Keys) {
    $field = $ProvenanceHashFields[$name]
    Assert-Equal (Get-RequiredJsonProperty $preservedProvenance $field 'preserved fundamentals provenance') $preservedNormalizedHashes[$name] "preserved fundamentals provenance hash for $name"
}
foreach ($field in $sourceInputHashes.Keys) {
    Assert-Equal (Get-RequiredJsonProperty $preservedProvenance $field 'preserved fundamentals provenance') $sourceInputHashes[$field] "preserved fundamentals provenance $field"
}
$preservedArchiveManifest = Get-RequiredJsonProperty $preservedProvenance 'archive_manifest' 'preserved fundamentals provenance'
Assert-Equal (Get-RequiredJsonProperty $preservedArchiveManifest 'schema_version' 'preserved provenance archive manifest') 1 'preserved provenance archive manifest schema version'
$preservedArchiveEntries = Get-RequiredJsonProperty $preservedArchiveManifest 'archives' 'preserved provenance archive manifest'
Assert-ExactJsonProperties $preservedArchiveEntries @('submissions.zip', 'companyfacts.zip') 'preserved provenance archive entries'
foreach ($name in @('submissions.zip', 'companyfacts.zip')) {
    $entry = Get-RequiredJsonProperty $preservedArchiveEntries $name 'preserved provenance archive entries'
    Assert-Equal (Get-RequiredJsonProperty $entry 'sha256' "preserved provenance $name") $ExpectedArchives[$name]['sha256'] "preserved provenance $name SHA-256"
}

# Create-only publication layout.  Failures intentionally leave the fresh root
# in place for forensic inspection; this script never deletes or overwrites it.
New-Item -ItemType Directory -LiteralPath $outputRootPath -ErrorAction Stop | Out-Null
$freshSecDir = Join-Path $outputRootPath 'sec-pit'
New-Item -ItemType Directory -LiteralPath $freshSecDir -ErrorAction Stop | Out-Null
foreach ($name in @('submissions.zip', 'companyfacts.zip', 'sec_archives_provenance.json')) {
    $destination = Join-Path $freshSecDir $name
    Assert-True (-not (Test-Path -LiteralPath $destination -PathType Any)) "refusing existing fresh SEC target: $destination"
    # Copy-Item is deliberate: these must not become hardlinks to pinned source bytes.
    Copy-Item -LiteralPath $sourceArchives[$name] -Destination $destination -ErrorAction Stop
    [void](Get-RequiredFile $destination "fresh copied $name")
}

Assert-Equal (Get-Sha256 (Join-Path $freshSecDir 'sec_archives_provenance.json')) $sourceArchiveProvenanceHash 'copied archive provenance SHA-256'
[void](Assert-ArchiveManifest (Join-Path $freshSecDir 'sec_archives_provenance.json'))
foreach ($name in @('submissions.zip', 'companyfacts.zip')) {
    Assert-Equal (Get-Sha256 (Join-Path $freshSecDir $name)) $ExpectedArchives[$name]['sha256'] "fresh copied $name SHA-256"
}
foreach ($name in $NormalizedFiles + @('fundamentals_publication.json')) {
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $freshSecDir $name) -PathType Any)) "fresh SEC directory unexpectedly contains normalized target: $name"
}

Push-Location -LiteralPath $correctionWorktree
try {
    # All three archive-transaction files exist before this call.  The normalizer
    # therefore takes its verified local-reuse branch and does not call the SEC.
    Invoke-CheckedPython $pythonPath 'local SEC normalization' @(
        '-B', $fetchScript,
        '--membership-csv', $membershipCsv,
        '--security-names-csv', $securityNamesCsv,
        '--spy-trading-days-csv', $spyTradingDaysCsv,
        '--start-date', '2020-01-01',
        '--end-date', '2025-12-31',
        '--sec-user-agent', $SecUserAgent,
        '--max-archive-bytes', '10737418240',
        '--identity-manifest-csv', $identityManifest,
        '--output-dir', $freshSecDir
    )
}
finally {
    Pop-Location
}

# Rehash both original and copied archives after normalization.  Any change is
# terminal; neither the source nor the copied archive transaction may drift.
Assert-Equal (Get-Sha256 $sourceArchives['sec_archives_provenance.json']) $sourceArchiveProvenanceHash 'pinned archive provenance SHA-256 after normalization'
foreach ($name in @('submissions.zip', 'companyfacts.zip')) {
    Assert-Equal (Get-Sha256 $sourceArchives[$name]) $sourceArchiveHashesBefore[$name] "pinned source $name SHA-256 after normalization"
    Assert-Equal (Get-Sha256 (Join-Path $freshSecDir $name)) $ExpectedArchives[$name]['sha256'] "fresh copied $name SHA-256 after normalization"
}
[void](Assert-ArchiveManifest (Join-Path $freshSecDir 'sec_archives_provenance.json'))

$markerPath = Get-RequiredFile (Join-Path $freshSecDir 'fundamentals_publication.json') 'fresh fundamentals publication marker'
$marker = Read-JsonObject $markerPath
Assert-Equal (Get-RequiredJsonProperty $marker 'schema_version' 'fresh publication marker') 1 'fresh publication marker schema version'
Assert-Equal (Get-RequiredJsonProperty $marker 'status' 'fresh publication marker') 'complete' 'fresh publication marker status'
$markerFiles = Get-RequiredJsonProperty $marker 'files' 'fresh publication marker'
Assert-ExactJsonProperties $markerFiles $NormalizedFiles 'fresh publication marker files'
$normalizedHashes = [ordered]@{}
foreach ($name in $NormalizedFiles) {
    $path = Get-RequiredFile (Join-Path $freshSecDir $name) "fresh normalized $name"
    $normalizedHashes[$name] = Get-Sha256 $path
    Assert-Equal (Get-RequiredJsonProperty $markerFiles $name 'fresh publication marker files') $normalizedHashes[$name] "fresh publication marker hash for $name"
}

$fundamentalsProvenancePath = Join-Path $freshSecDir 'fundamentals_provenance.json'
$fundamentalsProvenance = Read-JsonObject $fundamentalsProvenancePath
Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance 'schema_version' 'fresh fundamentals provenance') 1 'fresh fundamentals provenance schema version'
Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance 'source' 'fresh fundamentals provenance') 'SEC EDGAR official bulk archives' 'fresh fundamentals provenance source'
Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance 'start_date' 'fresh fundamentals provenance') '2020-01-01' 'fresh fundamentals provenance start date'
Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance 'end_date' 'fresh fundamentals provenance') '2025-12-31' 'fresh fundamentals provenance end date'
foreach ($name in $ProvenanceHashFields.Keys) {
    $field = $ProvenanceHashFields[$name]
    Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance $field 'fresh fundamentals provenance') $normalizedHashes[$name] "fresh fundamentals provenance hash for $name"
}
foreach ($field in $sourceInputHashes.Keys) {
    Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance $field 'fresh fundamentals provenance') $sourceInputHashes[$field] "fresh fundamentals provenance $field"
}
Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance 'security_master_row_count' 'fresh fundamentals provenance') $ExpectedCounts['security_master'] 'fresh security-master row count'
Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance 'security_master_exclusion_row_count' 'fresh fundamentals provenance') $ExpectedCounts['security_master_exclusions'] 'fresh exclusion row count'
Assert-Equal (Get-RequiredJsonProperty $fundamentalsProvenance 'fundamental_row_count' 'fresh fundamentals provenance') $ExpectedCounts['fundamentals'] 'fresh fundamental row count'
$coverage = Read-JsonObject (Join-Path $freshSecDir 'fundamentals_coverage.json')
Assert-Equal (Get-RequiredJsonProperty $coverage 'fundamental_row_count' 'fresh fundamentals coverage') $ExpectedCounts['fundamentals'] 'fresh coverage fundamental row count'
Assert-Equal (Get-RequiredJsonProperty $coverage 'no_fundamental_rows_symbol_count' 'fresh fundamentals coverage') 1 'fresh coverage no-fundamental symbol count'

# Compare every non-XOM normalized row canonically, then enforce the reviewed
# XOM/FRC invariants without serializing any financial content into the audit.
$csvValidationCode = @'
import csv
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
fresh = pathlib.Path(sys.argv[2])
expected = {
    "security_master.csv": 568,
    "security_master_exclusions.csv": 39,
    "fundamentals.csv": 142538,
    "fundamentals_audit.csv": 142538,
}

def read_rows(path):
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or any(not field for field in reader.fieldnames):
            raise AssertionError(f"invalid CSV header: {path.name}")
        rows = list(reader)
    if any(set(row) != set(reader.fieldnames) for row in rows):
        raise AssertionError(f"incomplete CSV row: {path.name}")
    return tuple(reader.fieldnames), rows

def canonical_non_xom(path):
    header, rows = read_rows(path)
    normalized = [json.dumps({field: row[field] for field in header}, sort_keys=True, separators=(",", ":"))
                  for row in rows if row.get("ticker") != "XOM"]
    return header, tuple(sorted(normalized))

source_stats = {}
for name, total in expected.items():
    source_header, source_rows = read_rows(source / name)
    fresh_header, fresh_rows = read_rows(fresh / name)
    if source_header != fresh_header:
        raise AssertionError(f"header changed for {name}")
    if len(fresh_rows) != total:
        raise AssertionError(f"unexpected fresh row count for {name}: {len(fresh_rows)}")
    _, source_non_xom = canonical_non_xom(source / name)
    _, fresh_non_xom = canonical_non_xom(fresh / name)
    if source_non_xom != fresh_non_xom:
        raise AssertionError(f"non-XOM canonical rows changed for {name}")
    source_stats[name] = len(source_rows)

master_header, master_rows = read_rows(fresh / "security_master.csv")
xom_master = [row for row in master_rows if row["ticker"] == "XOM"]
if len(xom_master) != 1 or xom_master[0]["cik"] != "0000034088" or xom_master[0]["mapping_basis"] != "reviewed_baseline_cik":
    raise AssertionError("XOM reviewed security-master mapping is invalid")

fund_header, fund_rows = read_rows(fresh / "fundamentals.csv")
audit_header, audit_rows = read_rows(fresh / "fundamentals_audit.csv")
xom_fundamentals = [row for row in fund_rows if row["ticker"] == "XOM"]
xom_audit = [row for row in audit_rows if row["ticker"] == "XOM"]
if len(xom_fundamentals) != 209 or len(xom_audit) != 209:
    raise AssertionError("XOM normalized or audit row count is invalid")
by_statement = {kind: sum(row["statement_type"] == kind for row in xom_fundamentals)
                for kind in ("quarterly", "annual", "balance")}
if by_statement != {"quarterly": 71, "annual": 30, "balance": 108}:
    raise AssertionError(f"XOM statement split is invalid: {by_statement}")
if any(row["public_date"] > "2025-12-31" for row in xom_fundamentals):
    raise AssertionError("XOM has a post-cutoff public row")
fundamental_keys = {(row["ticker"], row["statement_type"], row["period_end"], row["public_date"])
                    for row in xom_fundamentals}
audit_keys = {(row["ticker"], row["statement_type"], row["period_end"], row["public_date"])
              for row in xom_audit}
if len(fundamental_keys) != len(xom_fundamentals):
    raise AssertionError("XOM fundamental keys are not one row per visible key")
if len(audit_keys) != len(xom_audit):
    raise AssertionError("XOM audit keys are not one row per visible key")
if fundamental_keys != audit_keys:
    raise AssertionError("XOM fundamental and audit keys differ")
master_tickers = {row["ticker"] for row in master_rows}
fundamental_tickers = {row["ticker"] for row in fund_rows}
if master_tickers - fundamental_tickers != {"FRC"}:
    raise AssertionError("FRC is not the sole resolved no-fundamental symbol")
print(json.dumps({"non_xom_rows_identical": True, "xom_rows": len(xom_fundamentals),
                  "xom_statement_counts": by_statement, "sole_no_fundamental_symbol_count": 1,
                  "source_row_counts": source_stats}, sort_keys=True))
'@
Invoke-CheckedPython $pythonPath 'SEC output invariant validation' @('-B', '-c', $csvValidationCode, $sourceSecDir, $freshSecDir)

$bundleDir = Join-Path $outputRootPath 'pit-bundle'
New-Item -ItemType Directory -LiteralPath $bundleDir -ErrorAction Stop | Out-Null
$bundlePath = Join-Path $bundleDir 'pit_baseline.sqlite3'
$bundleManifestPath = Join-Path $bundleDir 'bundle_manifest.json'
Assert-True (-not (Test-Path -LiteralPath $bundlePath -PathType Any)) "refusing existing bundle target: $bundlePath"
Assert-True (-not (Test-Path -LiteralPath $bundleManifestPath -PathType Any)) "refusing existing bundle manifest target: $bundleManifestPath"

Push-Location -LiteralPath $correctionWorktree
try {
    Invoke-CheckedPython $pythonPath 'fresh PIT bundle build' @(
        '-B', $bundleScript,
        '--membership-csv', $membershipCsv,
        '--prices-csv', $pricesCsv,
        '--fundamentals-csv', (Join-Path $freshSecDir 'fundamentals.csv'),
        '--data-cutoff', '2025-12-31',
        '--evaluation-start', '2021-01-01',
        '--warmup-start', '2020-01-01',
        '--membership-provenance', $membershipProvenance,
        '--prices-provenance', $pricesProvenance,
        '--fundamentals-provenance', $fundamentalsProvenancePath,
        '--output', $bundlePath,
        '--manifest-output', $bundleManifestPath
    )
    $bundleHash = Get-Sha256 (Get-RequiredFile $bundlePath 'fresh PIT bundle')
    [void](Get-RequiredFile $bundleManifestPath 'fresh PIT bundle manifest')
    Invoke-CheckedPython $pythonPath 'fresh PIT bundle verification' @(
        '-B', $verifyScript,
        '--bundle', $bundlePath,
        '--sha256', $bundleHash,
        '--manifest', $bundleManifestPath,
        '--membership-csv', $membershipCsv,
        '--prices-csv', $pricesCsv,
        '--fundamentals-csv', (Join-Path $freshSecDir 'fundamentals.csv'),
        '--membership-provenance', $membershipProvenance,
        '--prices-provenance', $pricesProvenance,
        '--fundamentals-provenance', $fundamentalsProvenancePath
    )
}
finally {
    Pop-Location
}

Assert-CorrectionProvenanceUnchanged `
    $gitExecutable $correctionWorktree $correctionGitAtLaunch $correctionSourceHashesAtLaunch `
    $fetchScript $bundleScript $verifyScript $regenerationDriverScript

$auditPath = Join-Path $outputRootPath 'task-4-regeneration-audit.json'
Assert-True (-not (Test-Path -LiteralPath $auditPath -PathType Any)) "refusing existing regeneration audit: $auditPath"
$audit = [ordered]@{
    schema_version = 2
    status = 'complete'
    correction_git_head = $correctionGitAtLaunch.head
    fetch_sec_pit_fundamentals_py_sha256 = $correctionSourceHashesAtLaunch['fetch_sec_pit_fundamentals_py_sha256']
    build_pit_bundle_py_sha256 = $correctionSourceHashesAtLaunch['build_pit_bundle_py_sha256']
    verify_pit_bundle_py_sha256 = $correctionSourceHashesAtLaunch['verify_pit_bundle_py_sha256']
    regeneration_driver_ps1_sha256 = $correctionSourceHashesAtLaunch['regeneration_driver_ps1_sha256']
    date_contract = [ordered]@{
        warmup_start = '2020-01-01'
        evaluation_start = '2021-01-01'
        data_cutoff = '2025-12-31'
    }
    source_archives_sha256 = [ordered]@{
        submissions = $ExpectedArchives['submissions.zip']['sha256']
        companyfacts = $ExpectedArchives['companyfacts.zip']['sha256']
    }
    sec_archives_provenance_sha256 = $sourceArchiveProvenanceHash
    normalized_files_sha256 = $normalizedHashes
    validated_counts = [ordered]@{
        security_master = $ExpectedCounts['security_master']
        security_master_exclusions = $ExpectedCounts['security_master_exclusions']
        fundamentals = $ExpectedCounts['fundamentals']
        fundamentals_audit = $ExpectedCounts['fundamentals_audit']
        xom = $ExpectedCounts['xom']
        xom_quarterly = $ExpectedCounts['xom_quarterly']
        xom_annual = $ExpectedCounts['xom_annual']
        xom_balance = $ExpectedCounts['xom_balance']
        sole_no_fundamental_symbols = 1
    }
    validations = [ordered]@{
        archive_copy = 'copy_item_and_sha256_verified_before_and_after'
        no_network = 'complete_preexisting_archive_transaction_reused'
        non_xom_rows = 'canonical_identical_to_preserved_outputs'
        xom_reviewed_cik = '0000034088'
        xom_mapping_basis = 'reviewed_baseline_cik'
        bundle = 'verify_pit_bundle_passed'
    }
    bundle_sha256 = $bundleHash
    bundle_manifest_sha256 = Get-Sha256 $bundleManifestPath
    generated_at_utc = [DateTime]::UtcNow.ToString('O')
}
$auditJson = $audit | ConvertTo-Json -Depth 8
$auditRoundTrip = $auditJson | ConvertFrom-Json -ErrorAction Stop
Assert-ExactJsonProperties $auditRoundTrip @(
    'schema_version',
    'status',
    'correction_git_head',
    'fetch_sec_pit_fundamentals_py_sha256',
    'build_pit_bundle_py_sha256',
    'verify_pit_bundle_py_sha256',
    'regeneration_driver_ps1_sha256',
    'date_contract',
    'source_archives_sha256',
    'sec_archives_provenance_sha256',
    'normalized_files_sha256',
    'validated_counts',
    'validations',
    'bundle_sha256',
    'bundle_manifest_sha256',
    'generated_at_utc'
) 'regeneration audit'
Write-CreateOnlyUtf8 $auditPath ($auditJson + "`n")
Write-Output "Task 4 regeneration completed and audited at $outputRootPath"
