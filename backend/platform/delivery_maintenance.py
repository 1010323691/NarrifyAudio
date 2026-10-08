"""Single-host recovery, outbox, delivery and retention maintenance coordinator."""
import logging
import time
from ..core.audio_probe_cache import prune_cache
from ..core.file_lock import exclusive_file_lock
from ..core.paths import PROJECT_ROOT
from .database import SessionLocal
from .storage import lock_storage_migration, storage_migration
from .delivery_index import backfill_pending_project
from .artifact_maintenance import retire_legacy_artifacts, remove_retired_files


def run_dispatch_once(*, recover, publish):
    """One-shot workers also respect an already running maintenance owner."""
    acquired = False
    try:
        with exclusive_file_lock(PROJECT_ROOT / ".narrify" / "host-maintenance.lock", timeout=0):
            acquired = True
            recover(limit=100)
            publish(limit=100)
            return True
    except TimeoutError:
        if acquired:
            raise
        return False


class MaintenanceSchedule:
    """Schedules bounded callbacks without holding a session across waits or calls."""

    def __init__(self, *, recover=None, publish=None, retention=None, refresh=None):
        self.recover, self.publish, self.retention = recover, publish, retention
        self.cursor = ""
        self.next_recovery = self.next_publish = self.next_retention = 0.
        self.retention_startup = True
        self.refresh = refresh
        self.next_refresh = 0.

    def tick(self):
        logger = logging.getLogger("audiobook.worker")
        now = time.monotonic()
        if self.refresh is not None and now >= self.next_refresh:
            self.next_refresh = now + 1
            try:
                self.refresh(limit=100)
            except Exception:
                logger.exception("Progress refresh maintenance failed; dirty state retained")
        if self.recover is not None and now >= self.next_recovery:
            self.next_recovery = now + 10
            try:
                _count, self.cursor = self.recover(limit=100, after_id=self.cursor)
            except Exception:
                logger.exception("Task lease recovery failed; cursor retained")
        if self.publish is not None and now >= self.next_publish:
            self.next_publish = now + 1
            try:
                self.publish(limit=100)
            except Exception:
                logger.exception("Outbox maintenance failed; pending events retained")
        if self.retention is not None and now >= self.next_retention:
            self.next_retention = now + 60 if self.retention_startup else now + 86400
            try:
                result = self.retention()
                purged, incomplete = result if isinstance(result, tuple) else (result, False)
                # A bounded round that hit its work budget continues next pass
                # so a large backlog cannot stall dispatch for a whole day.
                if incomplete:
                    self.next_retention = now + 60
                elif purged or not self.retention_startup:
                    self.next_retention = now + 86400
            except Exception:
                logger.exception("Retention maintenance failed; will retry")
                self.next_retention = now + 60
            self.retention_startup = False


def run(stop, *, recover=None, publish=None, retention=None, refresh=None):
    logger = logging.getLogger("audiobook.worker")
    while not stop.is_set():
        try:
            with exclusive_file_lock(PROJECT_ROOT / ".narrify" / "host-maintenance.lock", timeout=0):
                schedule = MaintenanceSchedule(recover=recover, publish=publish, retention=retention, refresh=refresh)
                cursor = ""
                scanned = False
                last_prune = time.monotonic()
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
                        if not paused:
                            schedule.tick()
                        if not paused and time.monotonic() - last_prune >= 3600:
                            prune_cache()
                            last_prune = time.monotonic()
                        stop.wait(10 if paused else 0.2 if pending or not scanned else 1 if publish is not None else 10)
                    except Exception:
                        logger.exception("Delivery index maintenance failed; retrying")
                        stop.wait(10)
        except TimeoutError:
            stop.wait(10)
        except Exception:
            logger.exception("Delivery maintenance coordinator could not acquire ownership; retrying")
            stop.wait(10)
