---
name: write-fast-clickhouse-sql
description: Rules to write ClickHouse SQL that is fast and never refused by the cost guard.
when_to_use: "Hand-written SQL on large tables, a query refused as too_expensive, slow queries, timeouts, optimisation."
---
1. Filter on the partition/time column (orders.order_date, web_events.event_date) with a
   closed range, and on a prefix of the ORDER BY key when possible.
2. Select only needed columns; aggregate in SQL; use LIMIT for top-N with ORDER BY.
3. Approximate when allowed: `uniq` over `uniqExact`, `quantileTDigest`, and `SAMPLE 0.1` on
   tables with a sampling key (web_events). Say so in the answer.
4. Conditional aggregates (`countIf`, `sumIf`, `avgIf`) instead of self-joins or UNIONs.
5. JOIN: smaller table on the right; pre-aggregate or pre-filter the right side in a subquery;
   prefer `IN (subquery)` for semi-joins.
6. Avoid `FINAL`, `LIKE '%x%'` on big tables without another selective filter, and
   `ORDER BY` on huge un-aggregated sets.
7. Use `explain_query` to see partitions/granules selected; aim for a small fraction.
