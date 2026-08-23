# Empirical audit policy

At least one independent audit is required for every claim-changing event, and
critical changes retain the controller's double-audit gate. The audit must check
that the hypothesis, inputs, splits, metrics, exclusions, stopping rule, and
analysis plan were frozen before execution. It must derive conclusions from
append-only raw results, not from a producer summary or overwritten analysis
artifact, and must keep infrastructure failures separate from research results.
