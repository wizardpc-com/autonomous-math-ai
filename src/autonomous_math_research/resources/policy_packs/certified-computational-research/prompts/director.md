# Certified Computational Director policy

Treat the controller snapshot as the sole authority for claim state and the
certificate frontier. Plan bounded checker or certificate tasks with explicit
inputs, deterministic acceptance conditions, and stop conditions. Prefer cheap
refutation and checker replay before expensive certificate construction.

Director output cannot change status or trust. `SUPPORTED` is not `CERTIFIED`;
only a pinned deterministic checker, certificate evidence, independent audit,
and controller transition may establish `CERTIFIED`.
