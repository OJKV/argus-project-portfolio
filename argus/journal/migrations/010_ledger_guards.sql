-- Ledger tables are insert-only, enforced by the database rather than by convention
-- (review J-3 / J-N1). Corrections are new rows carrying supersedes_id.
--
-- The one write the schema allows after INSERT: orders.exchange_id, algo_id and raw_response
-- start NULL because the venue's ids do not exist until it answers, and record_venue_ids fills
-- them in once (F-129). Those three columns are outside the row hash (ledger.WRITE_ONCE), so
-- filling them cannot break the chain; this trigger is what protects them instead — NULL may
-- become a value, a value may never change. Every other column, and every DELETE, aborts.
-- `argus audit` fails if any of these triggers is missing.

CREATE TRIGGER orders_write_once BEFORE UPDATE ON orders
WHEN NEW.order_id IS NOT OLD.order_id
  OR NEW.cl_ord_id IS NOT OLD.cl_ord_id
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.plan_id IS NOT OLD.plan_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.symbol IS NOT OLD.symbol
  OR NEW.side IS NOT OLD.side
  OR NEW.kind IS NOT OLD.kind
  OR NEW.order_type IS NOT OLD.order_type
  OR NEW.price IS NOT OLD.price
  OR NEW.size IS NOT OLD.size
  OR NEW.state IS NOT OLD.state
  OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.raw_request IS NOT OLD.raw_request
  OR NEW.supersedes_id IS NOT OLD.supersedes_id
  OR NEW.prev_hash IS NOT OLD.prev_hash
  OR NEW.row_hash IS NOT OLD.row_hash
  OR (OLD.exchange_id IS NOT NULL AND NEW.exchange_id IS NOT OLD.exchange_id)
  OR (OLD.algo_id IS NOT NULL AND NEW.algo_id IS NOT OLD.algo_id)
  OR (OLD.raw_response IS NOT NULL AND NEW.raw_response IS NOT OLD.raw_response)
BEGIN
    SELECT RAISE(ABORT, 'orders is an insert-only ledger table: only NULL venue ids may be filled in');
END;

CREATE TRIGGER orders_no_delete BEFORE DELETE ON orders
BEGIN
    SELECT RAISE(ABORT, 'orders is an insert-only ledger table');
END;

CREATE TRIGGER plans_no_update BEFORE UPDATE ON plans
BEGIN
    SELECT RAISE(ABORT, 'plans is an insert-only ledger table: append a row with supersedes_id');
END;

CREATE TRIGGER plans_no_delete BEFORE DELETE ON plans
BEGIN
    SELECT RAISE(ABORT, 'plans is an insert-only ledger table');
END;

CREATE TRIGGER gate_results_no_update BEFORE UPDATE ON gate_results
BEGIN
    SELECT RAISE(ABORT, 'gate_results is an insert-only ledger table: append a row with supersedes_id');
END;

CREATE TRIGGER gate_results_no_delete BEFORE DELETE ON gate_results
BEGIN
    SELECT RAISE(ABORT, 'gate_results is an insert-only ledger table');
END;

CREATE TRIGGER fills_no_update BEFORE UPDATE ON fills
BEGIN
    SELECT RAISE(ABORT, 'fills is an insert-only ledger table');
END;

CREATE TRIGGER fills_no_delete BEFORE DELETE ON fills
BEGIN
    SELECT RAISE(ABORT, 'fills is an insert-only ledger table');
END;

CREATE TRIGGER position_events_no_update BEFORE UPDATE ON position_events
BEGIN
    SELECT RAISE(ABORT, 'position_events is an insert-only ledger table');
END;

CREATE TRIGGER position_events_no_delete BEFORE DELETE ON position_events
BEGIN
    SELECT RAISE(ABORT, 'position_events is an insert-only ledger table');
END;

CREATE TRIGGER kill_switches_no_update BEFORE UPDATE ON kill_switches
BEGIN
    SELECT RAISE(ABORT, 'kill_switches is an insert-only ledger table');
END;

CREATE TRIGGER kill_switches_no_delete BEFORE DELETE ON kill_switches
BEGIN
    SELECT RAISE(ABORT, 'kill_switches is an insert-only ledger table');
END;
