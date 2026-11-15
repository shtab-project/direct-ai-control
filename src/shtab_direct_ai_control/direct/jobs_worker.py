"""Read-only assistant/scheduler loop; no advertising executor is imported here."""
import signal,threading,time
from pathlib import Path
from .main import app  # Register the same models/routes, without running lifespan.
from .assistant_jobs import tick

stopping=threading.Event()
def heartbeat():
    while not stopping.is_set():
        Path('/tmp/direct-ai-reader-heartbeat').write_text(str(time.time()))
        stopping.wait(5)
def main():
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *a:stopping.set())
    threading.Thread(target=heartbeat,daemon=True).start()
    while not stopping.is_set():
        try:
            from .daily_scheduler import schedule_due as schedule_daily
            schedule_daily()
        except Exception:pass
        try:
            from .checkpoints import schedule_due
            schedule_due()
        except ImportError:pass
        except Exception:pass  # No exception bodies/credentials in service logs.
        try:tick()
        except Exception:pass
        try:
            from .telegram import tick as notify
            notify()
        except Exception:pass
        stopping.wait(2)
if __name__=='__main__':main()
