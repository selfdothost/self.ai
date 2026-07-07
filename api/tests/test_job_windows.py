# test_job_windows.py
import time
import uuid
from selfai_ui.models.job_windows import JobWindow, JobWindows, JobWindowForm, JobWindowSlotForm, _window_status


def test_window_status_upcoming():
    now = int(time.time())
    w = JobWindow(start_at=now + 3600, end_at=now + 7200, enabled=True)
    assert _window_status(w) == "upcoming"

def test_window_status_active():
    now = int(time.time())
    w = JobWindow(start_at=now - 60, end_at=now + 3600, enabled=True)
    assert _window_status(w) == "active"

def test_window_status_completed():
    now = int(time.time())
    w = JobWindow(start_at=now - 7200, end_at=now - 3600, enabled=True)
    assert _window_status(w) == "completed"

# ---- get_active_window (DB-dependent) ----

def _make_window(db_session, *, start_at, end_at, enabled=True, preferred_job_type="training"):
    """Insert a minimal JobWindow row for testing."""
    now = int(time.time())
    w = JobWindow(
        id=str(uuid.uuid4()),
        name="test window",
        start_at=start_at,
        end_at=end_at,
        preferred_job_type=preferred_job_type,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )
    db_session.add(w)
    db_session.commit()
    return w


def test_get_active_window_none_when_empty(db_session):
    assert JobWindows.get_active_window() is None


def test_get_active_window_returns_active(db_session):
    now = int(time.time())
    _make_window(db_session, start_at=now - 60, end_at=now + 3600)
    result = JobWindows.get_active_window()
    assert result is not None
    assert result.status == "active"


def test_get_active_window_ignores_upcoming(db_session):
    now = int(time.time())
    _make_window(db_session, start_at=now + 3600, end_at=now + 7200)
    assert JobWindows.get_active_window() is None


def test_get_active_window_ignores_completed(db_session):
    now = int(time.time())
    _make_window(db_session, start_at=now - 7200, end_at=now - 3600)
    assert JobWindows.get_active_window() is None


def test_get_active_window_ignores_disabled(db_session):
    now = int(time.time())
    _make_window(db_session, start_at=now - 60, end_at=now + 3600, enabled=False)
    assert JobWindows.get_active_window() is None


def test_get_active_window_prefers_latest_start(db_session):
    now = int(time.time())
    _make_window(db_session, start_at=now - 7200, end_at=now + 3600)  # started 2h ago
    w2 = _make_window(db_session, start_at=now - 60, end_at=now + 3600)   # started 1m ago
    result = JobWindows.get_active_window()
    assert result.id == w2.id  # ORDER BY start_at DESC → most recent start wins



def test_insert_new_window_basic(db_session):
    now = int(time.time())
    form = JobWindowForm(name="w1", start_at=now - 60, end_at=now + 3600, preferred_job_type="training")
    result = JobWindows.insert_new_window(form)
    assert result is not None
    assert result.name == "w1"
    assert result.slots == []

def test_insert_new_window_with_slots(db_session):
    now = int(time.time())
    form = JobWindowForm(name="w1", start_at=now - 60, end_at=now + 3600, preferred_job_type="training", slots=[JobWindowSlotForm(job_type="training", max_concurrent=2)])
    result = JobWindows.insert_new_window(form)
    assert result is not None
    assert result.name == "w1"
    assert len(result.slots) == 1
    assert result.slots[0].job_type == "training"

def test_get_all_windows_empty(db_session):
    result = JobWindows.get_all_windows()
    assert result == []


def test_get_all_windows_returns_all(db_session):
    now = int(time.time())
    w1 = JobWindowForm(name="w1", start_at=now - 60, end_at=now + 3600, preferred_job_type="training")
    w2 = JobWindowForm(name="w2", start_at=now - 180, end_at=now + 4800, preferred_job_type="training")
    insert1 = JobWindows.insert_new_window(w1)
    insert2 = JobWindows.insert_new_window(w2)
    result = JobWindows.get_all_windows()
    assert len(result) == 2

def test_get_window_by_id_found(db_session):
    now = int(time.time())
    form = JobWindowForm(name="w1", start_at=now - 60, end_at=now + 3600, preferred_job_type="training", slots=[JobWindowSlotForm(job_type="training", max_concurrent=2)])
    inserted = JobWindows.insert_new_window(form)
    fetched = JobWindows.get_window_by_id(inserted.id)
    assert fetched.id == inserted.id

def test_get_window_by_id_not_found(db_session):
    fetched = JobWindows.get_window_by_id("nope-try-again")
    assert fetched == None

def test_update_window_name(db_session):
    now = int(time.time())
    form = JobWindowForm(name="w1", start_at=now - 60, end_at=now + 3600, preferred_job_type="training", slots=[JobWindowSlotForm(job_type="training", max_concurrent=2)])
    submit = JobWindows.insert_new_window(form)
    result = JobWindows.update_window(submit.id, {"name": "renamed"})
    assert result.name == "renamed"

def test_update_window_replaces_slots(db_session):
    now = int(time.time())
    form = JobWindowForm(name="w1", start_at=now - 60, end_at=now + 3600, preferred_job_type="training", slots=[JobWindowSlotForm(job_type="training", max_concurrent=2)])
    submit = JobWindows.insert_new_window(form)
    result = JobWindows.update_window(submit.id, {"slots": [{"job_type": "language-eval", "max_concurrent": 1}]})
    assert result.slots[0].job_type == "language-eval"

def test_delete_window(db_session):
    now = int(time.time())
    form = JobWindowForm(name="w1", start_at=now - 60, end_at=now + 3600, preferred_job_type="training", slots=[JobWindowSlotForm(job_type="training", max_concurrent=2)])
    result = JobWindows.insert_new_window(form)
    delete = JobWindows.delete_window(result.id)
    assert delete == True
    assert JobWindows.get_window_by_id(result.id) == None
def test_window_status_disabled():
    # A disabled window inside its time range should return "disabled", not "completed"
    now = int(time.time())
    w = JobWindow(start_at=now - 60, end_at=now + 3600, enabled=False)
    assert _window_status(w) == "disabled"
