---
name: answer-data-question
description: Standard workflow to answer a business question with correct, verifiable numbers.
when_to_use: "Any question that needs a number, a list or a trend from the warehouse: how many, how much, evolution, breakdown, top N."
---
1. **Context.** Call `get_context` with the question verbatim. Read `tables`, `metrics`,
   `values_spotted`, `ambiguities`, `examples` and `knowledge`.
2. **Ambiguity.** For each entry in `ambiguities` (e.g. "sales" = revenue, orders or units;
   "country" = delivery or customer country), decide from the question; if the question does not settle it,
   call `ask_user_to_clarify` with the senses as options. Never choose silently.
3. **Period.** If the question has no period, say which one you use (default: last 30 days)
   rather than querying all history. For "today/yesterday", call `check_freshness` first.
4. **Metric first.** If a governed metric matches, use `query_metric` (dimensions, filters in the
   user's words, period, grain). Report `value_resolutions` if any.
5. **Otherwise SQL.** `describe_table` for exact names, `resolve_values` for each literal,
   start from the closest verified example, `validate_query`, then `run_query`.
6. **Check the result.** `truncated: true` means incomplete: aggregate instead. 0 rows: read
   `notes` (wrong literal?) before concluding "none". Sanity-check magnitudes.
7. **Answer.** Give the number(s), then the definition used (metric, filters, period, TTC/HT),
   and caveats from the dictionary. Offer the SQL if useful. If you discovered a reusable
   query or a definition, `suggest_knowledge`.
