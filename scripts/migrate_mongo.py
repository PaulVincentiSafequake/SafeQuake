"""
QuakeAngel Mongo migration script — export from source, import to target.

## What this does

- Reads every collection from `SRC_MONGO_URL` (the Emergent-hosted
  instance) and writes newline-delimited JSON files under a `dump/`
  directory, one file per collection.
- Restores those files into `DST_MONGO_URL` (MongoDB Atlas Frankfurt).
- Verifies row counts match before and after.
- Records what indexes existed on the source and creates them on the
  target — Mongo's default `_id_` plus any custom ones we added
  (right now that's the compound `(provider, external_id)` on
  emsc_events, and `updated_at` on emsc_soak_meta).

## Design decisions

1. **JSONL over BSON dump.** `mongodump` produces BSON which is
   compact but opaque. JSONL means Paul can `head dump/audit_log.jsonl`
   and eyeball anything he doesn't want migrated (PII sanity check
   before pushing to Atlas). Since our data is small (single-digit GB
   estimated), size isn't a constraint.

2. **Idempotent restore with `--upsert-mode`.** If a row already
   exists in the destination (e.g. we're rerunning after a partial
   failure), we update by `_id` rather than duplicate. Turns migration
   from a one-shot into a resumable process.

3. **Preserves ObjectId, datetime, and Binary.** The default
   `json.dumps` mangles all three. We use `bson.json_util` (canonical
   extended JSON) which round-trips losslessly.

4. **No implicit collection filtering.** Every collection ships,
   including the ones that could be considered "cache" like
   `emsc_events` — because our soak-continuity story depends on
   preserving the entire history. If Paul later wants to prune we do
   it with a separate script that runs post-migration on the
   destination.

5. **Reads and writes are serial, not parallel.** Motor could do this
   faster with `asyncio.gather` but this script runs once, so
   simplicity beats speed. Serial also makes progress readable in a
   terminal (log per collection).

6. **Runs from anywhere.** Both URLs are passed via CLI/env, so the
   same script works from Paul's laptop, from a CI job, from inside
   an Emergent pod, or from a temp Fly.io machine. No hardcoded
   endpoints.

## Usage

Export only (creates ./dump/):

    python migrate_mongo.py export \
        --src "$SRC_MONGO_URL" \
        --db  quakeguard_prod \
        --out ./dump

Restore only (assumes ./dump/ already exists):

    python migrate_mongo.py restore \
        --dst "$DST_MONGO_URL" \
        --db  quakeguard_prod \
        --in  ./dump

End-to-end (export then restore):

    python migrate_mongo.py migrate \
        --src "$SRC_MONGO_URL" \
        --dst "$DST_MONGO_URL" \
        --db  quakeguard_prod

## What this DOES NOT do

- Does not stop writes on the source. If you run this without
  freezing writes, you may miss records written between the export
  cursor snapshot and the import completion. Follow the runbook in
  /app/memory/migration-checklist.md for the freeze protocol.

- Does not migrate connection-limit or Atlas-specific settings. Those
  are set once in the Atlas console.

- Does not migrate change streams, GridFS, or sharding metadata.
  QuakeAngel uses none of those (as of 2026-08-06).
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, UpdateOne
from pymongo.errors import BulkWriteError


# System collections that Mongo creates itself. We skip these so we
# don't try to migrate index metadata or `system.views`.
SKIP_COLLECTIONS = {"system.indexes", "system.users", "system.version"}

# Chunk size for both export streaming and restore bulk writes. 500
# keeps peak memory under ~50MB even for wide docs like emsc_events.
CHUNK_SIZE = 500


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[{_iso_now()}] {msg}", flush=True)


async def _collection_names(client: AsyncIOMotorClient, db_name: str) -> list[str]:
    names = await client[db_name].list_collection_names()
    return sorted(n for n in names if n not in SKIP_COLLECTIONS)


# -----------------------------------------------------------------------------
# EXPORT
# -----------------------------------------------------------------------------

async def export_db(src_url: str, db_name: str, out_dir: Path) -> dict:
    """Dump every collection in `db_name` to `out_dir/<collection>.jsonl`.

    Returns a summary dict: {collection_name: rows_written}. Also
    writes `_manifest.json` recording total counts and index info,
    which restore() reads back to verify integrity.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    client = AsyncIOMotorClient(src_url)
    db = client[db_name]
    summary: dict = {"db_name": db_name, "started_at": _iso_now(), "collections": {}}

    try:
        collections = await _collection_names(client, db_name)
        _log(f"Found {len(collections)} collections in {db_name}: {collections}")

        for name in collections:
            col = db[name]
            file_path = out_dir / f"{name}.jsonl"
            count = 0

            # Snapshot indexes so restore() can recreate custom ones.
            # We do NOT recreate _id_ (Mongo makes that automatically).
            indexes = []
            async for idx in col.list_indexes():
                idx_dict = dict(idx)
                if idx_dict.get("name") == "_id_":
                    continue
                # Drop v/ns fields — Mongo sets these on creation.
                for k in ("v", "ns"):
                    idx_dict.pop(k, None)
                indexes.append(idx_dict)

            with file_path.open("w", encoding="utf-8") as f:
                cursor = col.find({})
                async for doc in cursor:
                    f.write(json_util.dumps(doc) + "\n")
                    count += 1

            summary["collections"][name] = {
                "rows": count,
                "indexes": indexes,
                "file": str(file_path.name),
            }
            _log(f"  exported {name}: {count} rows ({len(indexes)} custom indexes)")
    finally:
        client.close()

    summary["finished_at"] = _iso_now()
    manifest_path = out_dir / "_manifest.json"
    manifest_path.write_text(json_util.dumps(summary, indent=2))
    _log(f"Wrote manifest to {manifest_path}")
    return summary


# -----------------------------------------------------------------------------
# RESTORE
# -----------------------------------------------------------------------------

async def restore_db(dst_url: str, db_name: str, in_dir: Path, *, drop_first: bool = False) -> dict:
    """Restore every `.jsonl` in `in_dir` into `dst_url`/`db_name`.

    If `drop_first=True`, collections in the destination are dropped
    before import (dangerous — only for a first-time migration into
    an empty Atlas cluster). The default is False so re-running the
    script is safe (upserts by _id).
    """
    manifest_path = in_dir / "_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No manifest at {manifest_path} — run export first, or restore into the "
            "same directory the export wrote to."
        )
    manifest = json_util.loads(manifest_path.read_text())
    _log(f"Restoring db {manifest['db_name']!r} ({len(manifest['collections'])} collections) into {db_name!r}")

    client = AsyncIOMotorClient(dst_url)
    db = client[db_name]
    result: dict = {"db_name": db_name, "started_at": _iso_now(), "collections": {}}

    try:
        for name, meta in manifest["collections"].items():
            col = db[name]
            file_path = in_dir / meta["file"]
            expected_rows = meta["rows"]
            if drop_first:
                await col.drop()
                _log(f"  dropped destination {name}")

            written = 0
            with file_path.open("r", encoding="utf-8") as f:
                buf: list[UpdateOne] = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    doc = json_util.loads(line)
                    _id = doc.get("_id")
                    if _id is None:
                        # No _id — insert as-is (Mongo will assign one).
                        # Not idempotent but not our problem: every collection
                        # we have does carry an _id.
                        await col.insert_one(doc)
                        written += 1
                        continue
                    buf.append(UpdateOne({"_id": _id}, {"$set": doc}, upsert=True))
                    if len(buf) >= CHUNK_SIZE:
                        try:
                            r = await col.bulk_write(buf, ordered=False)
                            written += (r.upserted_count + r.modified_count + r.matched_count)
                        except BulkWriteError as e:
                            _log(f"  WARN bulk write partial ({name}): {e.details.get('writeErrors', [])[:3]}")
                        buf = []
                if buf:
                    try:
                        r = await col.bulk_write(buf, ordered=False)
                        written += (r.upserted_count + r.modified_count + r.matched_count)
                    except BulkWriteError as e:
                        _log(f"  WARN bulk write partial ({name}): {e.details.get('writeErrors', [])[:3]}")

            # Recreate indexes.
            for idx in meta.get("indexes", []):
                key = idx.get("key")
                if not key:
                    continue
                opts = {k: v for k, v in idx.items() if k not in ("key", "name")}
                idx_name = idx.get("name")
                if idx_name:
                    opts["name"] = idx_name
                try:
                    await col.create_index(list(key.items()), **opts)
                except Exception as e:
                    _log(f"  WARN index create failed on {name}.{idx_name}: {e}")

            # Verify row count. Non-fatal — log discrepancy but continue.
            actual = await col.count_documents({})
            result["collections"][name] = {
                "expected": expected_rows,
                "restored_upserts": written,
                "final_count": actual,
                "match": actual == expected_rows,
            }
            status = "OK" if actual == expected_rows else f"MISMATCH ({actual} vs {expected_rows})"
            _log(f"  restored {name}: {actual} rows [{status}]")
    finally:
        client.close()

    result["finished_at"] = _iso_now()
    return result


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QuakeAngel Mongo migration.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="Dump source DB to JSONL files.")
    p_exp.add_argument("--src", default=os.environ.get("SRC_MONGO_URL"), required=False)
    p_exp.add_argument("--db",  required=True)
    p_exp.add_argument("--out", default="./dump", type=Path)

    p_res = sub.add_parser("restore", help="Load JSONL files into destination DB.")
    p_res.add_argument("--dst", default=os.environ.get("DST_MONGO_URL"), required=False)
    p_res.add_argument("--db",  required=True)
    p_res.add_argument("--in",  dest="in_dir", default="./dump", type=Path)
    p_res.add_argument("--drop", action="store_true", help="Drop destination collections first.")

    p_mig = sub.add_parser("migrate", help="Export then restore end-to-end.")
    p_mig.add_argument("--src", default=os.environ.get("SRC_MONGO_URL"), required=False)
    p_mig.add_argument("--dst", default=os.environ.get("DST_MONGO_URL"), required=False)
    p_mig.add_argument("--db",  required=True)
    p_mig.add_argument("--out", default="./dump", type=Path)
    p_mig.add_argument("--drop", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "export":
        if not args.src:
            parser.error("--src or SRC_MONGO_URL env is required")
        asyncio.run(export_db(args.src, args.db, args.out))
    elif args.cmd == "restore":
        if not args.dst:
            parser.error("--dst or DST_MONGO_URL env is required")
        asyncio.run(restore_db(args.dst, args.db, args.in_dir, drop_first=args.drop))
    elif args.cmd == "migrate":
        if not args.src or not args.dst:
            parser.error("--src (or SRC_MONGO_URL) and --dst (or DST_MONGO_URL) required")

        async def _both():
            await export_db(args.src, args.db, args.out)
            await restore_db(args.dst, args.db, args.out, drop_first=args.drop)
        asyncio.run(_both())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
