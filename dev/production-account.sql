-- Production account for ClickSense, with SQL-driven access control (run as an admin).
-- This is the real safety boundary: the MCP server's guard is defence in depth on top.
-- Adjust the numbers to your cluster; CONSTRAINTS stop any client from raising them.

CREATE SETTINGS PROFILE IF NOT EXISTS clicksense_ro SETTINGS
    readonly = 2 CONST,                       -- read + change settings (needed for per-query limits)
    allow_ddl = 0 CONST,
    allow_introspection_functions = 0 CONST,
    max_execution_time = 30 MAX 60,
    timeout_overflow_mode = 'throw',
    max_memory_usage = 2000000000 MAX 4000000000,
    max_threads = 4 MAX 8,
    max_rows_to_read = 2000000000 MAX 5000000000,
    max_bytes_to_read = 200000000000 MAX 500000000000,
    max_result_rows = 1000000,
    max_concurrent_queries_for_user = 8 MAX 8,
    priority = 5 MIN 5,                       -- lower priority than interactive / ETL users
    max_bytes_before_external_group_by = 1000000000,
    max_bytes_before_external_sort = 1000000000,
    cancel_http_readonly_queries_on_client_close = 1;

CREATE QUOTA IF NOT EXISTS clicksense_quota
    FOR INTERVAL 1 hour MAX queries = 5000, errors = 1000, read_rows = 200000000000, execution_time = 7200
    TO clicksense;

CREATE USER IF NOT EXISTS clicksense
    IDENTIFIED WITH sha256_password BY 'change-me'
    HOST IP '10.0.0.0/8'                      -- the MCP host(s) only
    SETTINGS PROFILE 'clicksense_ro';

GRANT SELECT ON shop.* TO clicksense;          -- one line per allowed database
GRANT SELECT ON system.data_skipping_indices TO clicksense;

-- Optional, for workload isolation on ClickHouse >= 24.x: a dedicated workload with its own
-- CPU/IO share, so agent queries can never starve production dashboards.
-- CREATE RESOURCE cpu (MASTER THREAD, WORKER THREAD);
-- CREATE WORKLOAD all; CREATE WORKLOAD agents IN all SETTINGS weight = 1, max_concurrent_threads = 8;
-- ALTER SETTINGS PROFILE clicksense_ro SETTINGS workload = 'agents';
