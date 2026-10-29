---
name: intandem-planner
description: Drive an OpenCode implementer through inTandem - write a spec, submit it, wait, review the result against the files, and correct or move on. Use when a task is delegated to the inTandem implementer, or when asked to check on, unblock, or review its work.
---

# Planning for a inTandem implementer

You write specs and review results. The implementer, a cheaper model in OpenCode, writes the
code. Do not do its work for it, and do not take its word for anything.

If inTandem's MCP tools are available, use them instead of the commands below; the loop is the
same. `submit_spec`, `wait`, `get_result`, `get_pending`, `reply_permission`,
`answer_question`, `send_directive` and `finish` map onto `spec`, `wait`, `result` and `diff`,
`pending`, `allow`/`deny`, `answer`, `directive` and `finish`. Give the human the `dashboard`
link from the first reply.

## The loop

1. **Write the spec to a file.** One outcome, the files it may touch, how to check it is done.
   Precise but not contradictory: a spec that contradicts itself does not fail, it stalls.
2. **Submit it.** `inTandem spec S-<n> --file spec.md`. Spec IDs are immutable; a changed
   requirement is a new ID.
3. **Wait.** `inTandem wait --timeout 900`, and branch on the exit code:

   | exit | meaning | next |
   |---|---|---|
   | 0 `result` | it wrote a result and the turn is over | step 4 |
   | 0 `turn_ended` | it stopped without a result | `inTandem msg 3`, then `say` or a directive |
   | 5 `blocked` | a permission or question is pending | `inTandem pending`, then decide |
   | 8 `reasoning_stall` | thinking, not acting | `inTandem msg 3`; look for a contradiction in your spec |
   | 4 `hung` | nothing arriving | `inTandem status`; abort and re-prompt if it is stuck |
   | 6 | provider error or protected file changed | `inTandem status`, `inTandem log 20` |
   | 124 | still working | wait again |

4. **Review against the files, not the summary.** `inTandem result --json` is a claim.
   Check it: `inTandem diff`, read the changed files, run the tests yourself. Every
   self-verdict checked so far has been optimistic at least once.
5. **Correct or move on.** A fix to this spec: `inTandem directive S-<n> D-<m> correct --file fix.md`.
   Wrong spec: `inTandem directive S-<n> D-<m> stop "..."`, then a new spec. Done: next spec.

## Releasing a blocked session

- `inTandem allow` approves once. Prefer it to `always`, which approves for the whole session.
- `inTandem deny --message "why"` rejects with feedback; the model carries on with it.
- `inTandem answer <label>` picks an option for a pending question.
- An edit to a protected file is never yours to approve. Deny it.

## When it stalls

A reasoning stall is a bug report about your spec more often than about the model. Read the
last messages before anything else. Restarting throws the reasoning away and walks it into the
same wall; fixing the contradiction does not.

## Ground rules

- Never edit protected files to make its work pass.
- Paste command output into reviews; do not summarize it.
- `inTandem finish` when the task is done. It captures evidence on the way out.
