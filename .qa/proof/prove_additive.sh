#!/usr/bin/env bash
# Prove that revision e1c7b4a90d63 (crm_links) is STRICTLY ADDITIVE.
#
# Method, mechanical throughout — nothing is eyeballed:
#   1. throwaway PG18 cluster on port 55432 (never the live one on 5432)
#   2. migrate to c3e6a9d1f725, the revision immediately BEFORE mine
#   3. seed representative rows into existing tables, and checksum them
#   4. snapshot the CATALOGUE (every table/column/type/nullable/default, every index,
#      every constraint, every sequence) + a pg_dump -s, both BEFORE
#   5. alembic upgrade head  (applies ONLY e1c7b4a90d63)
#   6. snapshot both again, AFTER
#   7. diff and ASSERT: zero catalogue entries removed, zero changed, and every ADDED
#      entry names crm_links as its object
#   8. re-checksum the seeded rows and ASSERT unchanged
#   9. alembic downgrade -1, snapshot again, ASSERT identical to before
#
# Why a catalogue diff and not a text diff of pg_dump: PostgreSQL 18's pg_dump emits a
# random `\restrict <nonce>` header on every run, so two dumps of an IDENTICAL database
# never match textually; and a line-by-line grep cannot tell that "    id uuid NOT NULL,"
# belongs to crm_links rather than to something existing. Comparing the catalogue compares
# the actual objects, with each row already labelled by the table it belongs to.
set -uo pipefail

SCRATCH="$(cd "$(dirname "$0")" && pwd)"
OUT="$SCRATCH/out"
PG=/usr/lib/postgresql/18/bin
REPO=/home/qa/owen-main
PY="$REPO/backend/.venv/bin/python"
PORT=55432
DB=crmproof
export PGPASSWORD=x

fail=0
say()  { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  [PASS] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; fail=1; }
psqlq() { "$PG/psql" -h 127.0.0.1 -p $PORT -U crmtest -d "$DB" -tAq -c "$1"; }
alem()  { (cd "$REPO/backend" && env POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=$PORT \
            POSTGRES_USER=crmtest POSTGRES_PASSWORD=x POSTGRES_DB=$DB \
            "$PY" -m alembic "$@" 2>&1 | tail -3); }

# Every schema object that matters, one per line, each prefixed by the object it belongs to.
catalogue() {
  psqlq "
  select 'COLUMN      '||table_name||'.'||column_name||' :: '||data_type
         ||' null='||is_nullable||' default='||coalesce(replace(column_default,E'\n',' '),'-')
    from information_schema.columns where table_schema='public'
  union all
  select 'TABLE       '||table_name from information_schema.tables where table_schema='public'
  union all
  select 'INDEX       '||tablename||' :: '||indexname||' :: '||replace(indexdef,E'\n',' ')
    from pg_indexes where schemaname='public'
  union all
  select 'CONSTRAINT  '||rel.relname||' :: '||con.conname||' :: '
         ||replace(pg_get_constraintdef(con.oid),E'\n',' ')
    from pg_constraint con join pg_class rel on rel.oid=con.conrelid
    join pg_namespace n on n.oid=rel.relnamespace where n.nspname='public'
  union all
  select 'SEQUENCE    '||sequence_name from information_schema.sequences where sequence_schema='public'
  union all
  select 'TRIGGER     '||rel.relname||' :: '||tg.tgname
    from pg_trigger tg join pg_class rel on rel.oid=tg.tgrelid
    join pg_namespace n on n.oid=rel.relnamespace
    where n.nspname='public' and not tg.tgisinternal
  order by 1;"
}

# pg_dump with PG18's per-run random \restrict nonce stripped, so two dumps of an identical
# database compare byte-for-byte.
dump_schema() {
  "$PG/pg_dump" -h 127.0.0.1 -p $PORT -U crmtest -d "$DB" --schema-only --no-owner --no-acl \
    | grep -vE '^\\(un)?restrict '
}

# --- 1. throwaway cluster -------------------------------------------------------------
say "1. throwaway cluster (port $PORT — NOT the live cluster on 5432)"
"$PG/pg_ctl" -D "$SCRATCH/pgdata" stop -m immediate >/dev/null 2>&1
rm -rf "$SCRATCH/pgdata" "$OUT"; mkdir -p "$SCRATCH/pgdata" "$OUT"
"$PG/initdb" -D "$SCRATCH/pgdata" -U crmtest --auth=trust >/dev/null 2>&1
"$PG/pg_ctl" -D "$SCRATCH/pgdata" -o "-p $PORT -k $SCRATCH/pgdata -h 127.0.0.1" \
  -l "$SCRATCH/pg.log" start >/dev/null 2>&1
sleep 1
"$PG/psql" -h 127.0.0.1 -p $PORT -U crmtest -d postgres -qc "CREATE DATABASE $DB;" >/dev/null
ok "cluster up on $PORT"

# --- 2. migrate to the revision BEFORE mine -------------------------------------------
say "2. upgrade to c3e6a9d1f725 (the revision immediately before crm_links)"
alem upgrade c3e6a9d1f725 >/dev/null
CUR=$(alem current | grep -oE '[0-9a-f]{12}' | head -1)
[ "$CUR" = "c3e6a9d1f725" ] && ok "at c3e6a9d1f725" || bad "expected c3e6a9d1f725, got '$CUR'"
[ -z "$(psqlq "select to_regclass('crm_links')")" ] && ok "crm_links does not exist yet" \
  || bad "crm_links already exists"

# --- 3. seed representative rows into EXISTING tables ---------------------------------
say "3. seed existing tables, so 'existing rows untouched' can be proven too"
psqlq "
INSERT INTO providers (name) VALUES ('bulkvs'), ('asterisk');
INSERT INTO campaigns (id, name, source, active)
  VALUES (gen_random_uuid(), 'CL Ads', 'craigslist', true);
INSERT INTO numbers (id, provider_id, campaign_id, phone_number, friendly_name,
                     media_provider, owner_provider, active, sms_enabled)
  SELECT gen_random_uuid(), p.id, c.id, '+15615550200', 'Main line',
         'asterisk', 'bulkvs', true, false
  FROM providers p, campaigns c WHERE p.name='bulkvs' LIMIT 1;
INSERT INTO callers (id, phone_number, total_calls)
  VALUES (gen_random_uuid(), '+15615559999', 3);
INSERT INTO calls (id, provider_id, provider_call_sid, number_id, caller_id, campaign_id,
                   direction, status, status_rank, started_at, duration_seconds)
  SELECT gen_random_uuid(), p.id, '1799000444.1', n.id, cl.id, n.campaign_id,
         'inbound', 'completed', 5, now(), 42
  FROM providers p, numbers n, callers cl WHERE p.name='asterisk' LIMIT 1;
" >/dev/null 2>&1
[ "$(psqlq "select count(*) from calls")" = "1" ] \
  && ok "seeded providers/campaigns/numbers/callers/calls" || bad "seed failed"

data_checksum() {
  psqlq "
  select md5(string_agg(t, '|' order by t)) from (
    select md5(providers.*::text)     as t from providers
    union all select md5(campaigns.*::text) from campaigns
    union all select md5(numbers.*::text)   from numbers
    union all select md5(callers.*::text)   from callers
    union all select md5(calls.*::text)     from calls
  ) s;"
}
DATA_BEFORE=$(data_checksum)
ok "row checksum before: $DATA_BEFORE"

# --- 4. snapshot BEFORE ----------------------------------------------------------------
say "4. snapshot BEFORE"
catalogue    > "$OUT/before.catalogue"
dump_schema  > "$OUT/before.sql"
ok "catalogue: $(wc -l < "$OUT/before.catalogue") objects   pg_dump: $(wc -l < "$OUT/before.sql") lines"

# --- 5. apply MY revision ---------------------------------------------------------------
say "5. alembic upgrade head (applies ONLY e1c7b4a90d63)"
alem upgrade head
CUR=$(alem current | grep -oE '[0-9a-f]{12}' | head -1)
[ "$CUR" = "e1c7b4a90d63" ] && ok "at e1c7b4a90d63 (head)" || bad "expected head, got '$CUR'"

# --- 6. snapshot AFTER ------------------------------------------------------------------
say "6. snapshot AFTER"
catalogue    > "$OUT/after.catalogue"
dump_schema  > "$OUT/after.sql"
ok "catalogue: $(wc -l < "$OUT/after.catalogue") objects   pg_dump: $(wc -l < "$OUT/after.sql") lines"

# --- 7. THE ASSERTION -------------------------------------------------------------------
say "7. catalogue diff, asserted mechanically"
diff -u "$OUT/before.catalogue" "$OUT/after.catalogue" > "$OUT/catalogue.diff"

grep '^-[^-]' "$OUT/catalogue.diff" | sed 's/^-//' > "$OUT/removed.txt"
grep '^+[^+]' "$OUT/catalogue.diff" | sed 's/^+//' > "$OUT/added.txt"

if [ -s "$OUT/removed.txt" ]; then
  bad "$(wc -l < "$OUT/removed.txt") catalogue objects were REMOVED or CHANGED:"
  cat "$OUT/removed.txt"
else
  ok "ZERO catalogue objects removed or changed"
fi

# Every ADDED object must name crm_links. Column/index/constraint rows are already labelled
# with their owning table, so this is exact rather than a substring guess.
grep -vE '(^| )crm_links(\.| |$)' "$OUT/added.txt" > "$OUT/foreign_additions.txt"
if [ -s "$OUT/foreign_additions.txt" ]; then
  bad "added objects that do NOT belong to crm_links:"; cat "$OUT/foreign_additions.txt"
else
  ok "all $(wc -l < "$OUT/added.txt") added objects belong to crm_links and nothing else"
fi

# The FK constraint must sit on crm_links, never on numbers.
if grep -qE '^CONSTRAINT +numbers ' "$OUT/added.txt"; then
  bad "a constraint was added to the numbers table"
else
  ok "no constraint added to public.numbers (the FK lives on crm_links)"
fi

# And the pg_dump text must agree: nothing removed there either.
diff -u "$OUT/before.sql" "$OUT/after.sql" > "$OUT/schema.diff"
DUMP_REMOVED=$(grep -c '^-[^-]' "$OUT/schema.diff" || true)
[ "$DUMP_REMOVED" = "0" ] && ok "pg_dump agrees: zero lines removed" \
  || { bad "pg_dump shows $DUMP_REMOVED removed lines"; grep '^-[^-]' "$OUT/schema.diff"; }

# Every pg_dump hunk must be inside a crm_links object.
if grep -E '^\+.*(ALTER TABLE|DROP )' "$OUT/schema.diff" | grep -vi 'crm_links' | grep -q .; then
  bad "an ALTER/DROP against something other than crm_links:"
  grep -E '^\+.*(ALTER TABLE|DROP )' "$OUT/schema.diff" | grep -vi 'crm_links'
else
  ok "no ALTER or DROP against any pre-existing object"
fi

# --- 8. existing rows untouched ---------------------------------------------------------
say "8. existing ROWS untouched"
DATA_AFTER=$(data_checksum)
[ "$DATA_BEFORE" = "$DATA_AFTER" ] && ok "row checksum identical: $DATA_AFTER" \
  || bad "data changed! $DATA_BEFORE -> $DATA_AFTER"
[ "$(psqlq "select count(*) from crm_links")" = "0" ] \
  && ok "crm_links created EMPTY (no backfill, no data migration)" || bad "crm_links was backfilled"

# --- 9. downgrade restores everything ----------------------------------------------------
say "9. downgrade drops ONLY crm_links"
alem downgrade -1 >/dev/null
catalogue   > "$OUT/rollback.catalogue"
dump_schema > "$OUT/rollback.sql"
if diff -q "$OUT/before.catalogue" "$OUT/rollback.catalogue" >/dev/null; then
  ok "post-downgrade catalogue is IDENTICAL to before"
else
  bad "downgrade did not restore the catalogue:"; diff -u "$OUT/before.catalogue" "$OUT/rollback.catalogue"
fi
if diff -q "$OUT/before.sql" "$OUT/rollback.sql" >/dev/null; then
  ok "post-downgrade pg_dump is BYTE-IDENTICAL to before"
else
  bad "downgrade did not restore the dump:"; diff -u "$OUT/before.sql" "$OUT/rollback.sql" | head -20
fi
[ "$DATA_BEFORE" = "$(data_checksum)" ] && ok "rows still identical after downgrade" \
  || bad "downgrade changed data"

# --- teardown ----------------------------------------------------------------------------
say "teardown"
alem upgrade head >/dev/null
"$PG/pg_ctl" -D "$SCRATCH/pgdata" stop -m fast >/dev/null 2>&1
ok "throwaway cluster stopped"

echo
if [ "$fail" -ne 0 ]; then echo "PROOF FAILED — the revision is NOT strictly additive."; exit 1; fi
echo "PROOF PASSED — revision e1c7b4a90d63 is strictly additive."
