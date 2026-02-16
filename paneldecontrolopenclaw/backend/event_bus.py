"""Simple in-process event bus for live updates.

This module implements a very simple pub/sub mechanism backed by a
``queue.Queue``.  The proxy publishes events into the queue as they
occur (e.g. request sent, token received, request finished).  The
graphical user interface can subscribe to the queue and process
events in near real-time without having to poll the database or read
log files continuously.  Each event is represented as a dictionary
with the run identifier, event type, optional details and a
timestamp.

This design avoids the complexity of external message brokers.  Since
both the proxy and the UI run within the same Python process, a
simple in-memory queue suffices.  If the application architecture
changes in the future (for example, running the proxy as a separate
process), this module can be adapted to use sockets or other IPC
mechanisms while preserving the same ``publish_event`` and
``subscribe`` interfaces.
"""

from __future__ import annotations

import queue
from typing import Any, Dict, Iterator, Optional

# Internal global queue used for event delivery.  The queue is not
# bounded because events are relatively infrequent.  Consumers are
# expected to drain the queue regularly to avoid unbounded growth.
_event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()


def publish_event(run_id: str, event_type: str, details: Optional[str] = None, timestamp: Optional[float] = None) -> None:
    """Publish an event to the global event queue.

    Parameters
    ----------
    run_id : str
        Identifier of the run this event relates to.
    event_type : str
        Short code identifying the type of event (e.g. "request_sent",
        "first_token", "stream_finished").
    details : str, optional
        Additional textual details for the event.  Should be short; the
        UI truncates long strings.
    timestamp : float, optional
        Timestamp of the event.  If omitted the timestamp will be set
        on the consumer side when the UI receives the event.
    """
    evt: Dict[str, Any] = {
        "run_id": run_id,
        "event": event_type,
        "details": details,
        "timestamp": timestamp,
    }
    _event_queue.put(evt)


def subscribe() -> "queue.Queue[Dict[str, Any]]":
    """Return the global event queue for consumers.

    Consumers should call ``get()`` or ``get_nowait()`` on the
    returned queue to receive events.  The queue will contain
    dictionaries with the keys ``run_id``, ``event``, ``details`` and
    ``timestamp``.
    """
    return _event_queue