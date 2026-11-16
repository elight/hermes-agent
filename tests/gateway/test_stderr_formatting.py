"""Regression tests for operator-visible gateway stderr formatting."""

from __future__ import annotations

import logging
import os
import re
import sys

import hermes_logging

from gateway.run import _gateway_stderr_formatter


def test_gateway_stderr_formatter_includes_timestamp() -> None:
    record = logging.LogRecord(
        name="gateway.run",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="delivery failed",
        args=(),
        exc_info=None,
    )

    rendered = _gateway_stderr_formatter().format(record)

    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rendered), rendered
    assert "delivery failed" in rendered


def test_gateway_stderr_reaches_the_journal_as_one_record(monkeypatch) -> None:
    """One gateway log record must cost exactly ONE systemd journal record.

    systemd stores every newline a unit writes to stderr as its OWN record, all at
    PRIORITY=6, so an exception whose traceback spans ~40 lines used to arrive as
    ~40 ``info`` records: the levels lied and one failure spent 40 lines of the log
    shipper's daily budget (measured on the hermes-VM: a whole day's budget by
    02:39). This runs the pair the gateway wires — its formatter inside the
    journal-aware handler — and asserts what journald would store: one line,
    carrying the record's real priority.
    """
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    info = os.fstat(write_fd)
    monkeypatch.setenv("JOURNAL_STREAM", f"{info.st_dev}:{info.st_ino}")

    handler = hermes_logging.JournalStreamHandler(os.fdopen(write_fd, "w", encoding="utf-8"))
    handler.setFormatter(_gateway_stderr_formatter())
    try:
        try:
            raise RuntimeError("the llm tenant could not be started")
        except RuntimeError:
            record = logging.LogRecord(
                name="agent.chat_completion_helpers",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="Streaming failed before delivery: %s",
                args=(sys.exception(),),
                exc_info=sys.exc_info(),
            )
        handler.emit(record)
        written = os.read(read_fd, 65536).decode()
    finally:
        handler.stream.close()
        os.close(read_fd)

    # Exactly one physical line (a single terminator) -> one journal record.
    assert written.count("\n") == 1, written
    # `<3>` is journald's syslog level for the record; the shipper maps PRIORITY to
    # Loki's `level` label, so without it the single record would still read `info`.
    assert written.startswith("<3>"), written
    assert re.match(r"<3>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ ERROR ", written), written
    assert "Streaming failed before delivery: the llm tenant could not be started" in written
    # The exception must still be present — serialized, not dropped.
    assert "Traceback (most recent call last):" in written
    assert "RuntimeError: the llm tenant could not be started" in written
