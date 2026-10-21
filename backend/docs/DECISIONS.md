# Architecture Decisions

Decisions 1 to 4 cover authentication. Decisions 5 to 18 cover the agent runtime.

## 1. Refresh tokens live in Redis, not Postgres
**Decision:** One Redis hash per login session (`refresh:{user_id}:{sid}`), plus a set
indexing each user's sessions. TTL = token lifetime.
**Why:** Any server instance can validate sessions, so the API scales horizontally.
Expiry is handled by Redis itself. Multi-device login and "logout everywhere" are cheap.
**Trade-off:** Sessions are lost if Redis loses data (users log in again). Acceptable,
since nothing permanent lives there.

## 2. Refresh rotation with reuse detection
**Decision:** Every refresh replaces the stored token hash atomically (Lua script).
Presenting an already-rotated token revokes all of that user's sessions.
**Why:** A stolen token that is replayed later is detected, not silently accepted.
**Trade-off:** The legitimate user is also logged out when reuse is detected.

## 3. Session id (`sid`) inside the access token
**Why:** `get_current_user` checks the session still exists, so logout and
revocation take effect immediately instead of waiting 15 minutes for expiry.
**Trade-off:** One Redis lookup per authenticated request.

## 4. bcrypt runs in a thread
**Why:** bcrypt is blocking; calling it directly inside `async def` would freeze every other request.

## 5. The model chooses tool calls; plain code runs them
**Decision:** `choose_tool` must return exactly one tool call (`tool_choice="any"`), and a
plain `ToolNode` executes it. The repo, branch and file tree come from the run state
(`InjectedState`), not from the model.
**Why:** The model can only ask for the tools that exist, and only for paths inside this
repo. Tools can be tested without a model.
**Trade-off:** One more hop per step than a single node that thinks and acts.

## 6. Stop conditions are code, not model judgment
**Decision:** In `evaluate`, the step limit and the token budget are checked first, then the
model's own "done". LangGraph's `recursion_limit` is a second safety net.
**Why:** A confused model that always says "read more" must not be able to loop forever or
burn the token budget.
**Trade-off:** A run can stop at a limit with partial coverage. The report says so
(`stop_reason` and its limitations).

## 7. State carries findings, not file contents
**Decision:** After a file part is summarized, its text in the message history is replaced
by a short stub that keeps the "characters a-b of N" header.
**Why:** Token use stays roughly flat instead of growing with every file read.
**Trade-off:** The report is written from short summaries, never from the code itself. It can
miss detail, and for well-known libraries the model can fill gaps from memory. The report
lists which files were read and its own limitations.

## 8. Files are read in chunks, with search
**Decision:** `read_repository_file` takes an offset and says where the chunk ended.
`search_in_file` returns line numbers and character offsets.
**Why:** With a fixed character cap, the agent looped on a large file: it knew the part it
needed was cut off and had no way to get it. The model had even asked for a search tool
that did not exist.
**Trade-off:** Each chunk is a new GitHub request for the same file (no cache yet).

## 9. A file part counts as read only when it produced a finding
**Decision:** Repeat reads are blocked per chunk (`file@offset`), and `files_read` only
lists files that produced a finding.
**Why:** A failed evaluation must not mark a file as done, and reading the next chunk of
the same file must stay allowed.
**Trade-off:** If evaluation keeps failing for a file, the agent can try it again.

## 10. Findings and the report use plain JSON, validated with Pydantic
**Decision:** The model is asked for one JSON object and the reply is validated, instead of
using the provider's structured-output feature.
**Why:** That feature works through a hidden tool call, and this model sometimes invented
the tool name, so the whole response was rejected. At temperature 0 a retry with the same
input fails the same way, so every retry changes the prompt.
**Trade-off:** More code to extract and validate JSON, and a fallback report when parsing fails.

## 11. Failures are classified, and each class has its own behavior
**Decision:**
- GitHub: retry 5xx and network errors 3 times with backoff, never retry 4xx, and map
  404, 403, 429 and 409 to specific errors.
- LLM: on a 429, wait the time the provider asks for, up to 4 attempts, then fail the run
  with a "busy" message that says it can be resumed.
- A model that keeps failing to pick a tool ends the loop with a partial report instead of a crash.
- Unexpected errors are logged in full and shown to the user as a generic message.

**Why:** Retrying a 404 is pointless, and a rate limit is not a bug. In early traces the
slow model calls were waits for the provider's token limit: the server log showed 429
responses followed by SDK retries of 16, 22 and 44 seconds.
**Trade-off:** Waiting makes a run slow instead of failing fast.

## 12. Checkpoints live in Postgres, not Redis
**Decision:** LangGraph's Postgres checkpointer, with `thread_id` = the investigation id,
opened in one function (`checkpoint.py`).
**Why:** Sessions in Redis (decision 1) can be lost because the cost is a re-login. A lost
checkpoint costs a run that cannot be resumed: minutes of work and paid model calls.
Postgres is durable without extra configuration and can be inspected with SQL.
**Trade-off:** A database write per node, which is visible latency on a remote database.
Redis is usually faster per write. Because the choice is isolated in one function,
switching later means changing that function.

## 13. Trace and checkpoint are separate
**Decision:** `trace_steps` is an append-only log for people. Checkpoints are snapshots for
the machine.
**Why:** They need different data. The trace keeps previews, timings and the agent's
reasoning, never whole files. A checkpoint needs the full state and nothing about timing.
**Trade-off:** Two writes per node.

## 14. Resume is on request and claimed atomically
**Decision:** On startup, rows still `pending` or `running` become `interrupted`.
`POST /investigations/{id}/resume` claims the run with
`UPDATE ... WHERE status IN ('interrupted', 'failed')` and continues from the last
checkpoint with `None` as the input.
**Why:** Resuming automatically at startup would spend tokens nobody asked for and could
resume the same run from two instances. One `UPDATE` lets the database decide which of two
requests wins.
**Trade-off:** A user has to click resume. A repeated request a moment later is tested (it
gets 409). Two requests at exactly the same instant rely on the `UPDATE` being atomic and
are not load-tested.

## 15. Runs are background tasks in the API process
**Decision:** FastAPI `BackgroundTasks`, with the status and trace in the database as the
shared record between the request and the task.
**Why:** It is the simplest thing that returns `202` immediately and lets a client poll. The
task opens its own database session because the request's session is closed by then.
**Trade-off:** A restart kills running tasks (resume recovers them), and the startup cleanup
assumes a single server process. A queue with a separate worker is the next step.

## 16. Schema changes go through Alembic only
**Decision:** The checkpoint tables are created by LangGraph and ignored by Alembic
(`include_object` in `env.py`).
**Why:** Dropping tables by hand once left `alembic_version` pointing at a migration the
database no longer matched, and it had to be repaired with `alembic stamp`. One owner per
table avoids that.
**Trade-off:** Two systems create tables, so the filter has to stay correct.

## 17. Someone else's investigation and a missing one both return 404
**Why:** Returning 403 would confirm that the id exists.
**Trade-off:** Slightly harder to debug.

## 18. The report separates facts from model text
**Decision:** `findings`, `coverage` and the repo info are added by code. File paths that the
model lists for a section are dropped if those files were not read.
**Why:** Anything that can be computed should not be written by a model, and a reader should
see exactly which evidence was used.
**Trade-off:** The model-written part can still contain unsupported detail (see decision 7).