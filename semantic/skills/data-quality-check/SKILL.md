---
name: data-quality-check
description: Assess whether a table or column can be trusted before using it.
when_to_use: "Suspicious numbers, a new table, nulls or duplicates suspected, reliability checks before a key report."
---
1. `describe_table`: grain, keys, caveats, refresh cadence.
2. `check_freshness`: latest timestamp versus expected refresh.
3. For each key column, `profile_column`: null rate, empty strings, distinct count versus rows
   (a key should be ~unique), min/max (dates in the future? negative amounts?), top values
   (a placeholder like 'N/A' or 0 dominating?).
4. Duplicates on the declared grain:
   `SELECT <grain cols>, count() c FROM t WHERE <recent period> GROUP BY <grain cols> HAVING c > 1 LIMIT 20`.
5. Referential integrity on joins, on a bounded period:
   `SELECT count() FROM orders o LEFT ANTI JOIN customers c ON o.customer_id = c.customer_id WHERE o.order_date >= today() - 30`.
6. Report findings with severity and record durable ones with `suggest_knowledge` (kind: caveat).
