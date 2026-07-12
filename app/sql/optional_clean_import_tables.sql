-- Optional reset before a clean re-upload.
-- Use only if you want to remove imported master/recipe data and upload again.
-- This keeps users, companies, roles, permissions and production transactions.
SET FOREIGN_KEY_CHECKS = 0;
TRUNCATE TABLE recipe_ingredients;
TRUNCATE TABLE recipes;
TRUNCATE TABLE ingredients;
TRUNCATE TABLE customers;
TRUNCATE TABLE suppliers;
TRUNCATE TABLE chefs;
TRUNCATE TABLE brands;
TRUNCATE TABLE revenue_streams;
TRUNCATE TABLE kitchen_locations;
TRUNCATE TABLE master_records;
SET FOREIGN_KEY_CHECKS = 1;
