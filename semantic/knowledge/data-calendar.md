---
id: data-calendar
title: Data freshness and calendar
tags: [freshness, refresh, today, yesterday, latency, calendar]
applies_to: [shop.orders, shop.web_events]
---
`shop.orders` is loaded once a day around 06:00 (Europe/Paris) with the complete previous day:
a question about "today" has no reliable answer before the next morning.
`shop.web_events` is fed in near real time (a few minutes of delay).

Weeks start on Monday (`toMonday`). Months are calendar months.
Check with `check_freshness` before answering about yesterday or today.
