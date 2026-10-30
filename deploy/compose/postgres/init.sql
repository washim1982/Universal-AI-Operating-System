-- Runs once, when the data volume is first created.
-- Schema migrations for the event store (WP 1.1) and memory (WP 2.1) are applied by AIOS itself.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
