"""Per-conversation FIFO for drawing work, independent of the chat lock."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass


@dataclass(eq=False)
class Ticket:
    origin: str
    ready: asyncio.Future
    position: int
    submitted_at: float
    released: bool = False


class DrawingQueue:
    def __init__(self):
        self.lanes: dict[str, deque[Ticket]] = {}

    def reserve(self, origin: str) -> Ticket:
        lane = self.lanes.setdefault(origin, deque())
        ticket = Ticket(origin, asyncio.get_running_loop().create_future(), len(lane) + 1, time.perf_counter())
        lane.append(ticket)
        if len(lane) == 1:
            ticket.ready.set_result(None)
        return ticket

    async def wait(self, ticket: Ticket, timeout: float) -> int:
        # Cancelling a waiter must not cancel the signal needed by its successor.
        await asyncio.wait_for(asyncio.shield(ticket.ready), timeout=timeout)
        return round((time.perf_counter() - ticket.submitted_at) * 1000)

    def release(self, ticket: Ticket) -> None:
        # Both the coroutine finally and its done callback can run cleanup.
        if ticket.released:
            return
        ticket.released = True
        lane = self.lanes.get(ticket.origin)
        if not lane:
            return
        lane.remove(ticket)
        if not lane:
            self.lanes.pop(ticket.origin, None)
        elif not lane[0].ready.done():
            lane[0].ready.set_result(None)
