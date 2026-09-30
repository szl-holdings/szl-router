"""Owned loopback-only Ollama worker; cached models and process-local CPU limits."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
import urllib.request


@dataclass(frozen=True)
class Worker:
    root_url: str
    pid: int
    version: str
    metadata_sha256: str


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("worker metadata redirect refused")


def executable():
    found = shutil.which("ollama")
    if not found and os.name == "nt":
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"
        if candidate.is_file():
            found = str(candidate)
    if not found:
        raise FileNotFoundError("installed Ollama executable required")
    return found


def worker_environment(port):
    # Keep an explicitly configured cache; discard inherited daemon controls.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("OLLAMA_") or key.upper() == "OLLAMA_MODELS"}
    env.update(OLLAMA_HOST=f"127.0.0.1:{port}", OLLAMA_NO_CLOUD="1", OLLAMA_NO_PRUNE="1",
               OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS="1",
               OLLAMA_CONTEXT_LENGTH="2048", OLLAMA_LOAD_TIMEOUT="60s",
               OLLAMA_KEEP_ALIVE="5m", OLLAMA_VULKAN="0",
               CUDA_VISIBLE_DEVICES="-1", ROCR_VISIBLE_DEVICES="-1",
               HIP_VISIBLE_DEVICES="-1", GGML_VK_VISIBLE_DEVICES="-1")
    return env


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class WindowsProcess:
    """Create suspended, so job ownership precedes the child's first instruction."""
    def __init__(self, binary, env):
        import ctypes
        from ctypes import wintypes
        class Startup(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR)] + [
                        (name, wintypes.DWORD) for name in
                        ("x", "y", "x_size", "y_size", "x_chars", "y_chars", "fill", "flags")] + [
                        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
                        ("reserved_bytes", ctypes.c_void_p), ("stdin", wintypes.HANDLE),
                        ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]
        class Information(ctypes.Structure):
            _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                        ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(Information)]
        kernel.CreateProcessW.restype = wintypes.BOOL
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        startup, info = Startup(), Information()
        startup.cb = ctypes.sizeof(startup)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([binary, "serve"]))
        environment = ctypes.create_unicode_buffer("\0".join(
            key + "=" + value for key, value in sorted(env.items(), key=lambda pair: pair[0].upper())) + "\0\0")
        # NO_WINDOW | UNICODE_ENVIRONMENT | SUSPENDED, with no inherited handles.
        if not kernel.CreateProcessW(binary, command, None, None, False, 0x08000404,
                                     environment, None, ctypes.byref(startup), ctypes.byref(info)):
            raise OSError("owned worker creation failed")
        self.kernel, self.ctypes, self.wintypes = kernel, ctypes, wintypes
        self._handle, self.thread, self.pid = info.process, info.thread, info.pid

    def resume(self):
        try:
            if self.kernel.ResumeThread(self.thread) == 0xFFFFFFFF:
                raise OSError("owned worker resume failed")
        finally:
            self.kernel.CloseHandle(self.thread)
            self.thread = None

    def poll(self):
        state = self.kernel.WaitForSingleObject(self._handle, 0)
        if state == 0x102:  # WAIT_TIMEOUT
            return None
        if state != 0:
            raise OSError("owned worker wait failed")
        code = self.wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self._handle, self.ctypes.byref(code)):
            raise OSError("owned worker exit readback failed")
        return code.value

    def wait(self, timeout):
        state = self.kernel.WaitForSingleObject(self._handle, math.ceil(timeout * 1000))
        if state == 0x102:
            raise subprocess.TimeoutExpired("owned ollama worker", timeout)
        if state != 0:
            raise OSError("owned worker wait failed")
        return self.poll()

    def kill(self):
        if not self.kernel.TerminateProcess(self._handle, 1):
            raise OSError("owned worker termination failed")

    def close(self):
        if self.thread:
            self.kernel.CloseHandle(self.thread)
            self.thread = None
        if self._handle:
            self.kernel.CloseHandle(self._handle)
            self._handle = None


class WindowsJob:
    """Unnamed, non-inheritable job containing only the newly owned process tree."""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ("user_time", "kernel_time", "period_user", "period_kernel")] + [
                        (name, wintypes.DWORD) for name in
                        ("faults", "total", "active", "terminated")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel, self.ctypes, self.Accounting = kernel, ctypes, Accounting
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("owned worker job creation failed")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle)))):
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise OSError("owned worker job assignment failed")

    def close(self):
        if not self.handle:
            return
        try:
            if not self.kernel.TerminateJobObject(self.handle, 0):
                raise OSError("owned worker job termination failed")
            until = time.monotonic() + 5
            while True:
                accounting = self.Accounting()
                if not self.kernel.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(accounting),
                                                             self.ctypes.sizeof(accounting), None):
                    raise OSError("owned worker job readback failed")
                if accounting.active == 0:
                    break
                if time.monotonic() >= until:
                    raise TimeoutError("owned worker process tree did not exit")
                time.sleep(.05)
        finally:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def launch(binary, env):
    options = {"env": env, "stdin": subprocess.DEVNULL,
               "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        process = WindowsProcess(binary, env)
        job = None
        try:
            job = WindowsJob(process)
            process.resume()
            return process, job
        except BaseException:
            try:
                if job:
                    job.close()
                else:
                    process.kill()
                process.wait(timeout=5)
            finally:
                process.close()
            raise
    options["start_new_session"] = True
    return subprocess.Popen([binary, "serve"], **options), None


def stop_owned(process, job):
    if job is not None:
        try:
            job.close()
        finally:
            try:
                process.wait(timeout=5)
            finally:
                process.close()
        return
    # This group was created by start_new_session, never the shared daemon's.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def version_metadata(root, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(root + "/api/version", timeout=timeout) as response:
        raw = response.read(10_001)
    if len(raw) > 10_000:
        raise ValueError("worker metadata exceeded limit")
    data = json.loads(raw)
    if not isinstance(data.get("version"), str) or not data["version"]:
        raise ValueError("worker version unavailable")
    return data["version"], hashlib.sha256(raw).hexdigest()


@contextmanager
def isolated_cpu_worker(startup_seconds=30):
    if not 0 < startup_seconds <= 30:
        raise ValueError("worker startup budget must be 0..30 seconds")
    port = free_port()
    root = f"http://127.0.0.1:{port}"
    process, job = launch(executable(), worker_environment(port))
    try:
        until = time.monotonic() + startup_seconds
        while True:
            if process.poll() is not None:
                raise RuntimeError("owned worker exited during startup")
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("owned worker startup budget exceeded")
            try:
                version, digest = version_metadata(root, min(1, remaining))
                if process.poll() is not None:
                    raise RuntimeError("owned worker exited after metadata")
                if time.monotonic() > until:
                    raise TimeoutError("owned worker metadata exceeded startup budget")
                break
            except (OSError, ValueError):
                time.sleep(min(.1, max(0, until - time.monotonic())))
        yield Worker(root, process.pid, version, digest)
        if process.poll() is not None:
            raise RuntimeError("owned worker exited during inference")
    finally:
        stop_owned(process, job)
