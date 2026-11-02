"""Best-effort, process-local descriptor headroom; not a leak remedy."""
import logging
import os

log = logging.getLogger("canvas_hub.resources")
DEFAULT_NOFILE = 1024
MAX_NOFILE = 4096


def configure_nofile():
    try:
        import resource
    except ImportError:
        log.info("File descriptor limits are not available on this platform")
        return
    try:
        target = int(os.environ.get("CANVAS_HUB_NOFILE", DEFAULT_NOFILE))
        if not 1 <= target <= MAX_NOFILE:
            raise ValueError
    except ValueError:
        log.warning("Invalid CANVAS_HUB_NOFILE; using %d", DEFAULT_NOFILE)
        target = DEFAULT_NOFILE
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != resource.RLIM_INFINITY:
            desired = target if hard == resource.RLIM_INFINITY else min(target, hard)
            if desired > soft:
                resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    except (OSError, ValueError) as exc:
        log.warning("Could not adjust or read descriptor limits (%s); check process limits if resources are exhausted", type(exc).__name__)
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        log.warning("Canvas Hub descriptor limits: soft=%s hard=%s", soft, hard)
    except (OSError, ValueError):
        log.warning("Canvas Hub descriptor limits could not be read")
