# ISFC PIS Phase 7 - Approval, RBAC and DevOps Guide

## Approval cycle
1. Customer/Internal Order: customer selects customer, brand, sales channel, delivery date/time, recipes and quantities.
2. Production Orders: controller reviews total, today, pending and in-process orders.
3. Head Chef Planning: head chef enters cooking date/time and material receiving date/time. Customer delivery request is not edited here.
4. BOM: after head chef approval, system generates production BOM from active recipe master.
5. Store: store issues material line-wise and chooses the target section such as Thawing, Cutting, Hot Kitchen, Cold Kitchen or Bakery/Pastry.
6. Kitchen sections: sections receive, process, record waste/return and transfer to next section, QC or Trayline/Packing.
7. QC: quality checks pass/hold/reject.
8. Trayline/Packing and Dispatch: pack portions, reject if needed, then dispatch/deliver.

## Batch and sale/portion calculation
- Batch = ordered portions / recipe standard portions.
- Sale/Portion comes from active Recipe Master `sale_price_per_portion`.
- Estimated food cost comes from active Recipe Master `food_cost_per_portion`.

## Role based access
The UI now hides menus by role. ADMIN/SUPER_ADMIN see everything. Operational users see only their process screens. Run `app/sql/phase7_role_based_access_seed.sql` to create roles.
superadmin	    admin123	    SUPER_ADMIN
headchef	    admin123	    HEAD_CHEF
storekeeper	    admin123	    STORE_KEEPER
sectionchef	    admin123	    SECTION_CHEF
qcmanager	    admin123	    QC_MANAGER
packingmanager	admin123	    PACKING_MANAGER
dispatchmanager	admin123	    DISPATCH_MANAGER
customeruser	admin123	    CUSTOMER

## Recommended DevOps path
For local laptops now, use Docker Compose because it is repeatable and easier than copying Laragon manually. Kubernetes should come later after the ERP is stable.

### Local sharing without Docker
1. Copy project folder to the other laptop.
2. Install Python 3.12.
3. Create venv and install requirements.
4. Export/import MySQL database from phpMyAdmin.
5. Update `.env` database credentials.
6. Run `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`.

### Better: containerized
1. Add Dockerfile and docker-compose with app + MySQL.
2. Mount database volume.
3. Put environment variables in `.env`.
4. Run `docker compose up -d --build`.
5. Other laptops only need Docker Desktop.

Kubernetes is not required yet. Use Docker Compose for development and staging, then move to Kubernetes/managed cloud only when client users, uptime, backups and CI/CD are needed.
