"""
Shared event state updated by the scheduler and streamed to the frontend via SSE.
"""
import datetime
import threading

_lock = threading.Lock()

_state = {
    "status": "idle",            # idle | running
    "job_type": None,            # unfollow | follow | scan | check_follow_back
    "current": 0,
    "total": 0,
    "last_action": None,         # follow | unfollow | error | scan
    "last_username": None,
    "last_action_success": True,
    "last_action_time": None,    # ISO string
    "errors_session": 0,
    "follows_today": 0,
    "unfollows_today": 0,
    "next_run_seconds": None,    # estimated seconds until next action (during delays)
}


def update(**kwargs):
    with _lock:
        _state.update(kwargs)
        if "last_action" in kwargs:
            _state["last_action_time"] = datetime.datetime.utcnow().isoformat()


def job_start(job_type: str, total: int):
    with _lock:
        _state["status"] = "running"
        _state["job_type"] = job_type
        _state["current"] = 0
        _state["total"] = total
        _state["next_run_seconds"] = None


def job_action(action: str, username: str, success: bool, current: int,
               delay_seconds: int = 0):
    with _lock:
        _state["current"] = current
        _state["last_action"] = action
        _state["last_username"] = username
        _state["last_action_success"] = success
        _state["last_action_time"] = datetime.datetime.utcnow().isoformat()
        _state["next_run_seconds"] = delay_seconds if delay_seconds > 0 else None
        if not success:
            _state["errors_session"] += 1
        if action == "follow" and success:
            _state["follows_today"] += 1
        if action == "unfollow" and success:
            _state["unfollows_today"] += 1


def job_complete(job_type: str):
    with _lock:
        _state["status"] = "idle"
        _state["job_type"] = None
        _state["next_run_seconds"] = None


def reset_daily():
    with _lock:
        _state["follows_today"] = 0
        _state["unfollows_today"] = 0
        _state["errors_session"] = 0


def get_state() -> dict:
    with _lock:
        return dict(_state)
