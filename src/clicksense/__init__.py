"""ClickSense: a ClickHouse MCP server with a semantic layer and a guarded executor.

Map of the package (read in this order to understand it):

  config.py     every setting, documented; .env and environment overrides
  server.py     the MCP surface: tools, resources, prompts, skills, HTTP auth, CLI `clicksense`
  service.py    what each tool does (plain Python, testable without MCP)
  knowledge.py  loads semantic/ (dictionary, glossary, metrics, examples, knowledge, skills) + search
  catalog.py    live schema from ClickHouse merged with the dictionary
  guard.py      decides whether a SQL statement may run, and caps its rows
  executor.py   the only code that talks to ClickHouse: limits, breaker, rate, cache
  resolver.py   maps user wording to stored values ("France" -> 'FR')
  lint.py       findings on a query before/after it runs (unknown columns, wrong literals, perf)
  metrics.py    compiles governed metrics and periods into SQL
  errors.py     the error type and the ClickHouse error translation table
  audit.py      JSONL audit log
  agent.py      bridge to any OpenAI-compatible LLM; chat.py is its CLI `clicksense-chat`
  kbtools.py    validation / scaffolding / bootstrap of semantic/; admin.py is `clicksense-admin`
  templates/    commented templates used by `clicksense-admin kb new`
"""

__version__ = "1.0.0"
