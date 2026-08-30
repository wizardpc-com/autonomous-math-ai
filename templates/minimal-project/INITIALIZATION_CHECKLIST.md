# Initialization checklist

- [ ] Replace every `AMR_PLACEHOLDER` marker.
- [ ] Confirm the project id and final claim id.
- [ ] Record the exact claim, domain, quantifiers, assumptions, and dependencies.
- [ ] Freeze the same final statement in `autonomous/semantics.json`, recompute
      its SHA256, and review every structured core-term binding, free-text
      terminology risk, forbidden confusion, and bridge declaration.
- [ ] Review canonical inputs and protected paths.
- [ ] Define stable exact-scope ids; review any external-result and Asset Card
      manifests; create a Campaign Theme when the run must stay inside one topic.
- [ ] Review role/provider routes, budgets, retries, concurrency, and timeouts.
- [ ] Review mechanical selection, fallback, backpressure, and separate budget.
- [ ] Keep credentials as environment/system/profile references only.
- [ ] Run `amr config validate`, `amr config explain`, and `amr validate --strict`.
