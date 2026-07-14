# G005 final similarity audit contract

G005 runs a new, separately fingerprinted Korean/English KIPRIS query group for every
current finalist after `finalist-set-v1`. Identical text remains a separate attempt because
the persisted request fingerprint includes the finalist-set hash, finalist ID, and query
group ID; these values never enter the KIPRIS request parameters.

Each candidate corpus deduplicates application/publication identity plus content revision,
then orders records by query-hit count descending, best source rank ascending, application
identity, and content hash. It retains 100 records plus every substantive tie at the boundary.
The corpus contains only evidence reached through the exact G005 query IDs.

`simrisk-v1.0.0` uses exact rational arithmetic. Text risk is 25% title and 75% abstract,
where each overlap is the mean of normalized token Jaccard and character-trigram Dice.
Feature weights are problem 10%, inputs 10%, mechanism 30%, transformations 20%, outputs
10%, and technical effects 20%. Classification similarity is subgroup 1, main group .8,
subclass .55, section .25, otherwise 0. Explicit differentiation credit is limited to
reviewed essential-feature decisions supported by source spans or explicitly inspected
non-disclosure fields.

`R = 100 * clamp(.25T + .60F + .15C - .20D, 0, 1)`. Missing positive inputs are zero in
`R_obs` and one in `R_hi`. Labels begin at 35, 55, and 75. Observed risk at least 75 enters
`decision_required`; otherwise an empty corpus, Q below .80, or upper bound at least 75 enters
`coverage_insufficient`; all other results enter `audit_approved`. Reports always describe a
provisional research aid within the retrieved corpus, never a legal novelty conclusion.

The audit batch and a coverage/excessive-similarity gate are published in one transaction.
G006 records every decision as a current hash-bound private artifact. It requires a complete
per-finalist excessive-risk decision batch and dispatches retry, expansion, retention,
refinement, replacement/research, and stop through kernel-owned state targets.
