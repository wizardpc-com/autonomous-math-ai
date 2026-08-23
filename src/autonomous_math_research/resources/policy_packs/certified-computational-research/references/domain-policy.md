# Certified computational claim semantics

The claim statuses are `OPEN`, `SUPPORTED`, `REFUTED`, `INCONCLUSIVE`, and
`CERTIFIED`. `SUPPORTED` is frontier evidence, while only `CERTIFIED` satisfies a
positive dependency. `REFUTED` is the negative terminal status.

Every non-bridge candidate event requires a deterministic checker. A
`CHECKER_SUPPORT` or `CHECKER_REFUTATION` needs at least replayable exact-tested
evidence. A `CERTIFICATE` needs certificate-level evidence bound to the exact
claim, input, checker version, and configuration. An inconclusive or failed
check must not be silently recoded as support, refutation, or certification.
