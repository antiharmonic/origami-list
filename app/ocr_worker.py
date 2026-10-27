"""The `ocr` service: keeps data/ocr.db in step with the books folder, one book at a time.

Run with: python -m app.ocr_worker   (compose runs it under `nice`, with a memory cap)
"""
import os
import time

from . import books, db, ocr

IDLE_SLEEP = 300
PAUSE_SLEEP = 10


def log(msg):
    print(f"ocr: {msg}", flush=True)


def added_times():
    """{path: added_at} from the app's database (falling back to ctime for files it hasn't recorded yet)."""
    conn = db.connect()
    try:
        db.migrate(conn)
        return {**books.ctimes(), **dict(conn.execute("SELECT path, added_at FROM book_files").fetchall())}
    finally:
        conn.close()


def step(conn, workers):
    """Do one unit of work; returns how long to sleep before the next one."""
    if ocr.get_control(conn)["paused"]:
        return PAUSE_SLEEP
    present = books.scan(force=True)
    ocr.sync(conn, ocr.sizes_and_mtimes(present))
    path = ocr.next_book(conn, present, added_times())
    if path is None:
        return IDLE_SLEEP
    ocr.process_book(conn, path, workers, log=log)
    return 0


def main():
    workers = int(os.environ.get("OCR_WORKERS", "4"))
    conn = ocr.connect()
    # A book left "running" by a crash or restart just carries on where its pages stopped.
    with conn:
        conn.execute("UPDATE ocr_books SET state = 'queued' WHERE state = 'running'")
    log(f"started with {workers} workers")
    while True:
        try:
            wait = step(conn, workers)
        except Exception as e:  # keep the service alive; the next pass retries
            log(f"error: {type(e).__name__}: {e}")
            wait = 60
        if wait:
            time.sleep(wait)


if __name__ == "__main__":
    main()
