"""Single-host delivery backfill and legacy-layout maintenance coordinator."""
import logging
from ..core.file_lock import exclusive_file_lock
from ..core.paths import PROJECT_ROOT
from .database import SessionLocal
from .storage import lock_storage_migration, storage_migration
from .delivery_index import backfill_pending_project
from .artifact_maintenance import retire_legacy_artifacts, remove_retired_files


def run(stop):
    logger = logging.getLogger("audiobook.worker")
    while not stop.is_set():
        try:
            with exclusive_file_lock(PROJECT_ROOT / ".narrify" / "delivery-maintenance.lock", timeout=0):
                cursor = ""
                scanned = False
                while not stop.is_set():
                    try:
                        next_cursor, next_scanned = cursor, scanned
                        with SessionLocal() as db:
                            paused = False
                            if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
                                pending, retired = False, []
                                paused = True
                            else:
                                pending = backfill_pending_project(db)
                                retired = []
                                if not scanned:
                                    next_cursor, retired = retire_legacy_artifacts(db, cursor)
                                    next_scanned = not next_cursor
                                db.commit()
                        cursor, scanned = next_cursor, next_scanned
                        remove_retired_files(retired)
                        stop.wait(10 if paused else 0.2 if pending or not scanned else 10)
                    except Exception:
                        logger.exception("Delivery index maintenance failed; retrying")
                        stop.wait(10)
        except TimeoutError:
            stop.wait(10)
        except Exception:
            logger.exception("Delivery maintenance coordinator could not acquire ownership; retrying")
            stop.wait(10)
