CREATE TABLE IF NOT EXISTS kitchen_sections (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL DEFAULT 1,
    section_code VARCHAR(50) NOT NULL,
    section_name VARCHAR(255) NOT NULL,
    section_name_ar VARCHAR(255) NULL,
    kitchen_code VARCHAR(50) NULL,
    sequence_no INT NOT NULL DEFAULT 1,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    remarks TEXT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX ix_kitchen_sections_code (section_code),
    INDEX ix_kitchen_sections_company (company_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO kitchen_sections (company_id, section_code, section_name, sequence_no, status, is_active)
SELECT 1, 'THAW', 'Thawing', 10, 'ACTIVE', 1
WHERE NOT EXISTS (SELECT 1 FROM kitchen_sections WHERE section_code = 'THAW');
INSERT INTO kitchen_sections (company_id, section_code, section_name, sequence_no, status, is_active)
SELECT 1, 'CUT', 'Cutting', 20, 'ACTIVE', 1
WHERE NOT EXISTS (SELECT 1 FROM kitchen_sections WHERE section_code = 'CUT');
INSERT INTO kitchen_sections (company_id, section_code, section_name, sequence_no, status, is_active)
SELECT 1, 'BUTCH', 'Butchery', 30, 'ACTIVE', 1
WHERE NOT EXISTS (SELECT 1 FROM kitchen_sections WHERE section_code = 'BUTCH');
INSERT INTO kitchen_sections (company_id, section_code, section_name, sequence_no, status, is_active)
SELECT 1, 'MAR', 'Marination', 40, 'ACTIVE', 1
WHERE NOT EXISTS (SELECT 1 FROM kitchen_sections WHERE section_code = 'MAR');
INSERT INTO kitchen_sections (company_id, section_code, section_name, sequence_no, status, is_active)
SELECT 1, 'HOT', 'Hot Kitchen', 50, 'ACTIVE', 1
WHERE NOT EXISTS (SELECT 1 FROM kitchen_sections WHERE section_code = 'HOT');
INSERT INTO kitchen_sections (company_id, section_code, section_name, sequence_no, status, is_active)
SELECT 1, 'COLD', 'Cold Kitchen', 60, 'ACTIVE', 1
WHERE NOT EXISTS (SELECT 1 FROM kitchen_sections WHERE section_code = 'COLD');
INSERT INTO kitchen_sections (company_id, section_code, section_name, sequence_no, status, is_active)
SELECT 1, 'BAKERY', 'Bakery/Pastry', 70, 'ACTIVE', 1
WHERE NOT EXISTS (SELECT 1 FROM kitchen_sections WHERE section_code = 'BAKERY');
INSERT INTO kitchen_sections (company_id, section_code, section_name, sequence_no, status, is_active)
SELECT 1, 'TRAY', 'Tray Line', 80, 'ACTIVE', 1
WHERE NOT EXISTS (SELECT 1 FROM kitchen_sections WHERE section_code = 'TRAY');
