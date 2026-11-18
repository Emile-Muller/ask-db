# CLAUDE.md

Text-to-SQL app: natural language → SQL (via LLM) → DuckDB → results in Streamlit.
Dataset: Olist Brazilian E-Commerce (see `data/data_source.md`).

## Safety rules (must follow)

### 1. No secrets
- Never hardcode API keys, tokens or passwords in code, commits, logs or docs.
- Load secrets from environment variables or `.streamlit/secrets.toml` / `.env`.
- Keep `.env` and `.streamlit/secrets.toml` in `.gitignore`.

### 2. Database is read-only
- Always open DuckDB in read-only mode: `duckdb.connect(path, read_only=True)`.
- Generated SQL must only read data (`SELECT` / `WITH`). Reject anything else
  (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `ATTACH`, `COPY`, `PRAGMA`, `INSTALL`, `LOAD`, etc.).
- Allow a single statement per query (no `;`-chained statements).
- Never run LLM-generated SQL on a writable connection, even for testing.
- Treat all user input and LLM output as untrusted.
