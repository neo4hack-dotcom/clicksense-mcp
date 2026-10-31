---
id: revenue-rules
title: Revenue calculation rules
tags: [revenue, turnover, finance, vat, sales]
applies_to: [shop.orders]
---
Official revenue (executive reporting) is the sum of `amount_eur` over lines with
`status = 'completed'`. `pending` orders are never counted; `refunded` lines are excluded from
revenue but tracked separately by Finance.

Amounts **include VAT**. Net amounts are approximated as `amount_eur / 1.2` (French VAT); for
exact per-country net amounts, ask Finance for the VAT table (not in ClickHouse yet).

Revenue is attributed to the **order date** (`order_date`), not the delivery date.
