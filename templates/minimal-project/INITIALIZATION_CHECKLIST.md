# Initialization checklist

- [ ] Replace every `AMR_PLACEHOLDER` marker.
- [ ] Confirm the project id and final claim id.
- [ ] Record the exact claim, domain, quantifiers, assumptions, and dependencies.
- [ ] Review canonical inputs and protected paths.
- [ ] Review role/provider routes, budgets, retries, concurrency, and timeouts.
- [ ] Review mechanical selection, fallback, backpressure, and separate budget.
- [ ] Keep credentials as environment/system/profile references only.
- [ ] Run `amr config validate`, `amr config explain`, and `amr validate --strict`.
