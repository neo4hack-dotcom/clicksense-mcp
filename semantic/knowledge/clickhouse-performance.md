---
id: clickhouse-performance
title: Writing fast ClickHouse SQL on this warehouse
tags: [performance, sql, clickhouse, optimisation, index, slow]
---
- Always filter `shop.orders` on `order_date` and `shop.web_events` on `event_date`: they are
  the (monthly) partition keys and the start of the ORDER BY, so ClickHouse skips partitions
  and granules outside the period.
- Aggregate in ClickHouse (`count()`, `sum()`, `uniq()`, `quantile()`) instead of fetching rows.
- `uniq()` is approximate (~1%) and very fast; `uniqExact()` when exactness matters
  (official metrics).
- `countIf(cond)` / `sumIf(x, cond)` avoid several subqueries.
- JOIN: ClickHouse builds the hash table from the **right** side, in memory. Put the small table
  on the right (`orders JOIN products`, never the other way round).
- `shop.web_events` supports `SAMPLE 0.1` for fast exploration (multiply counts by 10).
- Avoid `SELECT *`: it is a column store, every column read costs.
