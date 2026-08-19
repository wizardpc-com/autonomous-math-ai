# Formalization policy

Use Lean 4 with Mathlib when one or more conditions hold:

- The result is a central new theorem or lemma.
- A subtle quantified or boundary argument is hard to audit informally.
- A computer-assisted reduction needs a trustworthy logical bridge.
- The result is approaching publication.
- Formal feedback would materially help proof search.

Do not formalize every exploratory lemma and do not use Lean merely because it is installed.

Follow this escalation order:

`falsify -> exact test -> independent check -> stabilize statement -> informal proof -> adversarial review -> formalize`

Before formalizing, freeze the intended natural-language statement, variables, domains, assumptions, edge conventions, and equivalences. After formalizing, audit the Lean theorem against that frozen statement separately from checking that Lean compiles.

Record the Lean version, Lake version, Mathlib revision, project or toolchain files, build/check command, source files, admitted declarations (`sorry`, axioms, or placeholders), and kernel result. Do not create or build a large Mathlib project solely to test whether Lean is installed.

A one-shot worker may compile or check one specified Lean file. It may not redesign the formal statement, choose a proof strategy, invent auxiliary lemmas, or continue from compiler feedback into an unassigned proof search. Those decisions return to the controller as `BLOCKED`.
