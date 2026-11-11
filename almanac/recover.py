"""Stalled squad agents: a session Almanac spawned into an active squad that stopped on an API error and sits idle
(`fleet.read_tail` sets `meta["stall"]`) is sent `continue`, up to three times, then escalated once.

Run from the door's ingest loop (`Service.loop`), so it is checked every LIVE_INTERVAL seconds while agents run;
the delays below are minimums. Only sessions with a `spawned_agents` row in an active squad are touched, whatever
their role: anything else may be the operator's own terminal. Nothing here depends on another agent being alive;
the operator always hears an escalation, the agent that added the stalled one (`spawned_by`) only if it is live and
not stalled itself. An escalation names the agent's open tasks and the step beyond `continue`: a respawn on the task
(door.respawn_task), which recover never does by itself. A respawn keeps the handle, so the fresh session working
closes the escalation like the old one recovering would.

Every message here is journaled as a notice (`answers=False`) with transport `system`: a `continue` from
`almanac-hub` (an operator name) would otherwise count as the operator answering the agent's open question or
writing to it, and its transcript copy, once ingested, would close the question too (the journaled row makes ingest
skip the copy). `system` also keeps these rows off the topology and the swimlane.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

# the `error` field of Claude Code's API-error record. Seen in this PC's transcripts (2026-11-11): server_error (2,
# "Connection lost mid-response"), authentication_failed (69, "Not logged in"), model_not_found (1). Only
# server_error is observed as transient; the other two names are the API's overload and timeout classes. An unknown
# class is never retried: it is escalated.
TRANSIENT = frozenset({"server_error", "overloaded_error", "timeout"})
DELAYS = (20, 60, 180)          # seconds: the stall to try 1, try 1 to try 2, try 2 to try 3
GRACE = DELAYS[0]               # after the last try, how long a stall must outlast it before escalating
TEXT = "continue"
HUB = "almanac-hub"
PREFIX = "[stalled] "           # an escalation's first word after it is the agent, so a restart can find it again
log = logging.getLogger("almanac")


def _epoch(ts, default: float) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return default


class Recover:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.state: dict[str, dict] = {}        # session id -> {start, tries, last, escalated}

    def tick(self, service) -> list[dict]:
        """One pass. Returns what it did ({action, agent, ...}), for tests and the log."""
        if not getattr(service, "auto_continue", True) or service.fleet is None:
            self.state.clear()
            return []
        now, done, stalled = self.clock(), [], set()
        flagged = _open_escalations(service)              # agent -> its open escalation, across restarts
        for row in service.fleet_now().get("processes", []):
            sid = row.get("session_id")
            # by session, however many spawns came after it (`spawned` returns only the newest 500)
            sp = service.store.spawn_by_session(sid) if sid and row.get("role") == "session" else None
            if sp is None or not sp.get("squad_id"):
                continue                                  # not ours to type into
            stall = service.fleet.view(sid)[2].get("stall")
            escs = flagged.get((sp["agent_name"], sp["squad_id"]), [])
            if not stall:
                if escs:                                  # working again: the operator's alert closes itself
                    _resolve(service, [i for i, _ in escs])
                    done.append(dict(action="resolved", agent=sp["agent_name"]))
                continue                                  # its state is dropped below
            squad = service.store.squad(sp["squad_id"])
            if not squad or squad.get("status") != "active":
                continue
            stalled.add(sid)
            # one stall is one unbroken run of errors (fleet's `since`): a reply in between, even inside one tick,
            # makes a new stall with fresh tries, and an escalation from before it began belonged to the old one
            since = _epoch(stall.get("since") or stall.get("ts"), now)
            _resolve(service, [i for i, ts in escs if ts < since])
            current = [i for i, ts in escs if ts >= since]
            st = self.state.get(sid)
            if st is None or st["start"] != since:
                # a stall already escalated before a restart is not tried or escalated again
                st = self.state[sid] = dict(start=since, tries=0, last=None, escalated=bool(current))
            if row.get("status") != "idle":
                continue                                  # a try in flight
            if st["escalated"]:
                continue
            error = stall.get("error") or "unknown"
            if error not in TRANSIENT:
                done.append(self._escalate(service, row, sp, stall, st, f"{error} is not retried"))
            elif st["tries"] < len(DELAYS):
                if now >= (st["last"] if st["tries"] else st["start"]) + DELAYS[st["tries"]]:
                    done.append(self._continue(service, row, sp, stall, st, now))
            elif now >= st["last"] + GRACE:
                done.append(self._escalate(service, row, sp, stall, st, f"{len(DELAYS)} tries did not get it going"))
        for sid in [s for s in self.state if s not in stalled]:
            del self.state[sid]                           # recovered, ended, or out of scope: the next stall starts over
        return done

    def _continue(self, service, row, sp, stall, st, now) -> dict:
        st["tries"] += 1
        st["last"] = now
        ok, message = _deliver(service, sp["agent_name"], TEXT, sp["squad_id"], row, ts=now)
        service.store.log_action("continue", row["pid"], dict(session_id=row.get("session_id"), name=sp["agent_name"]),
                                 ok, f"try {st['tries']}/{len(DELAYS)} after {stall.get('error')}: {message}", HUB)
        return dict(action="continue", agent=sp["agent_name"], try_=st["tries"], ok=ok)

    def _escalate(self, service, row, sp, stall, st, reason) -> dict:
        st["escalated"] = True
        name, squad = sp["agent_name"], sp["squad_id"]
        text = (f"{PREFIX}{name} (squad {squad}) stopped on an API error and is idle: {stall.get('error')}: "
                f"{(stall.get('text') or '')[:200]}. Auto-continue tries: {st['tries']}. {reason}; it needs a hand."
                + _respawn_hint(service, sp))
        told = []
        boss = sp.get("spawned_by")
        if boss:
            peers = {p.get("name"): p for p in service.mesh_peers(squad_id=squad)}
            if boss in peers and not peers[boss].get("stall") and _deliver(service, boss, text, squad, peers[boss], ts=self.clock())[0]:
                told.append(boss)
        # flagged blocking, so it raises the operator's alert (header badge, "blocked on operator") like an agent's
        # blocking question; it closes when the agent works again, or by hand
        if _deliver(service, "operator", text, squad, needs="blocking", ts=self.clock())[0]:
            told.append("operator")
        service.store.log_action("escalate", row["pid"], dict(session_id=row.get("session_id"), name=name), bool(told),
                                 f"{reason}; told {', '.join(told) or 'nobody'}", HUB)
        return dict(action="escalate", agent=name, told=told)


def _respawn_hint(service, sp: dict) -> str:
    """The step beyond `continue`, when the stalled agent owns an open task: respawn it there, from the task's log."""
    from almanac.store import TASK_STATUSES
    open_ = [s for s in TASK_STATUSES if s != "accepted"]
    owned = [o for o in service.store.objects(squad_id=sp["squad_id"], kind="task", statuses=open_)
             if o["owner_session"] == sp["session_id"]]
    if not owned:
        return ""
    refs = ", ".join(f"T-{o['id']}" for o in owned)
    return (f" It owns {refs}: respawn it there from the task's log with mesh_respawn(\"T-{owned[0]['id']}\") "
            "or the [RESPAWN] button on the task in PLAN.")


def _open_escalations(service) -> dict[tuple[str, str], list[tuple[int, float]]]:
    """(agent, squad) -> [(id, ts)] of its unanswered escalations to the operator."""
    out: dict = {}
    for m in service.store.open_needs(to_name="operator"):
        if m["from_name"] == HUB and m["transport"] == "system" and m["body"].startswith(PREFIX):
            out.setdefault((m["body"][len(PREFIX):].split(" ", 1)[0], m["squad_id"]), []).append((m["id"], m["ts"]))
    return out


def _resolve(service, ids) -> None:
    for i in ids:
        service.store.resolve_message(i)


def _deliver(service, to: str, text: str, squad: str, row: dict | None = None, needs: str | None = None,
             ts: float | None = None) -> tuple[bool, str]:
    """Send `text` to a live session (`row`: pid, created) through the fleet's send, as `/api/send` does, and journal
    it as a system notice that closes no flag. Without a row (the operator), journaling is the delivery."""
    ok, message = True, "journaled"
    if row is not None:
        hub_pipe = service.hub_peer.pipe_path if getattr(service, "hub_peer", None) else None
        try:
            ok, message, _ = service.fleet.send(row["pid"], row["created"], text, from_name=HUB, from_pipe=hub_pipe)
        except Exception as exc:                          # noqa: BLE001  a failed send still counts as a try
            ok, message = False, str(exc)
    if ok:
        # stamped with recover's clock: an escalation is matched to its stall by comparing this with the stall's start
        service.store.record_message(from_name=HUB, to_name=to, to_session=(row or {}).get("session_id"), ts=ts,
                                     squad_id=squad, body=text, transport="system", needs=needs, answers=False)
    else:
        log.warning("recover: could not reach %s: %s", to, message)
    return ok, message


def tick(service, clock=time.time) -> list[dict]:
    """The door's hook: one Recover per Service, kept across passes; never raises into the ingest loop."""
    rec = service.__dict__.get("_recover")
    if rec is None:
        rec = service.__dict__["_recover"] = Recover(clock)
    try:
        return rec.tick(service)
    except Exception:                                     # noqa: BLE001
        log.exception("recover: pass failed")
        return []
