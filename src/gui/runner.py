"""Execution back end for the GUI (Streamlit-free).

The Streamlit app (``app.py``) only builds the flow and renders progress; every
piece of logic that decides *what happens to the hardware* lives here, so that

* the GUI runs a flow through exactly the same path as the CLI
  (``python -m src.flow.run_flow``): Pydantic validation -> loop expansion ->
  :class:`~src.flow.experiment_session.ExperimentSession` (initialise, run,
  emergency stop on cancel, home on error, always clean up) ->
  :class:`~src.flow.experiment_logger.ExperimentLogger`; and
* the behaviour can be tested without a browser (see ``tests/test_gui_runner.py``).

Why one background thread instead of a step-by-step rerun
---------------------------------------------------------
Streamlit re-executes the whole script on every interaction, so the previous
design ran one step per rerun and kept live device handles in ``st.session_state``.
That makes the safety frame impossible to use: ``ExperimentSession.run()`` owns the
run inside a single ``try/except/finally`` (emergency stop, go-home, cleanup,
log finalisation) and cannot be sliced across script reruns.

So the whole flow runs in **one background thread with one asyncio event loop**.
Progress is published as :class:`ProgressEvent` objects on a thread-safe queue,
which the Streamlit script drains on each rerun. Stopping cancels the asyncio
task from the outside, which lands in ``ExperimentSession.run()``'s
``except CancelledError`` branch - the same path Ctrl+C takes on the CLI - so the
robots are emergency-stopped and everything is cleaned up and logged.
"""
import asyncio
import json
import logging
import os
import queue
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from src import config as lab_config
from src.devices.safety.validators import default_workspace_validator
from src.flow.accuracy_logger import DispenseAccuracyLogger
from src.flow.executor import SHARED_DEVICE_ACTIONS, execute_step, expand_loops
from src.flow.experiment_logger import ExperimentLogger
from src.flow.experiment_session import ExperimentSession
from src.flow.run_flow import LOGS_DIR, plan_resources
from src.flow.schema import ExperimentWorkflow

logger = logging.getLogger("gui.runner")

#: Robots the sidebar offers. Robot 3 is the slider/conveyer arm.
ROBOT_IDS = (1, 2, 3)


# ======================================================================
# Validation
# ======================================================================
def validate_workflow(data: dict):
    """Validate a flow dict and expand its loops.

    Returns:
        ``(workflow, expanded_steps)`` - a validated
        :class:`ExperimentWorkflow` and the flat list of step dicts.

    Raises:
        pydantic.ValidationError: the JSON does not match the schema.
        ValueError: the loop structure is broken (unmatched/nested loops)
            or the flow has no steps.
    """
    workflow = ExperimentWorkflow(**data)
    steps = [s.model_dump() for s in workflow.steps]
    expanded = expand_loops(steps)
    if not expanded:
        raise ValueError("The flow has no steps")
    return workflow, expanded


def _variant_matches(variant: str, action) -> bool:
    """``ActionCaptureAndSave`` <-> ``capture_and_save``."""
    if not variant or not isinstance(action, str):
        return False
    return variant.replace("Action", "").lower() == action.replace("_", "").lower()


def format_validation_error(exc: Exception, data: dict = None) -> str:
    """Turn a ValidationError into a short message for the UI.

    ``steps`` is a discriminated-by-nothing Union, so Pydantic reports a failure
    for *every* action type. Only the errors of the action the step actually
    declares are useful, so the rest are dropped; a step whose ``action`` matches
    nothing is reported as an unknown action.
    """
    get_errors = getattr(exc, "errors", None)
    if not callable(get_errors):
        return str(exc)

    steps = data.get("steps") if isinstance(data, dict) else None
    grouped = {}
    for err in get_errors():
        loc = list(err.get("loc", ()))
        index = next((p for p in loc if isinstance(p, int)), None)
        rest = loc[loc.index(index) + 1:] if index is not None else loc
        variant = (rest[0] if rest and isinstance(rest[0], str)
                   and rest[0].startswith("Action") else None)
        field = ".".join(str(p) for p in (rest[1:] if variant else rest))
        grouped.setdefault(index, []).append((variant, field, err.get("msg", "")))

    lines = []
    for index, items in grouped.items():
        label = f"step {index + 1}" if index is not None else "flow"
        action = None
        if isinstance(steps, list) and index is not None and index < len(steps):
            step = steps[index]
            action = step.get("action") if isinstance(step, dict) else None
        matching = [it for it in items if _variant_matches(it[0], action)]
        if not matching:
            if action is None:
                lines += [f"- {label}: {field or '(root)'}: {msg}"
                          for _, field, msg in items[:3]]
            else:
                lines.append(f"- {label}: unknown or mistyped action '{action}'")
            continue
        for _, field, msg in matching:
            if field == "action":
                continue
            lines.append(f"- {label} ({action}): {field or '(root)'}: {msg}")
    return "\n".join(lines[:20]) or str(exc)


# ======================================================================
# Device settings (config.yaml -> sidebar defaults)
# ======================================================================
def default_device_settings() -> dict:
    """Sidebar defaults taken from ``config.yaml`` / ``config.example.yaml``.

    Returns a dict with ``robots`` (per robot id) and ``shared``; the GUI keeps
    it in ``st.session_state`` and lets the user edit the ports before a run.
    """
    ports = lab_config.get_robot_ports()
    shared = lab_config.get_shared_devices()
    robots = {}
    for rid in ROBOT_IDS:
        rp = ports.get(rid, {})
        picus2 = rp.get("picus2_address", "") or ""
        robots[rid] = {
            "enabled": True,
            "use_dobot": True,
            "use_picus2": bool(picus2),
            "dobot_port": rp.get("dobot_port", "") or "",
            "picus2_port": picus2,
        }
    return {
        "robots": robots,
        "shared": {
            "enabled": True,
            "use_scale": True,
            "scale_port": shared.get("scale_port", "") or "",
            "camera_index": int(shared.get("camera_index", 0) or 0),
        },
    }


def settings_to_session_args(settings: dict):
    """Convert the sidebar settings into ExperimentSession arguments.

    Returns:
        ``(robot_ports, shared_config)`` in the shape ExperimentSession expects.
    """
    robot_ports = {}
    for rid, cfg in (settings.get("robots") or {}).items():
        robot_ports[int(rid)] = {
            "dobot_port": cfg.get("dobot_port", ""),
            "picus2_address": cfg.get("picus2_port", "") if cfg.get("use_picus2", True) else "",
        }
    shared = settings.get("shared") or {}
    shared_config = {
        "scale_port": shared.get("scale_port", ""),
        "camera_index": int(shared.get("camera_index", 0) or 0),
    }
    return robot_ports, shared_config


# ======================================================================
# Progress events
# ======================================================================
@dataclass
class ProgressEvent:
    """One thing worth showing in the UI.

    kind:
        ``"start"``   run accepted; ``log_dir`` is the run folder
        ``"log"``     a log line (device logs included)
        ``"step"``    exactly one per executed step, after it finished
        ``"finish"``  the run ended; ``status`` is completed/failed/aborted
    """
    kind: str
    message: str = ""
    index: Optional[int] = None
    total: Optional[int] = None
    action: Optional[str] = None
    robot_id: Optional[int] = None
    iteration: Optional[int] = None
    status: Optional[str] = None
    duration: Optional[float] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    log_dir: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class _ProgressLogHandler(logging.Handler):
    """Forwards log records to the runner as ``log`` events."""

    def __init__(self, emit_fn):
        super().__init__(level=logging.INFO)
        self._emit_fn = emit_fn
        self._busy = threading.local()

    def emit(self, record):
        if getattr(self._busy, "flag", False):
            return
        self._busy.flag = True
        try:
            self._emit_fn(ProgressEvent(kind="log", message=self.format(record)))
        except Exception:  # noqa: BLE001 - logging must never break a run
            pass
        finally:
            self._busy.flag = False


def _fmt_params(step: dict) -> str:
    return " ".join(
        f"{k}={v}" for k, v in step.items() if k not in ("action", "_iteration")
    )


# ======================================================================
# Runner
# ======================================================================
class FlowRunner:
    """Runs one flow in a background thread and reports progress.

    Args:
        workflow_data: the flow as a dict (LLM output, preset or edited canvas).
        mock: True runs against MockLabRobot / MockSharedDevices.
        robot_ports: robot id -> ``{"dobot_port", "picus2_address"}``.
            Defaults to ``config.yaml``.
        shared_config: ``{"scale_port", "camera_index"}``. Defaults to ``config.yaml``.
        on_event: called for every :class:`ProgressEvent` (from the run thread).
        logs_dir: root of the run folders (default ``<repo>/logs``).
        density: liquid density g/mL used for the dispense-accuracy CSV.
        source_path: path of the JSON the flow came from, copied into the run
            folder. When omitted the dict is written to a temporary file.
        pre_step: optional callable invoked before every step; raising aborts
            the run (the GUI uses it for the dashboard's ``/api/is_safe`` gate).
        robot_factory / shared_factory: ExperimentSession factory overrides (tests).
    """

    def __init__(
        self,
        workflow_data: dict,
        *,
        mock: bool = False,
        robot_ports: Optional[dict] = None,
        shared_config: Optional[dict] = None,
        on_event: Optional[Callable[[ProgressEvent], None]] = None,
        logs_dir: str = LOGS_DIR,
        density: float = 1.0,
        source_path: Optional[str] = None,
        pre_step: Optional[Callable[[dict], None]] = None,
        robot_factory: Optional[Callable] = None,
        shared_factory: Optional[Callable] = None,
    ):
        # Validation happens here: an invalid flow raises before anything is
        # built, so no ExperimentSession and no run folder are ever created.
        self.workflow, self.steps = validate_workflow(workflow_data)

        self.workflow_data = workflow_data
        self.mock = mock
        self.robot_ports = robot_ports
        self.shared_config = shared_config
        self.on_event = on_event
        self.logs_dir = logs_dir
        self.density = density
        self.source_path = source_path
        self.pre_step = pre_step
        self.robot_factory = robot_factory
        self.shared_factory = shared_factory

        self.events: "queue.Queue[ProgressEvent]" = queue.Queue()
        self.log_dir: Optional[str] = None
        self.status: str = "pending"
        self.error: Optional[str] = None
        self.session: Optional[ExperimentSession] = None

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------
    def _emit(self, event: ProgressEvent):
        self.events.put(event)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001 - a broken UI must not stop the run
                pass

    def drain_events(self) -> List[ProgressEvent]:
        """Pop everything published since the last call (for the UI to render)."""
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> "FlowRunner":
        """Start the run in a background thread and return immediately."""
        if self._thread is not None:
            raise RuntimeError("This runner has already been started")
        self._thread = threading.Thread(
            target=self._thread_main, name="gui-flow-runner", daemon=True
        )
        self._thread.start()
        return self

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request_stop(self):
        """Ask the run to stop; robots are emergency-stopped, then cleaned up."""
        self._stop.set()
        with self._lock:
            loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for the run to finish. Returns True if it did."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def run_blocking(self) -> str:
        """Run the flow in the calling thread (used by tests). Returns the status."""
        self._thread_main()
        return self.status

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------
    def _thread_main(self):
        try:
            asyncio.run(self._amain())
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        except Exception as e:  # noqa: BLE001 - reported through the event stream
            logger.error("Flow run failed: %s", e)

    def _source_json_path(self) -> str:
        """Path to copy into the run folder (write a temp file if unsaved)."""
        if self.source_path and os.path.exists(self.source_path):
            return self.source_path
        tmp_dir = tempfile.mkdtemp(prefix="gui_flow_")
        path = os.path.join(tmp_dir, "workflow.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.workflow_data, f, ensure_ascii=False, indent=2)
        return path

    async def _amain(self):
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()

        steps = self.steps
        total = len(steps)
        (robot_ids, picus2_robots, needs_scale, needs_camera,
         needs_microscope, needs_microscope_serial) = plan_resources(steps)

        exp_logger = ExperimentLogger(
            self.workflow.name,
            self.workflow.description,
            self._source_json_path(),
            base_dir=self.logs_dir,
        )
        exp_logger.record_resources(robot_ids, picus2_robots)
        self.log_dir = exp_logger.dir

        accuracy_logger = DispenseAccuracyLogger(
            os.path.join(exp_logger.dir, "dispense_accuracy.csv"), density=self.density
        )

        # Device logs (executor, robots, shared devices) reach the UI verbatim.
        # The CLI gets INFO through logging.basicConfig(); the GUI has no such
        # entry point, so make sure INFO records are not filtered out at the
        # root - otherwise both the UI log and run.log would stay empty.
        handler = _ProgressLogHandler(self._emit)
        handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        if previous_level == logging.NOTSET or previous_level > logging.INFO:
            root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

        self.status = "running"
        self._emit(ProgressEvent(
            kind="start", total=total, log_dir=exp_logger.dir,
            message=f"{self.workflow.name} / {'MOCK' if self.mock else 'REAL'} / "
                    f"{total} steps -> {exp_logger.dir}",
        ))

        session = ExperimentSession(
            mock=self.mock,
            robot_factory=self.robot_factory,
            shared_factory=self.shared_factory,
            robot_ports=self.robot_ports,
            shared_config=self.shared_config,
            workspace_validator=default_workspace_validator(),
        )
        self.session = session

        async def body():
            for rid in robot_ids:
                await session.add_robot(rid, use_picus2=(rid in picus2_robots))
            if needs_scale or needs_camera or needs_microscope or needs_microscope_serial:
                await session.add_shared(use_scale=needs_scale, use_camera=needs_camera,
                                         use_microscope=needs_microscope,
                                         use_microscope_serial=needs_microscope_serial)

            for i, step in enumerate(steps, 1):
                # Cancellation is checked between steps as well, so a Stop
                # pressed before the first await still aborts the run.
                if self._stop.is_set():
                    raise asyncio.CancelledError()
                action = step.get("action")
                rid = None if action in SHARED_DEVICE_ACTIONS else step.get("robot_id", 1)
                if self.pre_step is not None:
                    self.pre_step(step)

                # Keep captured images inside the run folder (as run_flow does).
                if action == "capture_and_save" and not step.get("file_path"):
                    step = {**step, "file_path": exp_logger.next_image_path(i)}
                elif action == "capture_microscope" and not step.get("file_path"):
                    step = {**step, "file_path": exp_logger.next_image_path(i, tag="microscope")}

                logger.info(
                    "[%d/%d] %s: %s %s", i, total,
                    "shared" if rid is None else f"Robot {rid}", action, _fmt_params(step),
                )
                iteration = step.get("_iteration")
                t0 = time.monotonic()
                try:
                    result = await execute_step(step, session.robots, session.shared, logger)
                except Exception as step_err:
                    duration = time.monotonic() - t0
                    exp_logger.record_step(i, total, action, rid, iteration,
                                           "error", duration, error=str(step_err))
                    self._emit(ProgressEvent(
                        kind="step", index=i, total=total, action=action, robot_id=rid,
                        iteration=iteration, status="error", duration=duration,
                        error=str(step_err),
                    ))
                    raise
                duration = time.monotonic() - t0
                exp_logger.record_step(i, total, action, rid, iteration,
                                       "ok", duration, result=result)
                self._emit(ProgressEvent(
                    kind="step", index=i, total=total, action=action, robot_id=rid,
                    iteration=iteration, status="ok", duration=duration,
                    result=result if isinstance(result, dict) else None,
                ))

                if action == "dispense":
                    accuracy_logger.record_dispense(
                        volume=step.get("volume"), speed=step.get("speed"),
                        iteration=iteration, step_index=i,
                    )
                elif action == "measure_weight":
                    weight = result.get("weight") if isinstance(result, dict) else result
                    err = accuracy_logger.record_weight(weight)
                    if err is not None:
                        logger.info("  dispense error: %+.4f g", err)

        try:
            await session.run(body)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass  # session.run already emergency-stopped and cleaned up
        except Exception as e:  # noqa: BLE001 - surfaced through the finish event
            logger.error("Flow run failed: %s", e)
        finally:
            self.status, self.error = session.status, session.error
            try:
                accuracy_logger.finalize(logger)
            except Exception:  # noqa: BLE001
                pass
            exp_logger.finalize(session.status, session.error)
            root_logger.removeHandler(handler)
            root_logger.setLevel(previous_level)
            handler.close()
            self._emit(ProgressEvent(
                kind="finish", status=session.status, error=session.error,
                total=total, log_dir=exp_logger.dir,
                message=f"{session.status}: {exp_logger.dir}",
            ))
