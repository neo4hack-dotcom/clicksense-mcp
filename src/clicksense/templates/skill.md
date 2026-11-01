---
name: {{name}}
# description: one sentence. Clients read it to decide when to load the skill: say what it is for.
description: TODO What this playbook does.
# when_to_use: the kinds of requests that should trigger it, in the words users actually use
# (several languages are fine). It is indexed by the search.
when_to_use: TODO e.g. weekly sales review, compare two periods, explain a KPI change
---
TODO Numbered steps the agent follows, each naming the tool to call and what to check:

1. Call `get_context` with the question. Settle any ambiguity with `ask_user_to_clarify`.
2. ...
3. Answer with the figures, the definitions used (metric, filters, period) and the caveats.
