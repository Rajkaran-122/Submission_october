-- Ghost Data Still Exists: point-in-time database snapshot
-- Dialect: SQLite-compatible SQL
-- Captured at: 2026-09-12T09:00:00Z
-- Database topology: read_replica
-- Compare the primary and any replica tables for replication drift.

BEGIN TRANSACTION;
CREATE TABLE customer_data_read_replica (
            resource_id TEXT PRIMARY KEY,
            origin_system TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            record_json TEXT NOT NULL CHECK (json_valid(record_json)),
            updated_at TEXT NOT NULL
        );
INSERT INTO "customer_data_read_replica" VALUES('account_h02','account_service','customer_profiles','{"resource_id":"account_h02","customer_reference":"user_h02","full_name":"Ines Moreau","email":"ines.moreau@example.com","phone":"+33 6 12 34 56 78","account_status":"ACTIVE","signup_channel":"organic"}','2026-09-12T09:00:00Z');
COMMIT;
