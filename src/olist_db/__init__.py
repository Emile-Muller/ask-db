"""Builds data/olist.duckdb from the Olist CSVs (``python -m olist_db.build``).

Kept out of ``text2sql`` on purpose: this is the only code that opens the database for
writing, and it runs fixed, trusted SQL only, never user- or LLM-generated SQL.
"""
