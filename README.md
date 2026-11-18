# Agentic Query

Ask questions in plain English, get SQL and results.

An AI model turns natural language into a SQL query, runs it against a DuckDB database, and shows the query and its results in a Streamlit app.

## Stack

- **Frontend:** Streamlit
- **Database:** DuckDB
- **AI:** LLM for text-to-SQL
- **Data:** [Brazilian E-Commerce Public Dataset by Olist](data/data_source.md) (Kaggle, ~100k orders, 2016–2018)

## Example

> "What are the top 5 product categories by revenue?"

The app generates the SQL, runs it, and displays the table.

## Data

CSV files live in `data/`. See [data/data_source.md](data/data_source.md) for source, license and table descriptions.

> **Note:** the original `olist_products_dataset.csv` misspells two columns (`product_name_lenght`, `product_description_lenght`). The header has been fixed locally to `product_name_length` and `product_description_length`. If you re-download the dataset, fix the header again before building the database.

## Setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Database

`build_db.py` loads every CSV in `data/` into `data/olist.duckdb`, one table per file, named after the file (e.g. `olist_products_dataset.csv` → table `olist_products_dataset`). Re-run it whenever the CSVs change.

```powershell
.\.venv\Scripts\python.exe build_db.py
```

`db.py` provides read-only access to the database:

- `get_connection()`: opens `data/olist.duckdb` with `read_only=True`. All queries go through this connection; only `build_db.py` writes to the database.
- `first_products(n=5)`: returns the first `n` rows of `olist_products_dataset` as a list of dicts.

```powershell
.\.venv\Scripts\python.exe db.py   # prints the first 5 products
```
