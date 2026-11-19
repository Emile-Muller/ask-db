"""text2sql: a 5-layer safe text-to-SQL pipeline.

The LLM only *proposes* SQL. Whether SQL runs is decided by deterministic code:
layer 3 (``validator``) and layer 4 (``executor``) are the security boundary.
"""
