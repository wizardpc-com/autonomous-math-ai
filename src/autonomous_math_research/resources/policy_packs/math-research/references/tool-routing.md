# Mathematical tool routing

Choose the least expensive backend that can produce the required evidence. Use exact arithmetic unless the task is explicitly exploratory.

For costly work, read [local-compute.md](local-compute.md).  Prefer a direct
local executable and one warm batch.  Benchmark a representative shard before
choosing backend, process count, internal threads, or GPU.  Never use a model
worker solely as a command launcher.

## Polynomial systems, ideals, elimination, and resultants

Use SageMath for orchestration and Singular for Gröbner bases, ideals, elimination, resultants, and finite-characteristic polynomial systems. For repeated WSL jobs, prefer the detected Sage/Singular environment binary over a fresh `conda run` for every shard. Use one WSL batch and warm CAS workers rather than one WSL/Sage launch per small system. Benchmark direct Singular against Sage for the actual kernel; direct Singular is preferred when the calculation needs no Sage-only construction or conversion. Benchmark Singular `--cpus`, `--threads`, and `--flint-threads`; otherwise shard independent systems across processes. If WolframScript is available, use it as an optional independent symbolic cross-check for important results. Use Macaulay2 when the problem is naturally algebraic-geometric and its abstractions materially improve the computation.

## Number theory and finite fields

Prefer SageMath or PARI/GP. Preserve exact integer, rational, algebraic, p-adic, and finite-field inputs. Record field definitions, modulus choices, precision when p-adic truncation is involved, and every exceptional characteristic.

## Group theory

Prefer GAP, optionally orchestrated through Sage. Record group presentations, library identifiers, actions, and enumeration limits.

## Finite combinatorics and graph theory

Start with Python or Sage. Canonicalize objects, hash canonical forms, and bucket by invariants before comparing pairs. Preserve generation rules and pruning. Move to compiled C/C++ or installed tools such as nauty, Traces, plantri, or bliss only when scale justifies it. Independently check boundary counts when inexpensive.

## Symbolic identities

Prefer Wolfram Language when available; otherwise use Sage/SymPy or the appropriate exact backend. Reproduce important identities with a meaningfully different implementation or verify the final identity by exact substitution/coefficient comparison. Two front ends using the same engine are not strong independence.

## Numerical exploration

Use Python scientific tools or Wolfram Language to discover patterns, extremizers, or adversarial parameter regions. Record precision, conditioning, algorithms, tolerances, bounds, and seeds. Floating-point output is `E1_NUMERIC`, never proof.

Use GPU only when the detected environment contains a CUDA-capable backend and
the workload has enough dense or batched arithmetic to amortize transfers and
compilation.  Exact symbolic CAS work remains CPU-first unless a specific GPU
kernel is implemented and independently checked.

## Finite feasibility

Use SAT/SMT or an exact integer constraint solver when the encoding is clearer than manual case analysis. Preserve the instance, solver version, exact command, and proof/certificate trace when supported. Validate a satisfying assignment independently; replay UNSAT certificates when practical.

## Formal proof

Use Lean 4 with Mathlib only after the formalization gate in [formalization-policy.md](formalization-policy.md). Lean is not a numerical or CAS backend. Separately audit that the encoded theorem matches the intended statement.
