
SELECT
    a.company_id,
    a.customer_code  AS code_a,
    a.customer_name  AS name_a,
    b.customer_code  AS code_b,
    b.customer_name  AS name_b,
    (SELECT COUNT(*) FROM customer_orders co WHERE co.customer_code = a.customer_code) AS orders_on_a,
    (SELECT COUNT(*) FROM customer_orders co WHERE co.customer_code = b.customer_code) AS orders_on_b
FROM customers a
JOIN customers b
  ON a.company_id = b.company_id
 AND a.customer_code < b.customer_code   -- each pair once, not twice
 AND LOWER(TRIM(REGEXP_REPLACE(a.customer_name, '[[:space:]]+', ' ')))
   = LOWER(TRIM(REGEXP_REPLACE(b.customer_name, '[[:space:]]+', ' ')))
ORDER BY a.company_id, a.customer_name;
