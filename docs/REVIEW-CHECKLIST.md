# The review checklist

A review is a list of findings, and a list of findings is alive until
somebody says it is closed. This file is that list: every finding of the
four numbered review waves, what it was, and where the answer lives.
(The fifth wave — the audit of v2.14.8 — found nothing new; its report
is `REVIEW-v2.14.8.md`.)

The rule this file follows: **a finding is closed when something checks
it, not when a note says so.** The "check" column names the thing that
would go red if the finding came back.

| wave | # | what it was | closed by | the check that would catch it |
|---|---|---|---|---|
| 1 (v2.14.3) | R1 | the file door measured the path and served the descriptor's bytes | v2.14.5 | `tests/test_file_door_v2145.py` |
| 1 | R2 | a dead session came back in the workspace root | v2.14.5 | `tests/test_session_cwd_v2145.py` |
| 1 | R3 | a bad command reached the model as `ValidationError` | v2.14.5 | `tests/test_review_kwen_v2141.py::TestTheCommandIsRefusedLegibly` |
| 1 | R4 | one operator in the tree could never run | v2.14.5 | `tests/test_tree_hygiene_v2145.py` |
| 1 | R5 | test classes that drive tmux did not say so | v2.14.5 | the CI job "The suite is green without tmux" + `tests/test_ci_without_tmux_v2146.py` |
| 1 | R6 | the MCP tool asked tmux on every call | v2.14.5 | `tests/test_mcp_session_cost_v2145.py` |
| 2 (v2.14.4) | S1 | the heal could kill a working command | v2.14.5 (the marker), v2.14.8 (the shell's own prompt) | `tests/test_stuck_session_v2144.py` + `tests/test_bare_prompt_v2148.py` |
| 2 | S2 | the production posture was not named | v2.14.5 | the release notes + `prod-verify.sh` |
| 2 | S3 | "not valid shell syntax" for an unfinished construct | v2.14.5 | `tests/test_stuck_session_v2144.py` |
| 2 | S4 | the suite killed the tmux sessions of other servers | v2.14.5 | `tests/test_suite_hygiene_v2145.py` |
| 2 | S5 | the mutation count was quoted, not counted | v2.14.5 | the scripts' own counters |
| 3 (v2.14.5) | T1 | the CI job "without tmux" ran the suite *with* tmux | v2.14.6 | `tests/test_ci_without_tmux_v2146.py` |
| 3 | T2 | the protocol's marker reached the caller's stdout | v2.14.6 | `tests/test_stdout_marker_v2146.py` |
| 3 | T3 | the limit was checked after the file was opened | v2.14.6 | `tests/test_file_door_order_v2146.py` |
| 4 (v2.14.7) | U1 | a mutation script counted a catch for a test that never ran | v2.14.8 | `tests/test_mutation_scripts_run_v2148.py` |
| 4 | U2 | the shell's own `>` was read as a continuation prompt | v2.14.8 | `tests/test_bare_prompt_v2148.py` |
| 4 | U3 | the false "101 mutations" stayed in the section that said it | v2.14.8 | `tests/test_changelog_correction_v2148.py` |

## How to use it

When a review arrives, **add a row** — do not write a paragraph about
it in the chat and let the list stay where it was. A finding that is
not in this file is a finding that the next wave will rediscover.

A row is closed when the "check" column names something that **can go
red**. A row whose check is "the code looks right" is not closed; it is
a hope with a table around it.

## The numbers in this file

Nothing here is a hand-written sum. The test counter is the `unittest`
count (the drift tripwire reads it), and the mutation count is what the
mutation scripts print when they run. A number that somebody wrote down
and nobody recomputed is a number that rots — it happened twice, with
the same number and in both directions.
