# ruff: noqa: I001  # the gateway imports are resolved by PYTHONPATH at run time,
# so isort cannot classify them against the stdlib block.
"""Drive the Signal adapter's streaming-TTS contract and time the first bubble.

Runs inside a Hermes checkout (the path is HERMES_SRC, default the fork checkout
on this box), where `gateway` is importable. It uses the real SignalAdapter with
the attachment send replaced by a recorder, and a streamer paced like omen
(~54 characters/second, first byte ~0.05 s), so the number printed is the time
from the start of the reply to the moment the listener's phone gets audio.

    python3 measure_signal_stream.py
"""
# Run with the Hermes checkout on PYTHONPATH, e.g.
#   PYTHONPATH=/home/evan/claude-tmp/hermes-pr-124162 python3 measure_signal_stream.py
# (on VM 103: PYTHONPATH=$HOME/.hermes/hermes-agent). The gateway imports below
# are marked import-not-found because the checker has no Hermes checkout on it.
import asyncio
import os
import sys
import time
from pathlib import Path

from gateway.platforms.base import AudioFormat, SendResult  # type: ignore[import-not-found]
from gateway.platforms.signal import SignalAdapter  # type: ignore[import-not-found]

CHARS_PER_SEC = 54.0
CLAUSES = [
    "The short answer is yes, and here is the longer explanation of why.",
    "Omen synthesises at roughly fifty four characters a second.",
    "So a reply of this length takes a while to finish.",
    "The relay streams, the proxy streams, and the caller asks one sentence at a time.",
]


def pcm_for(text: str) -> bytes:
    """Silence of the length omen would take to speak `text` (s16le, 24 kHz mono)."""
    seconds = max(0.2, len(text) / CHARS_PER_SEC)
    return b"\x00\x01" * int(24000 * seconds)


class RecordingAdapter(SignalAdapter):
    """The real adapter with Signal's RPC send replaced by a stopwatch."""

    def __init__(self) -> None:
        self.sent: list[tuple[float, int]] = []
        self.t0 = time.monotonic()

    async def _stop_typing_indicator(self, chat_id: str) -> None:
        return None

    async def _send_attachment(self, chat_id: str, file_path: str, media_label: str,
                               caption: str | None = None) -> SendResult:
        self.sent.append((time.monotonic() - self.t0, Path(file_path).stat().st_size))
        return SendResult(success=True)


async def run() -> int:
    adapter = RecordingAdapter()
    adapter.sent = []
    adapter.t0 = time.monotonic()

    if not adapter.supports_streaming_tts("chat", AudioFormat()):
        print("adapter declined streaming")
        return 1

    handle = await adapter.begin_streaming_tts("chat", AudioFormat())
    if handle is None:
        print("begin_streaming_tts returned None")
        return 1

    for clause in CLAUSES:
        # omen's pacing: a first byte almost at once, then the clause arriving as
        # it is synthesised (~0.15 s between chunks of one clause).
        blob = pcm_for(clause)
        step = max(1, len(blob) // 8)
        for i in range(0, len(blob), step):
            await adapter.write_streaming_tts(handle, blob[i:i + step])
            await asyncio.sleep(len(clause) / CHARS_PER_SEC / 8)
    await adapter.finish_streaming_tts(handle)

    sent = adapter.sent
    print(f"clauses: {len(CLAUSES)}  bubbles sent: {len(sent)}")
    for i, (t, size) in enumerate(sent, 1):
        print(f"  bubble {i}: at {t:5.2f}s  ({size} bytes)")
    whole_file_only = sum(max(0.2, len(c) / CHARS_PER_SEC) for c in CLAUSES)
    print(f"first bubble at {sent[0][0]:.2f}s; whole-file path sends one bubble at "
          f"{whole_file_only:.2f}s")
    return 0


def main() -> int:
    if not os.environ.get("PYTHONPATH"):
        print("set PYTHONPATH to a Hermes checkout first", file=sys.stderr)
        return 2
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
