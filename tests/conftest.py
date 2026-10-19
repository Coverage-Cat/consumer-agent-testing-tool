"""Default collection is the supported local product; opt in to legacy experiments."""
import functools
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from consumer_agent_testing_tool.regression_scope import development_case, development_file

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_TEMP = pytest.StashKey[Path]()
CREATED_RUNS = pytest.StashKey[list]()


def pytest_addoption(parser):
    parser.addoption("--development-extensions", action="store_true",
                     help="Also collect unqualified worker, local-model, isolation and calibration experiments")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    record_repository_runs(config)
    # Product paths must stay inside the workspace. A plain `pytest` gets the same
    # workspace-local temporary layout as scripts/run_regression.py; an explicit
    # --basetemp (the regression runner) keeps its own environment.
    if config.option.basetemp:
        return
    prune_sessions(ROOT / ".cache" / "pytest")
    work = ROOT / ".cache" / "pytest" / str(os.getpid())
    shutil.rmtree(work, ignore_errors=True)
    for name in ("home", "tmp", "config", "cache"):
        (work / name).mkdir(parents=True)
    os.environ.update(HOME=str(work / "home"), TMPDIR=str(work / "tmp"),
                      XDG_CONFIG_HOME=str(work / "config"), XDG_CACHE_HOME=str(work / "cache"))
    tempfile.tempdir = str(work / "tmp")
    config.option.basetemp = str(work / "tests")
    config.stash[WORKSPACE_TEMP] = work


def record_repository_runs(config):
    """Surface and browser runs default to <workspace>/runs. Remember the ones this session creates there
    (only this process's, so concurrent sessions are untouched) and remove them when the session ends."""
    from consumer_agent_testing_tool.browser import BrowserBroker
    from consumer_agent_testing_tool.surface import SurfaceRun
    created = config.stash[CREATED_RUNS] = []
    for kind in (SurfaceRun, BrowserBroker):
        def recording(self, *args, construct=kind.__init__, **kwargs):
            construct(self, *args, **kwargs)
            created.append(Path(self.path))
        kind.__init__ = functools.wraps(kind.__init__)(recording)


def prune_sessions(base, keep=3):
    """Like pytest's own retention: keep the newest few failed sessions, never a live one."""
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    if not base.is_dir():
        return
    stale = sorted((path for path in base.iterdir() if path.is_dir() and path.name.isdigit()
                    and not alive(int(path.name))), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in stale[keep:]:
        shutil.rmtree(path, ignore_errors=True)


def pytest_sessionfinish(session, exitstatus):
    runs = ROOT / "runs"
    for path in session.config.stash.get(CREATED_RUNS, []):
        if path.resolve().parent == runs and path.name.startswith(("surface-", "browser-")):
            shutil.rmtree(path, ignore_errors=True)
    # Keep failed sessions for inspection; passing sessions leave nothing behind.
    work = session.config.stash.get(WORKSPACE_TEMP, None)
    if work and exitstatus == 0:
        session.config.add_cleanup(lambda: shutil.rmtree(work, ignore_errors=True))


def pytest_ignore_collect(collection_path, config):
    if not config.getoption("--development-extensions") and development_file(collection_path.name):
        return True
    return None


def pytest_collection_modifyitems(config, items):
    if config.getoption("--development-extensions"):
        return
    excluded = [item for item in items if development_case(item)]
    if excluded:
        items[:] = [item for item in items if item not in excluded]
        config.hook.pytest_deselected(items=excluded)
