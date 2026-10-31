---
name: disambiguate-request
description: Turn a vague request into precise, checkable query parameters.
when_to_use: 'Vague or ambiguous requests ("good customers", "recently", "sales"), values in user wording, several possible columns.'
---
1. List the parts of the request: measure, entity, filters, period, grain.
2. Measure: `search_knowledge` (kinds: metric, term). Several senses -> ask.
3. Entity/column: `find_columns` for each business word; `ambiguous: true` -> ask or decide from
   the glossary, and state the choice.
4. Values: `resolve_values` for each literal (or `locate_value` if the column is unknown).
   `resolved` -> use the value and mention the mapping; `ambiguous`/`not_found` -> ask with the
   candidates (`ask_user_to_clarify`).
5. Period: vague words ("recently", "this year") -> propose an explicit range.
6. Restate the precise request in one sentence before querying.
