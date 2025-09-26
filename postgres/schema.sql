-- 1) Schema and parent
CREATE SCHEMA IF NOT EXISTS metrics;

CREATE TABLE IF NOT EXISTS metrics.ntp_parent (
  ts               timestamptz NOT NULL,
  last_offset_sec  double precision,
  stratum          integer,
  total_sources    integer,
  leap_status      text,
  gps_mode         text,
  selected_source  text,
  gps_summary      text,
  created_at       timestamptz DEFAULT now(),
  PRIMARY KEY (ts)
) PARTITION BY RANGE (ts);

-- Helpful for pruning/queries
CREATE INDEX IF NOT EXISTS idx_ntp_ts ON metrics.ntp_parent (ts);

-- 2) Drop any old versions (safe if they don't exist)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_proc
    WHERE proname = 'create_daily_partition'
      AND pronamespace = 'metrics'::regnamespace
  ) THEN
    DROP FUNCTION metrics.create_daily_partition(date);
  END IF;

  IF EXISTS (
    SELECT 1 FROM pg_proc
    WHERE proname = 'maintain_partitions'
      AND pronamespace = 'metrics'::regnamespace
  ) THEN
    DROP FUNCTION metrics.maintain_partitions(int, int);
  END IF;
END$$;

-- 3) Child-creator (idempotent) with full schema qualification
CREATE OR REPLACE FUNCTION metrics.create_daily_partition(p_day date)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
  part_start   timestamptz := p_day::timestamptz;
  part_end     timestamptz := (p_day + 1)::timestamptz;
  schema_name  text := 'metrics';
  parent_name  text := 'ntp_parent';
  part_name    text := format('ntp_y%sm%sd%s',
                     to_char(p_day,'YYYY'), to_char(p_day,'MM'), to_char(p_day,'DD'));
  idx_name     text := part_name || '_ts_idx';
  child_reg    regclass;
BEGIN
  -- Create the partition table only if it does not already exist
  SELECT to_regclass(format('%I.%I', schema_name, part_name)) INTO child_reg;

  IF child_reg IS NULL THEN
    EXECUTE format(
      'CREATE TABLE %I.%I PARTITION OF %I.%I FOR VALUES FROM (%L) TO (%L);',
      schema_name, part_name, schema_name, parent_name, part_start, part_end
    );
  END IF;

  -- Ensure per-child index exists (safe to run repeatedly)
  EXECUTE format(
    'CREATE INDEX IF NOT EXISTS %I ON %I.%I (ts);',
    idx_name, schema_name, part_name
  );
END;
$$;

-- 4) Maintenance with direct regexp_match (no EXECUTE)
CREATE OR REPLACE FUNCTION metrics.maintain_partitions(retention_days int, premake_days int DEFAULT 1)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
  d date;
  keep_after timestamptz := (current_date - retention_days)::timestamptz;
  r RECORD;
  lower_ts timestamptz;
BEGIN
  -- Ensure today's and the next N days' partitions exist
  PERFORM metrics.create_daily_partition(current_date);
  FOR d IN
    SELECT generate_series(current_date + 1, current_date + premake_days, '1 day')::date
  LOOP
    PERFORM metrics.create_daily_partition(d);
  END LOOP;

  -- Drop partitions whose LOWER bound is older than the retention window
  FOR r IN
    SELECT c.oid, n.nspname, c.relname,
           pg_get_expr(c.relpartbound, c.oid) AS bound
    FROM pg_inherits i
    JOIN pg_class c      ON c.oid = i.inhrelid
    JOIN pg_namespace n  ON n.oid = c.relnamespace
    WHERE i.inhparent = 'metrics.ntp_parent'::regclass
  LOOP
    -- Example bound: FOR VALUES FROM ('2025-09-25 00:00:00+00') TO ('2025-09-26 00:00:00+00')
    SELECT ((regexp_match(r.bound, 'FROM \(''([^'']+)''\)')))[1]::timestamptz
      INTO lower_ts;

    IF lower_ts IS NOT NULL AND lower_ts < keep_after THEN
      EXECUTE format('DROP TABLE IF EXISTS %I.%I CASCADE;', r.nspname, r.relname);
    END IF;
  END LOOP;
END;
$$;

-- 5) Bootstrap partitions (yesterday/today/tomorrow) - safe to re-run
SELECT metrics.create_daily_partition(current_date - 1);
SELECT metrics.create_daily_partition(current_date);
SELECT metrics.create_daily_partition(current_date + 1);

-- =====================================================================
-- 6) pg_cron: schedule automatic partition maintenance (RDS-friendly)
-- =====================================================================
-- Prerequisites (especially on Amazon RDS for PostgreSQL):
--   * Add 'pg_cron' to the DB parameter group's shared_preload_libraries.
--   * Set parameter 'cron.database_name' to THIS database's name.
--   * Restart the DB instance for parameters to take effect.
-- All pg_cron schedules run in the server's time zone (often UTC on RDS).

-- Create the extension if available
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Wrapper marked SECURITY DEFINER so the cron job can run with the creator's privileges.
-- Adjust retention/premake as desired (e.g., 90-day retention, pre-create 1 day ahead).
CREATE OR REPLACE FUNCTION metrics.run_partition_maintenance()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  PERFORM metrics.maintain_partitions(90, 1);
END;
$$;

-- Ensure the function owner is a role that will remain (alternatively ALTER FUNCTION OWNER TO some_role;)

-- Create or update a daily cron job at 00:15 (server time) to run maintenance.
DO $$
DECLARE
  v_has_jobname boolean;
  v_jobid int;
BEGIN
  -- Detect whether pg_cron has 'jobname' column (newer versions)
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'cron' AND table_name = 'job' AND column_name = 'jobname'
  ) INTO v_has_jobname;

  IF v_has_jobname THEN
    -- Use jobname-based idempotency
    SELECT jobid INTO v_jobid FROM cron.job WHERE jobname = 'metrics_partition_maint' LIMIT 1;

    IF v_jobid IS NULL THEN
      PERFORM cron.schedule('metrics_partition_maint', '15 0 * * *',
                            $$SELECT metrics.run_partition_maintenance();$$);
    ELSE
      PERFORM cron.alter_job(v_jobid,
              schedule => '15 0 * * *',
              command  => 'SELECT metrics.run_partition_maintenance();',
              active   => true);
    END IF;
  ELSE
    -- Fallback for older pg_cron: match by command text
    SELECT jobid INTO v_jobid
      FROM cron.job
     WHERE command = 'SELECT metrics.run_partition_maintenance();'
     LIMIT 1;

    IF v_jobid IS NULL THEN
      PERFORM cron.schedule('15 0 * * *', 'SELECT metrics.run_partition_maintenance();');
    ELSE
      PERFORM cron.alter_job(v_jobid,
              schedule => '15 0 * * *',
              command  => 'SELECT metrics.run_partition_maintenance();',
              active   => true);
    END IF;
  END IF;
END$$;

-- 7) Optional: run once now (90-day retention, pre-create 1 day ahead)
SELECT metrics.maintain_partitions(90, 1);
