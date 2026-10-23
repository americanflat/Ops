-- Shopify units sold per SKU per week (Monday start) since 2025-12-01, with each SKU's product title.
SELECT FORMAT_DATE("%Y-%m-%d", DATE_TRUNC(DATE(order_created_at, "America/New_York"), WEEK(MONDAY))) AS week,
  sku, ANY_VALUE(title) AS product, SUM(quantity) AS units
FROM `americanflat.shopify.order_line_items`
WHERE order_created_at >= "2025-12-01" AND COALESCE(sku, "") != ""
GROUP BY 1, 2
ORDER BY 1, 2
