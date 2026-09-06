SELECT
    ri.recipe_id,
    r.recipe_code,
    r.recipe_name,
    ri.inventory_code,
    ri.item_name,
    ri.uom          AS recipe_line_uom,
    i.uom_group     AS ingredient_master_uom_group,
    i.standard_uom  AS ingredient_master_standard_uom
FROM recipe_ingredients ri
JOIN recipes r      ON r.id = ri.recipe_id
JOIN ingredients i  ON i.ingredient_code = ri.inventory_code
WHERE
    -- ingredient master says Mass (weight), line says a volume unit
    (LOWER(i.uom_group) = 'mass'
     AND LOWER(TRIM(ri.uom)) IN ('ml','milliliter','milliliters','l','liter','liters','litre','litres','cl'))
    OR
    -- ingredient master says Volume, line says a weight unit
    (LOWER(i.uom_group) = 'volume'
     AND LOWER(TRIM(ri.uom)) IN ('g','gram','grams','kg','kilogram','kilograms','mg'))
ORDER BY r.recipe_code, ri.line_no;

