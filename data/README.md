# Data Source

**Brazilian E-Commerce Public Dataset by Olist** (Kaggle)

- **Link:** https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce
- **Size:** ~100,000 orders with product, customer and review info
- **Period:** 2016 – 2018
- **License:** CC BY-NC-SA 4.0

## About

A public dataset of orders made at the Olist Store across multiple Brazilian marketplaces. Each order can be viewed from several angles: order status, price, payment and freight performance, customer location, product attributes, and customer reviews. A geolocation dataset maps Brazilian zip codes to lat/lng coordinates.

This is real commercial data, anonymised. References to companies and partners in review texts were replaced with names of Game of Thrones great houses.

## Context

Olist is the largest department store in Brazilian marketplaces. It connects small businesses across Brazil to sales channels with a single contract. Merchants sell through the Olist Store and ship directly to customers via Olist logistics partners.

After a purchase, the seller is notified to fulfill the order. Once the customer receives the product (or the estimated delivery date passes), they receive a satisfaction survey by email to rate the experience and leave comments.

## Things to know

- An order can have multiple items.
- Each item can be fulfilled by a different seller.
- Store and partner names in text were replaced with Game of Thrones great houses.

## Files

Download the CSVs from Kaggle into `data/raw/` (not tracked by git), then run
`python -m olist_db.build` to build `data/olist.duckdb`. Use the files as downloaded: the
build renames the misspelled `product_name_lenght` / `product_description_lenght` columns
of `olist_products_dataset.csv` to `*_length`. Table and column
descriptions used in the prompt live in `data/schema.yaml`.

| File | Description | Key columns |
|---|---|---|
| `olist_orders_dataset.csv` | Orders (central table) | `order_id`, `customer_id` |
| `olist_order_items_dataset.csv` | Items in each order | `order_id`, `product_id`, `seller_id` |
| `olist_order_payments_dataset.csv` | Payments per order | `order_id` |
| `olist_order_reviews_dataset.csv` | Customer reviews | `review_id`, `order_id` |
| `olist_customers_dataset.csv` | Customers | `customer_id`, `customer_zip_code_prefix` |
| `olist_sellers_dataset.csv` | Sellers | `seller_id`, `seller_zip_code_prefix` |
| `olist_products_dataset.csv` | Products | `product_id`, `product_category_name` |
| `olist_geolocation_dataset.csv` | Zip code → lat/lng | `geolocation_zip_code_prefix` |
| `product_category_name_translation.csv` | Category names PT → EN | `product_category_name` |

## Acknowledgements

Thanks to Olist for releasing this dataset.
