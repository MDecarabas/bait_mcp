import pytest

from bait_mcp import qserver_client as qc


class FakeAPI:
    """Records calls and returns canned responses shaped like the real API."""

    def __init__(self, **kwargs):
        self.calls = []
        self.task_result_value = None
        self.task_results = None  # optional list, popped per task_result call
        self.script_uploads = 0

    def script_upload(self, script, *, update_lists=None, update_re=None, run_in_background=None):
        self.script_uploads += 1
        return {"task_uid": "S1"}

    def function_execute(self, item, *, run_in_background=None, user=None, user_group=None):
        self.calls.append(("function_execute", item.to_dict(), run_in_background))
        return {"task_uid": "T1"}

    def wait_for_completed_task(self, task_uid, *, timeout=None):
        return {"status": "completed"}

    def task_result(self, task_uid):
        if self.task_results is not None:
            return self.task_results.pop(0)
        return self.task_result_value

    def plans_allowed(self, *, user_group=None, reload=False):
        return {"plans_allowed": {"count": {"name": "count"}, "scan": {}}}

    def devices_allowed(self, *, user_group=None, reload=False):
        return {"devices_allowed": {"sim_motor": {"classname": "SynAxis"}}}

    def status(self, *, reload=False):
        return {"manager_state": "idle"}

    def item_add(self, item, *, user=None, user_group=None):
        return {"item": item.to_dict(), "qsize": 1}

    def item_execute(self, item, *, user=None, user_group=None):
        return {"item": item.to_dict()}

    def queue_start(self):
        return {"success": True}

    def queue_stop(self):
        return {"success": True}


@pytest.fixture
def client(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(qc, "REManagerAPI", lambda **kwargs: fake)
    cfg = {
        "qserver": {
            "zmq_control_addr": "tcp://x",
            "timeout": 5,
            "user": "u",
            "user_group": "primary",
        }
    }
    return qc.QServerClient(cfg), fake


def test_read_device_runs_in_background_and_returns_value(client):
    c, fake = client
    fake.task_result_value = {
        "result": {"success": True, "return_value": {"sim_motor": {"value": 1.0}}}
    }
    out = c.read_device("sim_motor")
    assert out == {"ok": True, "value": {"sim_motor": {"value": 1.0}}}
    _, item, background = fake.calls[-1]
    assert item["name"] == "read_device" and item["args"] == ["sim_motor"]
    assert background is True


def test_set_device_runs_in_foreground(client):
    c, fake = client
    fake.task_result_value = {"result": {"success": True, "return_value": {"device": "sim_motor"}}}
    out = c.set_device("sim_motor", 5)
    assert out["ok"] is True
    assert fake.calls[-1][2] is False  # foreground -> RE serializes / interlock


def test_function_failure_surfaces_traceback(client):
    c, fake = client
    fake.task_result_value = {"result": {"success": False, "traceback": "KeyError: 'nope'"}}
    out = c.read_device("nope")
    assert out["ok"] is False and "KeyError" in out["error"]


def test_unexpected_task_result_is_error(client):
    c, fake = client
    fake.task_result_value = {"weird": 1}
    assert c.read_device("x")["ok"] is False


def test_api_exception_is_normalized(client, monkeypatch):
    c, fake = client

    def boom(*a, **k):
        raise RuntimeError("no server")

    monkeypatch.setattr(fake, "function_execute", boom)
    assert c.read_device("x") == {"ok": False, "error": "RuntimeError: no server"}


def test_list_plans_and_devices_sorted(client):
    c, _ = client
    assert c.list_plans() == {"ok": True, "plans": ["count", "scan"]}
    assert c.list_devices() == {"ok": True, "devices": ["sim_motor"]}


def test_describe_unknown_is_error(client):
    c, _ = client
    assert c.describe_plan("nope")["ok"] is False
    assert c.describe_device("nope")["ok"] is False


def test_add_and_run_plan(client):
    c, _ = client
    added = c.add_plan("count", [["sim_det"]], {"num": 3})
    assert added["ok"] is True and added["qsize"] == 1
    assert added["item"]["name"] == "count"
    assert c.run_plan("count", [["sim_det"]])["ok"] is True


def test_functions_injected_once(client):
    c, fake = client
    fake.task_result_value = {"result": {"success": True, "return_value": {"motor": {"value": 1}}}}
    c.read_device("sim_motor")
    c.read_device("sim_motor")
    assert fake.script_uploads == 1  # injected on first use, cached thereafter


def test_reinjects_when_function_missing(client):
    c, fake = client
    fake.task_results = [
        {"result": {"success": False, "traceback": "not found in the worker namespace"}},
        {"result": {"success": True, "return_value": {"motor": {"value": 2}}}},
    ]
    out = c.read_device("sim_motor")
    assert out["ok"] is True  # succeeded on retry
    assert fake.script_uploads == 2  # initial inject + re-inject after "not found"
