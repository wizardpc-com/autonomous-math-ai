# Verification levels and result statuses

Assign the highest level actually supported by preserved evidence. State the scope of every computation explicitly.

| Level | Meaning | Minimum support |
| --- | --- | --- |
| `E0_SPECULATIVE` | LLM idea, informal conjecture, or untested argument | A clearly labeled idea or claim |
| `E1_NUMERIC` | Floating-point, random, heuristic, or optimization evidence | Parameters, precision, range or sample count, seed when relevant, and output |
| `E2_EXACT_TESTED` | Exact computation or exhaustive verification over a finite stated range | Exact inputs, command/code, version, full range, and exact output |
| `E3_REDUNDANT_EXACT` | Important exact result reproduced independently | Two meaningfully different implementations or tools, with both records preserved |
| `E4_CERTIFIED` | A replayable independent machine certificate or small exact verifier exists | Certificate plus a documented, independently runnable checker |
| `E5_FORMAL` | A proof-assistant kernel has checked the theorem | Source, toolchain versions, build command, and successful kernel check |

## Guardrails

- Never infer proof from a failed counterexample search.
- Never label floating-point agreement as exact.
- Never count two front ends calling the same backend as strong independent reproduction.
- Keep finite exhaustive work at `E2_EXACT_TESTED` or `E3_REDUNDANT_EXACT` unless the general-to-finite reduction is itself justified.
- A CAS output is not a formal proof. It may support E2-E4 depending on exactness, independence, and certificates.
- `E5_FORMAL` certifies the encoded statement; separately audit that the encoding matches the intended theorem.
- Do not assign an evidence level merely from a worker status. Verify that every required artifact exists and supports the claimed level.

## Allowed statuses

- `OPEN`: a live conjecture or proof obligation with material gaps.
- `FALSIFIED`: a valid counterexample refutes the exact statement.
- `EXPERIMENTALLY_SUPPORTED`: evidence supports the claim but does not prove it.
- `PROVED_INFORMALLY`: a complete human-auditable proof is recorded but not kernel-checked.
- `FORMALLY_VERIFIED`: a faithful statement has a successful proof-assistant kernel check.
- `UNKNOWN`: evidence is missing, conflicting, or not yet interpretable.

Do not use `PROVED` as a status for computational evidence.

Worker execution statuses (`COMPLETED`, `FALSIFIED`, `NO_COUNTEREXAMPLE_WITHIN_SCOPE`, `FORMAL_CHECK_PASSED`, `BLOCKED`, `TOOL_ERROR`) describe one bounded run. They do not replace these research-claim statuses. In particular, `COMPLETED` does not mean proved, and `FORMAL_CHECK_PASSED` reaches `E5_FORMAL` only when the checked statement is faithful and the proof has no admitted gap.
