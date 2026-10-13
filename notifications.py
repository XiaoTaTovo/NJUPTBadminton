"""One bounded notification worker. Never submits requests or writes recovery data."""
import queue
import threading
import time


class Notifications:
    def __init__(self, callback):
        self.callback = callback
        self.queue = queue.Queue(maxsize=64)
        self.stopping = threading.Event()
        self.failed = False
        self.dropped = 0
        self.timings = []
        self.thread = threading.Thread(target=self._work, name='booking-notifications', daemon=True)
        self.thread.start()

    def _work(self):
        while True:
            try:
                message = self.queue.get(timeout=.1)
            except queue.Empty:
                if self.stopping.is_set(): return
                continue
            try:
                if message is None:
                    return
                start = time.perf_counter_ns()
                try:
                    self.callback(message)
                except Exception:
                    self.failed = True  # No raw exception text in logs.
                finally:
                    self.timings.append((time.perf_counter_ns()-start)/1e6)
            finally:
                self.queue.task_done()

    def send(self, message):
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            self.dropped += 1

    def close(self):
        self.stopping.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            self.dropped += 1
        self.thread.join(timeout=.1)
        return dict(failed=self.failed, dropped=self.dropped, pending=self.thread.is_alive(),
                    durations_ms=list(self.timings))
