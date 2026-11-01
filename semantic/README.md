# The semantic layer

Everything the agent knows about your data, besides what ClickHouse itself says. Plain files:
edit them, save, and the running server picks the change up within a few seconds (no restart).
The step-by-step guide is [docs/UPDATING.md](../docs/UPDATING.md).

| Path | What it holds | Add one with |
|---|---|---|
| `dictionary/<database>.yaml` | Tables and columns: meaning, units, PII, stored values and the words users say for them, joins, dimensions, caveats | `clicksense-admin kb bootstrap db.table` |
| `metrics.yaml` | Governed metrics: the official formula, filters, allowed dimensions | `clicksense-admin kb new metric NAME` |
| `glossary.yaml` | Business terms; a term with several `senses` is ambiguous and makes the agent ask | `clicksense-admin kb new term "WORD"` |
| `examples.yaml` | Verified question -> SQL pairs, used as models by the agent | `clicksense-admin kb new example "QUESTION"` |
| `knowledge/*.md` | Business rules and conventions, one topic per file | `clicksense-admin kb new knowledge NAME` |
| `skills/<name>/SKILL.md` | Playbooks for recurring analyses (Agent Skills format) | `clicksense-admin kb new skill NAME` |
| `inbox/` | Suggestions written by the agent, waiting for a person | `clicksense-admin kb inbox` |

After every change:

```bash
clicksense-admin kb validate --live      # structure, typos, references, and SQL against ClickHouse
clicksense-admin kb search "a question"  # what the agent will now find for it
```

The commented templates behind `kb new` are in `src/clicksense/templates/`: they document every
key each kind of entry understands.
