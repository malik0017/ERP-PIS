-- =============================================================================
-- Batch 176/182 — audit_recipe_category_coverage.sql
-- -----------------------------------------------------------------------------
-- Answers "is the Section Report actually dropping ingredient categories, or
-- is a category filter just left applied?" (Img 5: "include vegetable,
-- chicken, beef... all recipe ingredient").
--
-- bom_lines has no sub-recipe concept — every row is already a flattened,
-- real ingredient with its own category tags (ingredient_category,
-- ingredient_main_category, ingredient_sub_category), written at BOM
-- generation time. So the Section Report itself cannot silently drop a
-- category that exists in bom_lines — the only way a category goes missing
-- on screen is the "Recipe Category" filter dropdown being set to something
-- other than "All categories".
--
-- This query shows you the actual category breakdown straight from
-- bom_lines for one recipe (or order), so you can compare it directly
-- against what the Section Report shows with "All categories" selected.
-- If they match, the report is fine — the earlier screenshot's report just
-- had a filter applied, or the recipe genuinely doesn't use chicken/beef on
-- that particular order. If they DON'T match, that's a real BOM-generation
-- gap, not a report bug, and worth flagging with the specific recipe below.
--
-- HOW TO USE
--   Fill in ONE of :recipe_no or :order_no below (comment out the other's
--   WHERE line), then run.
-- =============================================================================

SELECT
    bl.order_no,
    bl.recipe_no,
    bl.recipe_name,
    COALESCE(bl.ingredient_main_category, '(uncategorized)') AS main_category,
    COALESCE(bl.ingredient_sub_category, '(uncategorized)')  AS sub_category,
    bl.ingredient_code,
    bl.ingredient_name,
    bl.total_required_with_waste_standard AS qty,
    bl.standard_uom
FROM bom_lines bl
WHERE bl.recipe_no = 'RCP-FRSH-00015'   -- <-- replace with the recipe you're checking
-- WHERE bl.order_no = 'ORD-20260905-0001'  -- <-- or check one whole order instead
ORDER BY main_category, sub_category, bl.ingredient_name;


-- ---------------------------------------------------------------------------
-- SUMMARY VIEW — one row per category actually present for that recipe/order,
-- so you can eyeball "does this include Produce, Meat/Chicken, Dairy... or
-- really just pantry items" without scrolling a long ingredient list.
-- ---------------------------------------------------------------------------
SELECT
    COALESCE(bl.ingredient_main_category, '(uncategorized)') AS main_category,
    COUNT(*) AS ingredient_lines,
    GROUP_CONCAT(DISTINCT bl.ingredient_name ORDER BY bl.ingredient_name SEPARATOR ', ') AS ingredients
FROM bom_lines bl
WHERE bl.recipe_no = 'RCP-FRSH-00015'   -- <-- same recipe/order as above
GROUP BY main_category
ORDER BY main_category;
