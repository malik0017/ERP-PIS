-- Phase 2 master upload schema
-- Run in phpMyAdmin after selecting the ISFC database.

CREATE TABLE IF NOT EXISTS master_records (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL DEFAULT 1,
    master_type VARCHAR(50) NOT NULL,
    code VARCHAR(100) NOT NULL,
    name_en VARCHAR(255) NULL,
    name_ar VARCHAR(255) NULL,
    version INT NOT NULL DEFAULT 1,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    approval_status VARCHAR(20) NOT NULL DEFAULT 'APPROVED',
    raw_json LONGTEXT NULL,
    remarks TEXT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY ix_master_records_type_code (company_id, master_type, code),
    KEY ix_master_records_status (status),
    KEY ix_master_records_approval (approval_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS brands (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL DEFAULT 1,
    brand_code VARCHAR(50) NOT NULL,
    brand_name_en VARCHAR(255) NOT NULL,
    brand_name_ar VARCHAR(255) NULL,
    short_code VARCHAR(50) NULL,
    revenue_stream_code VARCHAR(255) NULL,
    revenue_stream_name VARCHAR(255) NULL,
    default_kitchen_code VARCHAR(50) NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    version INT NOT NULL DEFAULT 1,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    approval_status VARCHAR(20) NOT NULL DEFAULT 'APPROVED',
    remarks TEXT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY ix_brands_code (company_id, brand_code),
    KEY ix_brands_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS revenue_streams (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL DEFAULT 1,
    stream_code VARCHAR(50) NOT NULL,
    stream_name VARCHAR(255) NOT NULL,
    description TEXT NULL,
    revenue_category VARCHAR(150) NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    version INT NOT NULL DEFAULT 1,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    approval_status VARCHAR(20) NOT NULL DEFAULT 'APPROVED',
    remarks TEXT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY ix_revenue_streams_code (company_id, stream_code),
    KEY ix_revenue_streams_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS kitchen_locations (
    id INT AUTO_INCREMENT PRIMARY KEY,
    company_id INT NOT NULL DEFAULT 1,
    kitchen_code VARCHAR(50) NOT NULL,
    kitchen_name VARCHAR(255) NOT NULL,
    kitchen_type VARCHAR(150) NULL,
    location VARCHAR(255) NULL,
    city VARCHAR(100) NULL,
    brand_supported TEXT NULL,
    capacity VARCHAR(100) NULL,
    manager VARCHAR(255) NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    version INT NOT NULL DEFAULT 1,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    approval_status VARCHAR(20) NOT NULL DEFAULT 'APPROVED',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY ix_kitchen_locations_code (company_id, kitchen_code),
    KEY ix_kitchen_locations_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
