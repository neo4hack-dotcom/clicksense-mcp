---
name: investigate-metric-change
description: Find what drives a rise or drop of a metric between two periods.
when_to_use: "Why did revenue drop or rise, what explains a change, variance analysis, anomaly, decline, increase."
---
1. Pin the metric (`list_metrics`) and the two periods (current vs comparison, same length;
   prefer week-over-week or year-over-year for seasonality).
2. Confirm the change: `query_metric` with each period, no dimension. Compute delta and %.
   Rule out data freshness first (`check_freshness`): a partial last day looks like a drop.
3. Decompose: run `query_metric` for both periods by each allowed dimension (channel, country,
   category, segment...). For each dimension value compute contribution = delta of that value /
   total delta. Rank by absolute contribution.
4. Split volume vs value: compare `orders` and `average_order_value` to see if the change comes
   from fewer orders or smaller baskets.
5. Drill down only into the top 1-2 contributors (combine two dimensions, or a finer grain).
6. Check mix and data effects: new/removed category, status changes (`cancellation_rate`),
   a country with abnormal values (`profile_column`).
7. Report: headline change, top 3 drivers with their share of the change, what is ruled out,
   and the queries used.
