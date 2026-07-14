# Similarity scorer goldens

`simrisk-v1.0.0.json` is the frozen, synthetic correctness corpus for the
version-locked scorer. Cases are ordered by the scenario list in the G005 test
specification; retained references are ordered by `evidence_id`. Each case
contains canonical raw scorer inputs and exact expected pair and candidate
summary outputs. Feature and decision objects are keyed by `feature_id`, and
exact percentages use closed numerator/denominator/value objects. These
fixtures prove deterministic implementation behavior,
not the legal validity or empirical calibration of the provisional thresholds.
JSON Schema enforces their closed structural and bounded-value contract;
`validate_audit_artifact` authoritatively cross-checks every rational against
its six-decimal `value` and display mirror before audit hashing or publication.

Calibration data, if independently reviewed, belongs in the sibling
`tests/fixtures/similarity/calibration/` directory and must not be mixed into
these implementation goldens.
