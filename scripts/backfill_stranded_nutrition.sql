-- =============================================================================
-- Batch 176/178/182 — backfill_stranded_nutrition.sql  (176-E2)
-- -----------------------------------------------------------------------------
-- Fixes the DATA left behind by the bug fixed in Batch 178: every section
-- transfer creates a NEW kitchen_section_transactions row for the next
-- section, but carb_g/protein_g/vegetable_g/yield_g/produced_portion/
-- portion_weight_g/output_uom/byproduct_qty_standard were never copied onto
-- it — they stayed on the OLD, now-locked ("Transferred") row forever.
-- transfer_transaction() now copies them forward on every NEW transfer
-- (Batch 178/180); this script is for transfers that already happened
-- BEFORE that fix, where the values are still sitting on the old row.
--
-- Safe to run any time: it only fills a NEW row's nutrition columns when
-- they are all still NULL, and only from an old row that actually has a
-- value to give. It never overwrites a value that's already there, so
-- running it twice, or after Batch 178 is live, changes nothing further.
--
-- HOW TO USE
--   1. Run the PREVIEW query first. Check the row count and a sample of
--      what would change.
--   2. Take a backup (mysqldump the kitchen_section_transactions table, or
--      your usual full backup) — this only fills NULLs, but any UPDATE
--      deserves a backup on principle.
--   3. Run the UPDATE.
--   4. Re-run the PREVIEW query — it should now return 0 rows.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- STEP 1 — PREVIEW: which new-section rows are still missing nutrition data
-- that exists on the old row they came from.
-- ---------------------------------------------------------------------------
SELECT
    new_tx.id            AS new_row_id,
    new_tx.order_no,
    new_tx.recipe_no,
    new_tx.recipe_name,
    new_tx.ingredient_code,
    new_tx.current_section  AS now_sitting_in,
    old_tx.id            AS old_row_id,
    old_tx.current_section  AS came_from,
    old_tx.carb_g, old_tx.protein_g, old_tx.vegetable_g, old_tx.yield_g,
    old_tx.produced_portion, old_tx.portion_weight_g, old_tx.output_uom,
    old_tx.byproduct_qty_standard
FROM kitchen_section_transactions new_tx
JOIN kitchen_section_transactions old_tx
  ON old_tx.order_no        = new_tx.order_no
 AND old_tx.recipe_no       = new_tx.recipe_no
 AND old_tx.ingredient_code = new_tx.ingredient_code
 AND old_tx.current_section = new_tx.from_section
 AND old_tx.transaction_status = 'Transferred'
WHERE new_tx.carb_g IS NULL
  AND new_tx.protein_g IS NULL
  AND new_tx.vegetable_g IS NULL
  AND new_tx.yield_g IS NULL
  AND (old_tx.carb_g IS NOT NULL OR old_tx.protein_g IS NOT NULL
       OR old_tx.vegetable_g IS NOT NULL OR old_tx.yield_g IS NOT NULL
       OR old_tx.produced_portion IS NOT NULL OR old_tx.portion_weight_g IS NOT NULL
       OR old_tx.output_uom IS NOT NULL OR old_tx.byproduct_qty_standard IS NOT NULL)
ORDER BY new_tx.order_no, new_tx.recipe_no;


-- ---------------------------------------------------------------------------
-- STEP 2 — THE ACTUAL BACKFILL. Only fills columns that are currently NULL
-- on the new row, from the single matching "Transferred" old row (LIMIT 1
-- via the correlated subquery guards against the rare case of more than one
-- candidate old row — takes the most recent by id).
-- ---------------------------------------------------------------------------
UPDATE kitchen_section_transactions new_tx
SET
    new_tx.carb_g = COALESCE(new_tx.carb_g, (
        SELECT old_tx.carb_g FROM kitchen_section_transactions old_tx
        WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
          AND old_tx.ingredient_code = new_tx.ingredient_code
          AND old_tx.current_section = new_tx.from_section
          AND old_tx.transaction_status = 'Transferred'
        ORDER BY old_tx.id DESC LIMIT 1)),
    new_tx.protein_g = COALESCE(new_tx.protein_g, (
        SELECT old_tx.protein_g FROM kitchen_section_transactions old_tx
        WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
          AND old_tx.ingredient_code = new_tx.ingredient_code
          AND old_tx.current_section = new_tx.from_section
          AND old_tx.transaction_status = 'Transferred'
        ORDER BY old_tx.id DESC LIMIT 1)),
    new_tx.vegetable_g = COALESCE(new_tx.vegetable_g, (
        SELECT old_tx.vegetable_g FROM kitchen_section_transactions old_tx
        WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
          AND old_tx.ingredient_code = new_tx.ingredient_code
          AND old_tx.current_section = new_tx.from_section
          AND old_tx.transaction_status = 'Transferred'
        ORDER BY old_tx.id DESC LIMIT 1)),
    new_tx.yield_g = COALESCE(new_tx.yield_g, (
        SELECT old_tx.yield_g FROM kitchen_section_transactions old_tx
        WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
          AND old_tx.ingredient_code = new_tx.ingredient_code
          AND old_tx.current_section = new_tx.from_section
          AND old_tx.transaction_status = 'Transferred'
        ORDER BY old_tx.id DESC LIMIT 1)),
    new_tx.produced_portion = COALESCE(new_tx.produced_portion, (
        SELECT old_tx.produced_portion FROM kitchen_section_transactions old_tx
        WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
          AND old_tx.ingredient_code = new_tx.ingredient_code
          AND old_tx.current_section = new_tx.from_section
          AND old_tx.transaction_status = 'Transferred'
        ORDER BY old_tx.id DESC LIMIT 1)),
    new_tx.portion_weight_g = COALESCE(new_tx.portion_weight_g, (
        SELECT old_tx.portion_weight_g FROM kitchen_section_transactions old_tx
        WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
          AND old_tx.ingredient_code = new_tx.ingredient_code
          AND old_tx.current_section = new_tx.from_section
          AND old_tx.transaction_status = 'Transferred'
        ORDER BY old_tx.id DESC LIMIT 1)),
    new_tx.output_uom = COALESCE(new_tx.output_uom, (
        SELECT old_tx.output_uom FROM kitchen_section_transactions old_tx
        WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
          AND old_tx.ingredient_code = new_tx.ingredient_code
          AND old_tx.current_section = new_tx.from_section
          AND old_tx.transaction_status = 'Transferred'
        ORDER BY old_tx.id DESC LIMIT 1)),
    new_tx.byproduct_qty_standard = COALESCE(new_tx.byproduct_qty_standard, (
        SELECT old_tx.byproduct_qty_standard FROM kitchen_section_transactions old_tx
        WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
          AND old_tx.ingredient_code = new_tx.ingredient_code
          AND old_tx.current_section = new_tx.from_section
          AND old_tx.transaction_status = 'Transferred'
        ORDER BY old_tx.id DESC LIMIT 1))
WHERE new_tx.carb_g IS NULL
  AND new_tx.protein_g IS NULL
  AND new_tx.vegetable_g IS NULL
  AND new_tx.yield_g IS NULL
  AND EXISTS (
      SELECT 1 FROM kitchen_section_transactions old_tx
      WHERE old_tx.order_no = new_tx.order_no AND old_tx.recipe_no = new_tx.recipe_no
        AND old_tx.ingredient_code = new_tx.ingredient_code
        AND old_tx.current_section = new_tx.from_section
        AND old_tx.transaction_status = 'Transferred'
        AND (old_tx.carb_g IS NOT NULL OR old_tx.protein_g IS NOT NULL
             OR old_tx.vegetable_g IS NOT NULL OR old_tx.yield_g IS NOT NULL
             OR old_tx.produced_portion IS NOT NULL OR old_tx.portion_weight_g IS NOT NULL
             OR old_tx.output_uom IS NOT NULL OR old_tx.byproduct_qty_standard IS NOT NULL));


-- ---------------------------------------------------------------------------
-- STEP 3 — re-run the STEP 1 preview query. It should now return 0 rows.
-- If it still returns rows, they are cases the join couldn't match (e.g. an
-- old row whose `current_section` no longer equals the new row's
-- `from_section` for some historical reason) — send those specific
-- order_no/recipe_no/ingredient_code values back and they can be looked at
-- individually rather than guessed at in bulk.
-- ---------------------------------------------------------------------------
