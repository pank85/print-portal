"""`supportfins.py`: the node CLI wrapper, its serialisation and its error mapping.

Everything the wrapper does wrong is invisible from a sloppy test: a swallowed timeout, a
lost ``--json`` parse, an unsynchronised run. So ``subprocess.run`` is faked here — the
argv, the timeout, the semaphore and every exit path are asserted directly — and the real
bundled CLI is exercised exactly once, in the integration test at the bottom, which skips
when node is not installed.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import supportfins

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JSON_LINE = '{"stats": {"tines": 3, "finTriangles": 12}, "report": "3 walls, 3 tines"}'


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A CLI that exists on disk and a node that ``shutil.which`` can find."""
    path = tmp_path / "support_fins.mjs"
    path.write_text("// stand-in for the bundled bundle\n")
    monkeypatch.setattr(supportfins, "CLI", path)
    monkeypatch.setattr(supportfins.shutil, "which",
                        lambda name: "/usr/bin/node" if name == "node" else None)
    return path


@pytest.fixture
def src(tmp_path):
    path = tmp_path / "overhang.stl"
    path.write_bytes(b"solid overhang\nendsolid overhang\n")
    return path


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["node"], returncode, stdout, stderr)


@pytest.fixture
def record_run(monkeypatch):
    """Install a fake ``subprocess.run`` that records its call and answers as told."""
    calls: list[dict] = []

    def install(result=None, *, creates: bool = True, exc: BaseException | None = None,
                on_run=None):
        def fake_run(argv, **kwargs):
            calls.append({"argv": list(argv), **kwargs})
            if on_run is not None:
                on_run(argv, kwargs)
            if exc is not None:
                raise exc
            if creates:
                Path(argv[4]).write_bytes(b"PK\x03\x04finned")
            return completed(stdout=JSON_LINE) if result is None else result
        monkeypatch.setattr(supportfins.subprocess, "run", fake_run)
        return calls

    return install


# ------------------------------------------------------------------------- availability
def test_available_needs_both_the_cli_and_node(cli, monkeypatch):
    assert supportfins.available() is True

    monkeypatch.setattr(supportfins.shutil, "which", lambda _name: None)
    assert supportfins.available() is False

    monkeypatch.setattr(supportfins.shutil, "which",
                        lambda name: "/usr/bin/node" if name == "node" else None)
    monkeypatch.setattr(supportfins, "CLI", cli.with_name("absent.mjs"))
    assert supportfins.available() is False


def test_unavailable_reason_names_the_missing_piece(tmp_path, monkeypatch):
    """The UI shows this string, so it has to name what the operator has to fix."""
    monkeypatch.setattr(supportfins, "CLI", tmp_path / "absent.mjs")
    monkeypatch.setattr(supportfins.shutil, "which", lambda _name: "/usr/bin/node")
    assert supportfins.unavailable_reason() == "support-fins CLI is missing from the image"

    present = tmp_path / "support_fins.mjs"
    present.write_text("//\n")
    monkeypatch.setattr(supportfins, "CLI", present)
    monkeypatch.setattr(supportfins.shutil, "which", lambda _name: None)
    assert supportfins.unavailable_reason() == "node runtime is not available"

    monkeypatch.setattr(supportfins.shutil, "which", lambda _name: "/usr/bin/node")
    assert supportfins.unavailable_reason() == ""


# ------------------------------------------------------------------------------ the run
def test_fin_model_invokes_the_cli_with_json_and_returns_its_report(cli, src, tmp_path,
                                                                   record_run):
    calls = record_run()
    dest = tmp_path / "out" / "fins-overhang.3mf"

    result = supportfins.fin_model(src, dest)

    assert calls[0]["argv"] == ["node", str(cli), str(src), "-o", str(dest), "--json"]
    assert calls[0]["timeout"] == supportfins.DEFAULT_TIMEOUT
    assert calls[0]["text"] is True
    assert result == {"report": "3 walls, 3 tines",
                      "stats": {"tines": 3, "finTriangles": 12}}
    assert dest.read_bytes().startswith(b"PK\x03\x04")


def test_the_caller_timeout_reaches_the_subprocess(cli, src, tmp_path, record_run):
    calls = record_run()

    supportfins.fin_model(src, tmp_path / "out.3mf", timeout=1.5)

    assert calls[0]["timeout"] == 1.5


def test_the_destination_directory_is_created_before_the_run(cli, src, tmp_path, record_run):
    record_run()

    supportfins.fin_model(src, tmp_path / "deep" / "nested" / "out.3mf")

    assert (tmp_path / "deep" / "nested").is_dir()


def test_a_stale_destination_is_deleted_before_the_run(cli, src, tmp_path, record_run):
    """A leftover from an earlier attempt must never be mistaken for this run's output."""
    dest = tmp_path / "out.3mf"
    dest.write_bytes(b"stale")
    record_run(completed(returncode=1, stderr="boom"), creates=False)

    with pytest.raises(supportfins.SupportFinsError):
        supportfins.fin_model(src, dest)

    assert not dest.exists()


def test_an_unsupported_suffix_is_refused_before_node_runs(cli, tmp_path, record_run):
    calls = record_run()

    with pytest.raises(supportfins.SupportFinsError, match=r"does not support '\.zip'"):
        supportfins.fin_model(tmp_path / "model.zip", tmp_path / "out.3mf")

    assert calls == []


def test_the_suffix_check_ignores_case(cli, src, tmp_path, record_run):
    calls = record_run()
    upper = tmp_path / "OVERHANG.STL"
    upper.write_bytes(src.read_bytes())

    supportfins.fin_model(upper, tmp_path / "out.3mf")

    assert len(calls) == 1


# ------------------------------------------------------------------------- the timeouts
def test_a_timeout_names_the_limit_it_hit(cli, src, tmp_path, record_run):
    record_run(exc=subprocess.TimeoutExpired(cmd=["node"], timeout=7.5))

    with pytest.raises(supportfins.SupportFinsError,
                       match=r"support fins timed out after 7\.5s"):
        supportfins.fin_model(src, tmp_path / "out.3mf", timeout=7.5)


def test_the_default_timeout_is_reported_when_the_caller_sets_none(cli, src, tmp_path,
                                                                   record_run):
    record_run(exc=subprocess.TimeoutExpired(cmd=["node"], timeout=300))

    with pytest.raises(supportfins.SupportFinsError, match=r"timed out after 300s"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


@pytest.mark.parametrize("value", [0, -1, 0.0, float("nan"), float("inf"), "soon"])
def test_a_bad_timeout_is_rejected_before_node_runs(cli, src, tmp_path, record_run, value):
    calls = record_run()

    with pytest.raises(supportfins.SupportFinsError, match="must be a positive number"):
        supportfins.fin_model(src, tmp_path / "out.3mf", timeout=value)

    assert calls == []


# ---------------------------------------------------------------------------- exit paths
def test_node_missing_before_the_run_is_unavailable(cli, tmp_path, src, monkeypatch,
                                                    record_run):
    calls = record_run()
    monkeypatch.setattr(supportfins.shutil, "which", lambda _name: None)

    with pytest.raises(supportfins.SupportFinsUnavailable,
                       match="node runtime is not available"):
        supportfins.fin_model(src, tmp_path / "out.3mf")

    assert calls == []


def test_node_vanishing_between_the_check_and_the_run_is_unavailable(cli, src, tmp_path,
                                                                     record_run):
    record_run(exc=FileNotFoundError(2, "No such file or directory", "node"))

    with pytest.raises(supportfins.SupportFinsUnavailable,
                       match="node runtime is not available"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


def test_an_oserror_becomes_a_support_fins_error(cli, src, tmp_path, record_run):
    record_run(exc=OSError("cannot fork"))

    with pytest.raises(supportfins.SupportFinsError, match="could not start the CLI: cannot fork"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


def test_exit_2_is_reported_as_an_argument_rejection(cli, src, tmp_path, record_run):
    record_run(completed(returncode=2, stderr="unknown option --fins\n"), creates=False)

    with pytest.raises(supportfins.SupportFinsError, match="unknown option --fins"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


def test_exit_1_uses_the_clis_own_text(cli, src, tmp_path, record_run):
    record_run(completed(returncode=1, stderr="overhang.stl: no closed manifold\n"),
               creates=False)

    with pytest.raises(supportfins.SupportFinsError,
                       match="overhang.stl: no closed manifold"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


def test_the_stderr_text_wins_and_stdout_is_the_fallback(cli, src, tmp_path, record_run):
    record_run(completed(returncode=1, stdout="from stdout\n", stderr="  from stderr  \n"),
               creates=False)

    with pytest.raises(supportfins.SupportFinsError, match="from stderr"):
        supportfins.fin_model(src, tmp_path / "out1.3mf")

    record_run(completed(returncode=1, stdout="only stdout\n"), creates=False)
    with pytest.raises(supportfins.SupportFinsError, match="only stdout"):
        supportfins.fin_model(src, tmp_path / "out2.3mf")


def test_a_failure_without_any_text_still_says_something(cli, src, tmp_path, record_run):
    record_run(completed(returncode=1), creates=False)

    with pytest.raises(supportfins.SupportFinsError, match="support-fins failed"):
        supportfins.fin_model(src, tmp_path / "out.3mf")

    record_run(completed(returncode=2), creates=False)
    with pytest.raises(supportfins.SupportFinsError,
                       match="support-fins rejected its arguments"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


def test_a_silent_run_without_an_output_is_an_error(cli, src, tmp_path, record_run):
    record_run(completed(returncode=0), creates=False)

    with pytest.raises(supportfins.SupportFinsError, match="produced no output"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


def test_a_missing_output_prefers_the_clis_own_text(cli, src, tmp_path, record_run):
    record_run(completed(returncode=0, stdout="wrote no file\n"), creates=False)

    with pytest.raises(supportfins.SupportFinsError, match="wrote no file"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


def test_a_zero_byte_output_is_an_error(cli, src, tmp_path, record_run):
    record_run(completed(returncode=0, stderr="wrote nothing"), creates=False,
               on_run=lambda argv, _kwargs: Path(argv[4]).write_bytes(b""))

    with pytest.raises(supportfins.SupportFinsError, match="wrote nothing"):
        supportfins.fin_model(src, tmp_path / "out.3mf")


# ------------------------------------------------------------------------- --json parsing
def test_a_warning_before_the_json_line_does_not_fail_the_run(cli, src, tmp_path, record_run):
    record_run(completed(stdout="warning: stale cache\n" + JSON_LINE + "\n"))

    result = supportfins.fin_model(src, tmp_path / "out.3mf")

    assert result["stats"]["tines"] == 3
    assert result["report"] == "3 walls, 3 tines"


def test_the_last_parsable_object_wins(cli, src, tmp_path, record_run):
    record_run(completed(stdout='{"stats": {"tines": 1}}\n{"report": "second"}\n'))

    assert supportfins.fin_model(src, tmp_path / "out.3mf") == {"report": "second", "stats": {}}


def test_an_unrelated_json_line_is_skipped(cli, src, tmp_path, record_run):
    record_run(completed(stdout='{"input": "x.stl"}\n' + JSON_LINE))

    assert supportfins.fin_model(src, tmp_path / "out.3mf")["stats"]["finTriangles"] == 12


def test_an_unparsable_stream_degrades_to_the_plain_text(cli, src, tmp_path, record_run):
    """``--json`` is the contract, but a CLI printing prose must not become a crash."""
    record_run(completed(stdout="14 walls\n24 tines\n"))

    assert supportfins.fin_model(src, tmp_path / "out.3mf") == {"report": "24 tines", "stats": {}}


def test_broken_json_never_masks_a_successful_run(cli, src, tmp_path, record_run):
    record_run(completed(stdout='{"stats": not json}\n{"report": "ok", "stats": {}}\n'))

    assert supportfins.fin_model(src, tmp_path / "out.3mf") == {"report": "ok", "stats": {}}


# -------------------------------------------------------------------------- serialisation
def test_runs_are_serialised_by_the_module_semaphore(cli, src, tmp_path, monkeypatch):
    """A node run holds ~300 MB: two concurrent fin uploads must not spawn two processes."""
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def slow_run(argv, **_kwargs):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        Path(argv[4]).write_bytes(b"PK\x03\x04finned")
        with guard:
            state["active"] -= 1
        return completed(stdout=JSON_LINE)

    monkeypatch.setattr(supportfins.subprocess, "run", slow_run)
    errors: list[BaseException] = []

    def run(index: int) -> None:
        try:
            supportfins.fin_model(src, tmp_path / f"out-{index}.3mf")
        except BaseException as exc:            # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert state["peak"] == 1
    assert sorted(p.name for p in tmp_path.glob("out-*.3mf")) == [
        f"out-{index}.3mf" for index in range(4)]


def test_the_semaphore_is_released_after_a_failure(cli, src, tmp_path, monkeypatch):
    """A failed run must not wedge the lock for every later upload."""
    monkeypatch.setattr(supportfins.subprocess, "run",
                        lambda _argv, **_kwargs: completed(returncode=1, stderr="no"))

    for index in range(3):
        with pytest.raises(supportfins.SupportFinsError):
            supportfins.fin_model(src, tmp_path / f"bad-{index}.3mf")

    monkeypatch.setattr(supportfins.subprocess, "run",
                        lambda argv, **_kwargs: (
                            Path(argv[4]).write_bytes(b"PK\x03\x04"),
                            completed(stdout=JSON_LINE))[1])
    assert supportfins.fin_model(src, tmp_path / "good.3mf")["report"] == "3 walls, 3 tines"


# ------------------------------------------------------------------------- real CLI (opt-in)
@pytest.mark.skipif(not supportfins.available(), reason="node/the bundled CLI is unavailable")
def test_the_bundled_cli_fins_the_overhang_fixture(tmp_path):
    """The only test that runs the real bundle: argv, --json and the 3MF output together."""
    out = tmp_path / "overhang-fins.3mf"

    result = supportfins.fin_model(FIXTURES / "overhang.stl", out)

    assert out.read_bytes()[:4] == b"PK\x03\x04"
    assert result["stats"]["finTriangles"] > 0
    assert result["stats"]["tines"] > 0
    assert result["report"]
