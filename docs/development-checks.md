# Development checks

The fast continuous-integration quality job runs Ruff and Python compilation on Python 3.11 and 3.13. The full offline test suite runs in a separate, informational job on the same versions. Test failures remain visible in the logs and job summary, but that job is allowed to fail and does not gate the quality job.

For day-to-day work, run the checks relevant to the files and behavior being changed. Investigate a failure introduced by the current change. Record unrelated failures separately and continue the scoped work; do not make a full-suite pass a prerequisite for every issue.

The five legacy tests removed on September 26, 2026 used obsolete constructor contracts for the version two optimizer and the earlier capacity snapshot. Their failures were reproduced on main before version five integration. Their former source remains in Git history. Current regression tests and production authorization, accounting and execution checks remain in place.

The advisory test policy does not establish strategy quality, evidence interpretation, optimization improvement or paper-trading readiness. Those claims still require the acceptance evidence in the component issues.

To run offline tests explicitly:

```powershell
python -m pytest -q --no-cov -m "not integration"
```

Use `.artifacts/README.md` for the local inputs, planning registers and retained evidence needed by the current issues. Generated scratch outputs should be removed after their useful result has been recorded; they should not accumulate as another broad historical archive.
