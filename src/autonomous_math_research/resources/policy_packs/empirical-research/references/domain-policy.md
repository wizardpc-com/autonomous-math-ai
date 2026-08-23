# Empirical claim semantics

The claim statuses are `OPEN`, `SUPPORTED`, `NOT_SUPPORTED`, `INCONCLUSIVE`,
`CONFIRMED`, and `REPLICATED`. Support remains on the frontier. Positive
dependencies require `CONFIRMED` or `REPLICATED`; `NOT_SUPPORTED` is the
negative terminal status for the frozen protocol and tested scope.

Every non-bridge event must bind a frozen protocol. `EXPERIMENT_SUPPORT` and
`EXPERIMENT_NOT_SUPPORTED` report the protocol's bounded result. `CONFIRMATION`
and `REPLICATION` require redundant exact evidence under their declared
independence conditions. Finite benchmark evidence never means mathematical
`PROVED`, and an infrastructure error never means `NOT_SUPPORTED`.
