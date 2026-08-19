# Local-first mathematical compute

Use the user's machine as the default execution backend.  A model may design,
adapt, or audit a computation, but a known deterministic command does not need
another model call merely to launch it.

## Routing order

```text
known script/command -> direct local CAS/compiler/solver -> deterministic check
mechanical code missing -> one-shot worker prepares or adapts it -> run locally
new mathematical choice -> controller decides before more compute
```

Prefer one warm batch over many cold starts.  Preserve exact inputs, commands,
versions, bounds, stdout/stderr, runtime, exit status, and output hashes.

## Execution-interface granularity

Treat the controller-to-shell boundary as a coarse-grained interface:

```text
one controller launch -> one WSL batch -> a bounded pool of warm backends
                      -> many deterministic shards -> one canonical merge
```

- Do not launch `wsl.exe`, Conda, Sage, or a model worker once per small shard.
- If a representative shard runs for less than ten times the measured backend
  startup cost, batch several shards in one backend process or use a persistent
  process pool whose workers each initialize the CAS once.
- For long jobs, keep one execution session and poll its progress; do not
  relaunch the command merely to check state.
- Use `xargs -P` or one process per shard only when each shard is coarse enough
  to amortize backend startup.  For microtasks, use a handler loop or a process
  pool that consumes multiple inputs per worker.
- Measure end-to-end wall time and peak resident memory with the actual input;
  shell-launch microbenchmarks alone do not predict a large algebraic job.

## Resource preflight

Read `.tooling/math-tools.json`.  If it is missing, stale, or has schema below
3, refresh it with `detect_math_tools.py`.  Inspect:

- host logical and affinity CPU counts;
- host and WSL memory limits;
- NVIDIA device and free VRAM;
- host and Sage-environment acceleration modules;
- exact backend availability.

Do not infer GPU usability from the GPU name alone.  Require both a visible
device and an installed backend used by the actual program.

If WSL has materially less memory than a planned Sage/Singular job needs while
the host has spare RAM, report the bottleneck.  Changing `.wslconfig` and
restarting WSL affects every WSL workload and therefore remains an explicit
system-level user decision.

## CPU saturation without oversubscription

Choose between internal threads and independent processes; do not maximize
both.  For independent shards use

```text
jobs <= min(number_of_shards,
            floor(affinity_cpus / threads_per_job),
            floor(memory_budget / measured_peak_memory_per_job))
```

Calibrate one representative shard before a large batch.  If peak memory is
unknown, begin with 2--4 workers and scale after observing runtime and memory.
For memory-heavy Gröbner or elimination jobs, process count is usually bounded
by memory well before CPU count.

For a WSL job, take the memory budget from the smaller of current host-available
RAM and WSL-available RAM after reserves.  WSL's reported free memory is not an
extra pool independent of the Windows host.

This workspace permits an aggressive profile for explicitly requested heavy
local compute.  Even then, keep one or two host cores and at least 2 GiB host
memory available unless the user asks for exclusive use.  Run only one heavy
batch at a time and set a finite timeout/stop condition.

### Exact algebra

- Prefer Sage/Singular/FLINT/PARI over pure SymPy after a small benchmark when
  they represent the same exact problem.
- Invoke an already detected Sage environment binary directly for repeated
  jobs.  Avoid `conda run` per shard; it adds a fresh environment-launch cost.
- Batch many small Sage tasks in one process when isolation is unnecessary.
- When the workload is already expressible in Singular and needs no Sage-only
  objects, benchmark the direct `Singular` CLI.  Keep Sage as the orchestrator
  when it materially simplifies field construction, conversions, or checking;
  do not pay its front-end startup once per small Singular kernel.
- For direct Singular CLI jobs, benchmark its `--cpus`, `--threads`, and
  `--flint-threads` options.  Record the chosen values.  Embedded algorithms
  may ignore them, so verify scaling rather than assuming it.
- Parallelize independent primes, scenarios, supports, fibers, or parameter
  blocks as separate deterministic shards.  Merge in canonical order and
  independently validate counts/hashes.
- For large finite enumeration, move hot loops to compiled C/C++ or a native
  solver after profiling; Python should orchestrate rather than own the hot
  loop.

### Cache and resume

- Hash immutable inputs and code for every shard.
- Store one completion record per shard and skip only a shard whose input hash,
  command, tool version, and output validation still match.
- Write shard outputs separately, then perform one deterministic merge.
- Do not rerun a verified expensive stage merely to regenerate a summary.

## GPU gate

GPU is appropriate for dense numerical linear algebra, FFT/convolution,
large batched numerical evaluation, or a deliberately implemented batched
finite-field/modular kernel.  It is usually not useful for branch-heavy exact
Gröbner bases, symbolic factorization, arbitrary-precision integer arithmetic,
or proof checking.

Before GPU execution require all of:

1. `nvidia-smi` sees the intended device and enough free VRAM;
2. the exact program has an installed CUDA-capable backend such as CuPy,
   PyTorch, JAX, or a compiled CUDA implementation;
3. a small CPU/GPU parity smoke test passes;
4. transfer and compilation overhead are amortized by the batch;
5. important exact or certification output is independently checked on CPU.

If a required GPU runtime/library is absent, report the gate.  Installing a
driver, CUDA toolkit, or large Python environment is a separate system change
and requires explicit authorization; never pretend that setting
`CUDA_VISIBLE_DEVICES` makes a CPU-only program use the GPU.

## Process hygiene

- Prefer direct executable argument arrays over nested shell strings.
- Avoid repeated WSL and Conda cold starts.
- Prevent nested parallelism by setting per-process thread counts when running
  multiple shards.
- Use finite timeouts and terminate the complete process tree on timeout.
- After interruption, check for orphan Python, Sage, Singular, solver, and WSL
  processes before restarting.
- Restrict validation to the project or artifact in scope; do not traverse
  unrelated worktrees for a local result.

## Evidence boundary

Parallelism and faster hardware change runtime, not evidence level.  Validate
every shard, deterministic merge, and certificate exactly as in a sequential
run.  A faster no-counterexample search is still not a proof.
