---
# id: the document id (defaults to the file name). title: shown in search results.
# tags: words that help find it. applies_to: tables this rule concerns (listed by describe_table).
id: {{name}}
title: TODO Short title
tags: []
applies_to: []
---
TODO Write the rule as you would explain it to a new analyst: what is true, why, and how to
apply it in a query. Short paragraphs; the agent receives the paragraph that best matches the
question, so each paragraph should stand on its own.

Example of a good rule: "Revenue is attributed to the order date, not the delivery date. For a
monthly revenue, group by toStartOfMonth(order_date)."
