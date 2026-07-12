-- Phase 2E schema safety migration.
-- Run in phpMyAdmin against isfc_db if /settings shows missing columns.

CREATE TABLE IF NOT EXISTS system_settings (
    id INT AUTO_INCREMENT PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @db = DATABASE();

SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='company_id')=0, 'ALTER TABLE system_settings ADD COLUMN company_id INT NULL DEFAULT 1', 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='company_name')=0, "ALTER TABLE system_settings ADD COLUMN company_name VARCHAR(255) NOT NULL DEFAULT 'International Specialized Food Company'", 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='company_name_ar')=0, 'ALTER TABLE system_settings ADD COLUMN company_name_ar VARCHAR(255) NULL', 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='logo')=0, 'ALTER TABLE system_settings ADD COLUMN logo VARCHAR(255) NULL', 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='favicon')=0, 'ALTER TABLE system_settings ADD COLUMN favicon VARCHAR(255) NULL', 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='default_language')=0, "ALTER TABLE system_settings ADD COLUMN default_language VARCHAR(10) NOT NULL DEFAULT 'en'", 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='timezone')=0, "ALTER TABLE system_settings ADD COLUMN timezone VARCHAR(100) NOT NULL DEFAULT 'Asia/Riyadh'", 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='currency')=0, "ALTER TABLE system_settings ADD COLUMN currency VARCHAR(10) NOT NULL DEFAULT 'SAR'", 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='date_format')=0, "ALTER TABLE system_settings ADD COLUMN date_format VARCHAR(50) NOT NULL DEFAULT 'dd-mm-yyyy'", 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='number_format')=0, "ALTER TABLE system_settings ADD COLUMN number_format VARCHAR(50) NOT NULL DEFAULT '1,234.00'", 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='is_rtl_enabled')=0, 'ALTER TABLE system_settings ADD COLUMN is_rtl_enabled TINYINT(1) NOT NULL DEFAULT 1', 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='is_active')=0, 'ALTER TABLE system_settings ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1', 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='created_at')=0, 'ALTER TABLE system_settings ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP', 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @sql = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=@db AND table_name='system_settings' AND column_name='updated_at')=0, 'ALTER TABLE system_settings ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP', 'SELECT 1'); PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

INSERT INTO system_settings (company_id, company_name, company_name_ar, default_language, timezone, currency, is_rtl_enabled, is_active)
SELECT 1, 'International Specialized Food Company', 'الشركة العالمية المتخصصة للأغذية', 'en', 'Asia/Riyadh', 'SAR', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM system_settings WHERE company_id = 1);
