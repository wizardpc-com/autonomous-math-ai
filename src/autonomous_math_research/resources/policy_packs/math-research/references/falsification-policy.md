# Falsification-first policy

Before expensive proof construction, formalization, or broad search, choose the cheapest test that could expose a false claim or missing assumption.

Consider, when applicable:

- the smallest parameter values;
- boundary and degenerate cases;
- random and highly symmetric cases;
- adversarial examples and extreme parameter regions;
- exhaustive finite enumeration;
- finite-field reductions or reductions modulo small primes;
- removing each assumption in turn;
- nearby stronger and weaker variants.

State the tested claim and scope before running the test. Prefer exact serialization and deterministic enumeration. For randomized work, record the generator, seed, sample count, parameter distribution, and stopping rule.

If a counterexample appears:

1. stop trying to prove the false statement;
2. verify the witness independently when practical;
3. minimize it when that clarifies the failure;
4. record the exact claim, assumption, or proof step that fails;
5. return the evidence to the controller rather than automatically salvaging the original narrative.

If none appears, report only `NO_COUNTEREXAMPLE_WITHIN_SCOPE` or the research status `EXPERIMENTALLY_SUPPORTED`, with the exact finite or stochastic scope. Absence of a found counterexample is not proof.
