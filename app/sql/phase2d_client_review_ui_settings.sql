-- Phase 2D: client review UI, settings and richer master columns.
-- Run once in phpMyAdmin on database isfc_db before restarting after applying patch.

ALTER TABLE customers
  ADD COLUMN customer_name_ar VARCHAR(255) NULL,
  ADD COLUMN sales_man VARCHAR(150) NULL,
  ADD COLUMN contact_person VARCHAR(150) NULL,
  ADD COLUMN phone VARCHAR(80) NULL,
  ADD COLUMN email VARCHAR(150) NULL,
  ADD COLUMN brand VARCHAR(150) NULL,
  ADD COLUMN vat_number VARCHAR(100) NULL,
  ADD COLUMN customer_type VARCHAR(150) NULL,
  ADD COLUMN city VARCHAR(100) NULL,
  ADD COLUMN payment_terms VARCHAR(150) NULL;

ALTER TABLE suppliers
  ADD COLUMN supplier_name_ar VARCHAR(255) NULL,
  ADD COLUMN category VARCHAR(150) NULL,
  ADD COLUMN phone VARCHAR(80) NULL,
  ADD COLUMN email VARCHAR(150) NULL,
  ADD COLUMN vat_number VARCHAR(100) NULL,
  ADD COLUMN payment_terms VARCHAR(150) NULL,
  ADD COLUMN supplier_type VARCHAR(150) NULL,
  ADD COLUMN city VARCHAR(100) NULL,
  ADD COLUMN country VARCHAR(100) NULL;

ALTER TABLE chefs
  ADD COLUMN job_title VARCHAR(150) NULL,
  ADD COLUMN kitchen_section VARCHAR(150) NULL,
  ADD COLUMN tasks VARCHAR(255) NULL,
  ADD COLUMN brand_assign VARCHAR(255) NULL,
  ADD COLUMN remarks TEXT NULL;

-- Normalize old title-case statuses to uppercase for consistent filters.
UPDATE customers SET status='ACTIVE' WHERE status='Active';
UPDATE suppliers SET status='ACTIVE' WHERE status='Active';
UPDATE chefs SET status='ACTIVE' WHERE status='Active';
UPDATE ingredients SET status='ACTIVE' WHERE status='Active';

-- Ensure a settings row exists.
INSERT INTO system_settings (
  company_id, company_name, company_name_ar, default_language, timezone, currency, date_format, number_format, is_rtl_enabled, is_active
)
SELECT 1, 'International Specialized Food Company', 'الشركة العالمية المتخصصة للأغذية', 'en', 'Asia/Riyadh', 'SAR', 'dd-mm-yyyy', '1,234.00', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM system_settings WHERE company_id = 1 AND is_active = 1);
