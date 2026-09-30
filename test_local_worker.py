"""Daemon lifecycle acceptance with no model, network, or process launch."""
from types import SimpleNamespace
import pytest
from demo import local_worker as worker


def setup_worker(monkeypatch, status=None):
    process = SimpleNamespace(pid=12345, poll=lambda: status)
    stops = []
    monkeypatch.setattr(worker, "free_port", lambda: 12346)
    monkeypatch.setattr(worker, "executable", lambda: "installed-ollama")
    monkeypatch.setattr(worker, "launch", lambda *args: (process, "owned-job"))
    monkeypatch.setattr(worker, "stop_owned", lambda *args: stops.append(args))
    monkeypatch.setattr(worker, "version_metadata", lambda *args: ("0.test", "a" * 64))
    return process, stops


def test_worker_limits_are_process_local_and_replace_inherited_controls(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    monkeypatch.setenv("OLLAMA_LLM_LIBRARY", "cuda")
    monkeypatch.setenv("OLLAMA_MODELS", "existing-cache")
    env = worker.worker_environment(12346)
    assert env["OLLAMA_HOST"] == "127.0.0.1:12346"
    assert env["OLLAMA_MODELS"] == "existing-cache"
    assert "OLLAMA_LLM_LIBRARY" not in env
    assert env["OLLAMA_NO_CLOUD"] == "1" and env["CUDA_VISIBLE_DEVICES"] == "-1"
    assert env["OLLAMA_NO_PRUNE"] == "1"
    assert env["ROCR_VISIBLE_DEVICES"] == "-1" and env["OLLAMA_VULKAN"] == "0"
    assert worker.os.environ["OLLAMA_HOST"] == "0.0.0.0:11434"


@pytest.mark.parametrize("fail_inside", [False, True])
def test_owned_cleanup_after_success_or_inference_failure(monkeypatch, fail_inside):
    process, stops = setup_worker(monkeypatch)
    try:
        with worker.isolated_cpu_worker() as instance:
            assert instance.pid == process.pid and instance.root_url == "http://127.0.0.1:12346"
            if fail_inside:
                raise ValueError("inference failed")
    except ValueError:
        assert fail_inside
    assert stops == [(process, "owned-job")]


def test_startup_exit_still_closes_owned_tree(monkeypatch):
    process, stops = setup_worker(monkeypatch, status=1)
    with pytest.raises(RuntimeError, match="startup"):
        with worker.isolated_cpu_worker():
            pytest.fail("failed worker yielded")
    assert stops == [(process, "owned-job")]


def test_startup_timeout_retains_cleanup(monkeypatch):
    process, stops = setup_worker(monkeypatch)
    ticks = iter([0, .5, .6, 1.1])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(worker, "version_metadata", lambda *args: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(worker.time, "sleep", lambda *args: None)
    with pytest.raises(TimeoutError):
        with worker.isolated_cpu_worker(startup_seconds=1):
            pytest.fail("timed out worker yielded")
    assert stops == [(process, "owned-job")]


def test_cleanup_failure_is_not_a_success(monkeypatch):
    setup_worker(monkeypatch)
    monkeypatch.setattr(worker, "stop_owned", lambda *args: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(TimeoutError):
        with worker.isolated_cpu_worker():
            pass


def test_late_successful_metadata_cannot_pass_startup_budget(monkeypatch):
    process, stops = setup_worker(monkeypatch)
    ticks = iter([0, .5, 1.1, 1.2, 1.3])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(worker.time, "sleep", lambda *args: None)
    with pytest.raises(TimeoutError):
        with worker.isolated_cpu_worker(startup_seconds=1):
            pytest.fail("late worker yielded")
    assert stops == [(process, "owned-job")]


def test_daemon_exit_during_inference_is_rejected(monkeypatch):
    process, stops = setup_worker(monkeypatch)
    with pytest.raises(RuntimeError, match="during inference"):
        with worker.isolated_cpu_worker():
            process.poll = lambda: 1
    assert stops == [(process, "owned-job")]


@pytest.mark.parametrize("job_failure", [False, True])
def test_windows_worker_assigned_before_resume_and_failed_assignment_cleaned(monkeypatch, job_failure):
    events = []
    process = SimpleNamespace(resume=lambda: events.append("resume"),
                              kill=lambda: events.append("kill"),
                              wait=lambda **kwargs: events.append("wait"),
                              close=lambda: events.append("close"))
    monkeypatch.setattr(worker, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(worker, "WindowsProcess", lambda *args: events.append("suspended-create") or process)
    job = SimpleNamespace(close=lambda: events.append("job-close"))
    def assign(*args):
        events.append("assign")
        if job_failure:
            raise OSError("job assignment failed")
        return job
    monkeypatch.setattr(worker, "WindowsJob", assign)
    if job_failure:
        with pytest.raises(OSError):
            worker.launch("installed-ollama", {})
        assert events == ["suspended-create", "assign", "kill", "wait", "close"]
    else:
        assert worker.launch("installed-ollama", {}) == (process, job)
        assert events == ["suspended-create", "assign", "resume"]


def test_metadata_redirect_refused():
    with pytest.raises(ValueError):
        worker.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid")
