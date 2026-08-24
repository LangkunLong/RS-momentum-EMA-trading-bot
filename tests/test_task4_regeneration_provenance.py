from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".superpowers"
    / "sdd"
    / "2026-08-23-canonical-canslim-entry"
    / "task-4-regenerate.ps1"
)


def _run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def test_regeneration_refuses_dirty_correction_worktree_before_resolving_inputs(tmp_path: Path) -> None:
    """Removing the launch cleanliness guard must let the invalid-input error win."""
    repository = tmp_path / "correction-worktree"
    script = (
        repository
        / ".superpowers"
        / "sdd"
        / "2026-08-23-canonical-canslim-entry"
        / "task-4-regenerate.ps1"
    )
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    sentinel = repository / "tracked.txt"
    sentinel.write_text("clean\n", encoding="utf-8")
    _run_git(repository, "init", "--quiet")
    _run_git(repository, "add", ".")
    _run_git(
        repository,
        "-c",
        "user.name=Regeneration Probe",
        "-c",
        "user.email=regeneration-probe@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "probe fixture",
    )
    sentinel.write_text("dirty\n", encoding="utf-8")

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-SourceWorktree",
            str(tmp_path / "missing-source"),
            "-OutputRoot",
            str(tmp_path / "missing-parent" / "output"),
            "-PythonExe",
            str(tmp_path / "missing-python.exe"),
            "-SecUserAgent",
            "regeneration-probe@example.invalid",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "correction worktree must be clean at launch" in output
    assert not (tmp_path / "missing-parent").exists()


def test_create_only_directory_helper_runs_on_windows_powershell_and_refuses_existing_target(tmp_path: Path) -> None:
    """Removing create-only behavior must overwrite or accept the occupied probe target."""
    target = tmp_path / "create-only-target"
    harness = tmp_path / "create-only-directory-probe.ps1"
    harness.write_text(
        textwrap.dedent(
            """
            param(
                [string]$ProductionScript,
                [string]$Target
            )
            $ErrorActionPreference = 'Stop'
            $tokens = $null
            $parseErrors = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseFile(
                $ProductionScript,
                [ref]$tokens,
                [ref]$parseErrors
            )
            if ($parseErrors.Count -ne 0) {
                Write-Error 'production script did not parse'
                exit 90
            }
            $requiredFunctions = @('Assert-True', 'New-CreateOnlyDirectory')
            $functionAsts = @($ast.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
            }, $true))
            foreach ($name in $requiredFunctions) {
                $matches = @($functionAsts | Where-Object { $_.Name -ceq $name })
                if ($matches.Count -ne 1) {
                    Write-Error "required production function missing: $name"
                    exit 90
                }
                Invoke-Expression $matches[0].Extent.Text
            }

            [void](New-CreateOnlyDirectory $Target 'probe directory')
            $sentinel = Join-Path $Target 'sentinel.txt'
            [System.IO.File]::WriteAllText($sentinel, 'preserve me')
            try {
                [void](New-CreateOnlyDirectory $Target 'probe directory')
            }
            catch {
                Write-Output $_.Exception.Message
                exit 42
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-ProductionScript",
            str(SCRIPT),
            "-Target",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 42, output
    assert "refusing existing probe directory" in output
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "preserve me"
    assert "New-Item -ItemType Directory -LiteralPath" not in SCRIPT.read_text(encoding="utf-8")


def test_regeneration_audit_binds_exact_correction_sources_before_publication() -> None:
    """Removing a source hash or the final clean/unchanged guard must break this probe."""
    source = SCRIPT.read_text(encoding="utf-8")
    launch_git = source.index("$correctionGitAtLaunch =")
    launch_hashes = source.index("$correctionSourceHashesAtLaunch =")
    bundle_verification = source.index("'fresh PIT bundle verification'")
    final_guard = source.index("Assert-CorrectionProvenanceUnchanged `", bundle_verification)
    audit = source.index("$audit = [ordered]@{")
    publication = source.index("Write-CreateOnlyUtf8 $auditPath")

    assert launch_git < launch_hashes < bundle_verification
    assert bundle_verification < final_guard < audit < publication

    pre_audit_guard = source[final_guard:audit]
    assert "$gitExecutable $correctionWorktree $correctionGitAtLaunch $correctionSourceHashesAtLaunch" in pre_audit_guard
    assert "$fetchScript $bundleScript $verifyScript $regenerationDriverScript" in pre_audit_guard

    audit_block = source[audit:publication]
    assert "schema_version = 3" in audit_block
    for field in (
        "correction_git_head",
        "fetch_sec_pit_fundamentals_py_sha256",
        "build_pit_bundle_py_sha256",
        "verify_pit_bundle_py_sha256",
        "regeneration_driver_ps1_sha256",
    ):
        assert f"{field} =" in audit_block


def test_preserved_and_fresh_identity_manifest_hashes_are_wired_to_their_own_bytes() -> None:
    """Recombining the two identity hashes must reintroduce the preserved-provenance failure."""
    source = SCRIPT.read_text(encoding="utf-8")
    pinned_manifest = source.index("$pinnedSourceIdentityManifest = Get-RequiredFile")
    correction_manifest = source.index("$correctionProducerIdentityManifest = Get-RequiredFile")
    preserved_hashes = source.index("$preservedSourceInputHashes =")
    fresh_hashes = source.index("$freshSourceInputHashes =")
    preserved_validation = source.index("foreach ($field in $preservedSourceInputHashes.Keys)")
    fresh_validation = source.index("foreach ($field in $freshSourceInputHashes.Keys)")
    normalization = source.index("'--identity-manifest-csv', $correctionProducerIdentityManifest")
    final_identity_guard = source.index("Assert-IdentityManifestInputsUnchanged `", fresh_validation)
    audit = source.index("$audit = [ordered]@{")
    publication = source.index("Write-CreateOnlyUtf8 $auditPath")

    assert pinned_manifest < preserved_hashes < preserved_validation
    assert correction_manifest < fresh_hashes < normalization < fresh_validation
    assert preserved_validation < normalization
    assert fresh_validation < final_identity_guard < audit

    input_hash_setup = source[pinned_manifest:preserved_validation]
    assert r"Join-Path $sourceWorktreePath 'config\pit_price_identity_map.csv'" in input_hash_setup
    assert r"Join-Path $correctionWorktree 'config\pit_price_identity_map.csv'" in input_hash_setup
    assert "$pinnedSourceIdentityManifest" in source[preserved_hashes:fresh_hashes]
    assert "$correctionProducerIdentityManifest" in source[fresh_hashes:preserved_validation]

    final_guard = source[final_identity_guard:audit]
    assert "$pinnedSourceIdentityManifest $preservedSourceInputHashes['identity_manifest_csv_sha256']" in final_guard
    assert "$correctionProducerIdentityManifest $freshSourceInputHashes['identity_manifest_csv_sha256']" in final_guard

    audit_block = source[audit:publication]
    assert "schema_version = 3" in audit_block
    assert "pinned_source_identity_manifest_csv_sha256 =" in audit_block
    assert "correction_producer_identity_manifest_csv_sha256 =" in audit_block


@pytest.mark.parametrize(
    ("drift_target", "expected_error"),
    (
        (
            "fetch_sec_pit_fundamentals.py",
            "correction source fetch_sec_pit_fundamentals_py_sha256 after generation differs",
        ),
        (
            "pinned_identity_manifest.csv",
            "pinned source identity manifest SHA-256 after generation differs",
        ),
        (
            "pit_price_identity_map.csv",
            "correction producer identity manifest SHA-256 after generation differs",
        ),
    ),
)
def test_final_provenance_guard_rejects_hidden_input_drift_before_audit(
    tmp_path: Path,
    drift_target: str,
    expected_error: str,
) -> None:
    """Removing any final byte comparison must publish an audit for unreported drift."""
    repository = tmp_path / "correction-worktree"
    repository.mkdir()
    source_names = (
        "fetch_sec_pit_fundamentals.py",
        "build_pit_bundle.py",
        "verify_pit_bundle.py",
        "task-4-regenerate.ps1",
        "pinned_identity_manifest.csv",
        "pit_price_identity_map.csv",
    )
    for name in source_names:
        (repository / name).write_text(f"original {name}\n", encoding="utf-8")
    _run_git(repository, "init", "--quiet")
    _run_git(repository, "add", ".")
    _run_git(
        repository,
        "-c",
        "user.name=Regeneration Probe",
        "-c",
        "user.email=regeneration-probe@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "probe fixture",
    )

    harness = tmp_path / "final-provenance-probe.ps1"
    harness.write_text(
        textwrap.dedent(
            """
            param(
                [string]$ProductionScript,
                [string]$Repository,
                [string]$AuditPath,
                [string]$DriftTarget
            )
            $ErrorActionPreference = 'Stop'
            Import-Module Microsoft.PowerShell.Utility
            $tokens = $null
            $parseErrors = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseFile(
                $ProductionScript,
                [ref]$tokens,
                [ref]$parseErrors
            )
            if ($parseErrors.Count -ne 0) {
                Write-Error 'production script did not parse'
                exit 90
            }
            $requiredFunctions = @(
                'Assert-True',
                'Assert-Equal',
                'Get-Sha256',
                'Invoke-GitText',
                'Get-CorrectionGitSnapshot',
                'Get-CleanCorrectionGitSnapshot',
                'Get-CorrectionSourceHashes',
                'Assert-CorrectionProvenanceUnchanged',
                'Assert-IdentityManifestInputsUnchanged'
            )
            $functionAsts = @($ast.FindAll({
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
            }, $true))
            foreach ($name in $requiredFunctions) {
                $matches = @($functionAsts | Where-Object { $_.Name -ceq $name })
                if ($matches.Count -ne 1) {
                    Write-Error "required production function missing: $name"
                    exit 90
                }
                Invoke-Expression $matches[0].Extent.Text
            }

            $gitExecutable = (Get-Command git -CommandType Application | Select-Object -First 1).Source
            $fetchScript = Join-Path $Repository 'fetch_sec_pit_fundamentals.py'
            $bundleScript = Join-Path $Repository 'build_pit_bundle.py'
            $verifyScript = Join-Path $Repository 'verify_pit_bundle.py'
            $driverScript = Join-Path $Repository 'task-4-regenerate.ps1'
            $pinnedIdentityManifest = Join-Path $Repository 'pinned_identity_manifest.csv'
            $correctionIdentityManifest = Join-Path $Repository 'pit_price_identity_map.csv'
            $launchGit = Get-CleanCorrectionGitSnapshot $gitExecutable $Repository 'at launch'
            $launchHashes = Get-CorrectionSourceHashes $fetchScript $bundleScript $verifyScript $driverScript
            $pinnedIdentityHashAtLaunch = Get-Sha256 $pinnedIdentityManifest
            $correctionIdentityHashAtLaunch = Get-Sha256 $correctionIdentityManifest

            & $gitExecutable -C $Repository update-index --assume-unchanged -- $DriftTarget
            if ($LASTEXITCODE -ne 0) {
                Write-Error 'could not hide the controlled drift from Git status'
                exit 90
            }
            $driftPath = Join-Path $Repository $DriftTarget
            [System.IO.File]::WriteAllText($driftPath, "hidden drift`n")
            $hiddenStatus = Invoke-GitText $gitExecutable $Repository 'hidden-drift status read' @(
                'status', '--porcelain=v1', '--untracked-files=all'
            )
            if (-not [string]::IsNullOrWhiteSpace($hiddenStatus)) {
                Write-Error "probe drift was not hidden from Git status: $hiddenStatus"
                exit 90
            }

            try {
                Assert-CorrectionProvenanceUnchanged `
                    $gitExecutable $Repository $launchGit $launchHashes `
                    $fetchScript $bundleScript $verifyScript $driverScript
                Assert-IdentityManifestInputsUnchanged `
                    $pinnedIdentityManifest $pinnedIdentityHashAtLaunch `
                    $correctionIdentityManifest $correctionIdentityHashAtLaunch
                [System.IO.File]::WriteAllText($AuditPath, 'incorrectly published')
            }
            catch {
                Write-Output $_.Exception.Message
                exit 42
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None
    audit = tmp_path / "task-4-regeneration-audit.json"
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-ProductionScript",
            str(SCRIPT),
            "-Repository",
            str(repository),
            "-AuditPath",
            str(audit),
            "-DriftTarget",
            drift_target,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 42, output
    assert expected_error in output
    assert not audit.exists()
