"""Tests for complete server-sent event frame parsing."""

from typing import cast

import aiohttp

from huepy.client.http import HueHttpClient


class AsyncLineContent:
    """Minimal async byte-line iterator used by the SSE parser."""

    def __init__(self, *lines: bytes) -> None:
        self.lines = lines

    async def __aiter__(self):
        for line in self.lines:
            yield line


class FakeEventResponse:
    """Response-shaped object exposing only the stream content under test."""

    def __init__(self, *lines: bytes) -> None:
        self.content = AsyncLineContent(*lines)


class TestCompleteSSEFrames:
    async def test_multiline_data_preserves_the_frame_id_and_event_batch(
        self, bare_hue
    ):
        client = HueHttpClient(bare_hue.config)
        response = FakeEventResponse(
            b": keepalive\n",
            b"id: 1700000000:4\n",
            b"data: [\n",
            b'data: {"id":"event-1","type":"update"},\n',
            b'data: {"id":"event-2","type":"add"}\n',
            b"data: ]\n",
            b"\n",
        )

        frames = [
            frame
            async for frame in client._read_event_stream(
                cast("aiohttp.ClientResponse", cast("object", response))
            )
        ]

        assert len(frames) == 1
        assert frames[0].event_id == "1700000000:4"
        assert frames[0].received_at.tzinfo is not None
        assert frames[0].events == [
            {"id": "event-1", "type": "update"},
            {"id": "event-2", "type": "add"},
        ]

    async def test_an_unterminated_final_frame_is_still_emitted(self, bare_hue):
        client = HueHttpClient(bare_hue.config)
        response = FakeEventResponse(
            b"id: final:1\r\n",
            b'data: [{"id":"event-final","type":"delete"}]\r\n',
        )

        frames = [
            frame
            async for frame in client._read_event_stream(
                cast("aiohttp.ClientResponse", cast("object", response))
            )
        ]

        assert len(frames) == 1
        assert frames[0].event_id == "final:1"
        assert frames[0].events == [{"id": "event-final", "type": "delete"}]

    async def test_id_only_and_malformed_frames_still_advance_the_cursor(
        self, bare_hue
    ):
        client = HueHttpClient(bare_hue.config)
        response = FakeEventResponse(
            b"id: cursor:1\n",
            b"\n",
            b"data: not-json\n",
            b"\n",
            b'data: [{"id":"event-2","type":"update"}]\n',
            b"\n",
        )

        frames = [
            frame
            async for frame in client._read_event_stream(
                cast("aiohttp.ClientResponse", cast("object", response))
            )
        ]

        assert [(frame.event_id, frame.events) for frame in frames] == [
            ("cursor:1", []),
            ("cursor:1", []),
            ("cursor:1", [{"id": "event-2", "type": "update"}]),
        ]

    async def test_empty_id_resets_the_inherited_cursor(self, bare_hue):
        client = HueHttpClient(bare_hue.config)
        response = FakeEventResponse(
            b"id: cursor:1\n",
            b'data: [{"id":"event-1","type":"update"}]\n',
            b"\n",
            b"id:\n",
            b"\n",
            b'data: [{"id":"event-2","type":"update"}]\n',
            b"\n",
        )

        frames = [
            frame
            async for frame in client._read_event_stream(
                cast("aiohttp.ClientResponse", cast("object", response))
            )
        ]

        assert [frame.event_id for frame in frames] == ["cursor:1", "", ""]
