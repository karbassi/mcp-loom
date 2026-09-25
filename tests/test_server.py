"""Tests for the Loom MCP server.

Requires: pip install pytest anyio fastmcp
Run:      pytest test_server.py -v
"""

import os
import re
from unittest.mock import AsyncMock, patch

# Set dummy auth so lifespan can initialize without real credentials
os.environ.setdefault("LOOM_COOKIE", "test=dummy")

import pytest
from fastmcp.client import Client

from loom_mcp.client import LoomAPIError
from loom_mcp.server import mcp, _id, _ids

READ_TOOLS = {
    "list_videos",
    "search_videos",
    "get_video",
    "get_transcript",
    "get_captions",
    "get_summary",
    "get_chapters",
    "get_comments",
    "get_download_url",
    "get_tasks",
    "get_reactions",
    "get_meeting_notes",
    "list_folders",
    "list_spaces",
    "get_backlinks",
    "get_key_takeaways",
    "get_tags",
    "get_description",
    "get_confluence_pages",
    "search_folders",
    "get_last_watch_time",
    "get_watch_later_count",
    "get_total_videos_count",
    "get_frequent_reactions",
    "get_comment_reactions",
    "get_video_details",
    "get_user",
    "search_workspace_tags",
    "get_folder",
    "get_space",
}

DESTRUCTIVE_TOOLS = {
    "update_video_name",
    "update_video_description",
    "update_video_settings",
    "edit_comment",
    "delete_comment",
    "update_task",
    "delete_task",
    "delete_reaction",
    "delete_video",
    "archive_videos",
    "remove_from_watch_later",
    "rename_folder",
    "delete_folders",
    "move_videos",
    "move_folders",
}

CREATE_TOOLS = {
    "create_comment",
    "create_task",
    "add_reaction",
    "duplicate_video",
    "create_folder",
    "regenerate_mp4",
}

IDEMPOTENT_WRITE_TOOLS = {
    "download_media",  # writes a local file, not Loom data
    "approve_task",
    "respond_to_task",
    "toggle_following",
    "add_to_watch_later",
    "recover_video",
    "pin_video",
    "add_comment_reaction",
    "toggle_following_tag",
    "share_videos_to_spaces",
}

ALL_TOOLS = READ_TOOLS | DESTRUCTIVE_TOOLS | CREATE_TOOLS | IDEMPOTENT_WRITE_TOOLS


@pytest.fixture
async def client():
    async with Client(transport=mcp) as c:
        yield c


@pytest.mark.anyio
async def test_all_tools_registered(client):
    tools = await client.list_tools()
    names = {t.name for t in tools}
    missing = ALL_TOOLS - names
    extra = names - ALL_TOOLS
    assert not missing, f"Missing tools: {missing}"
    assert not extra, f"Unexpected tools: {extra}"


@pytest.mark.anyio
async def test_tool_count(client):
    tools = await client.list_tools()
    assert len(tools) == 61


@pytest.mark.anyio
async def test_read_tools_are_readonly(client):
    tools = await client.list_tools()
    tool_map = {t.name: t for t in tools}
    for name in READ_TOOLS:
        tool = tool_map[name]
        ann = tool.annotations
        assert ann is not None, f"{name} is missing annotations"
        assert ann.readOnlyHint is True, f"{name} should be readOnlyHint=True"
        assert ann.destructiveHint is False, f"{name} should be destructiveHint=False"
        assert ann.idempotentHint is True, f"{name} should be idempotentHint=True"


@pytest.mark.anyio
async def test_destructive_tools_are_marked(client):
    tools = await client.list_tools()
    tool_map = {t.name: t for t in tools}
    for name in DESTRUCTIVE_TOOLS:
        tool = tool_map[name]
        ann = tool.annotations
        assert ann is not None, f"{name} is missing annotations"
        assert ann.readOnlyHint is False, f"{name} should be readOnlyHint=False"
        assert ann.destructiveHint is True, f"{name} should be destructiveHint=True"


@pytest.mark.anyio
async def test_create_tools_are_not_destructive(client):
    tools = await client.list_tools()
    tool_map = {t.name: t for t in tools}
    for name in CREATE_TOOLS:
        tool = tool_map[name]
        ann = tool.annotations
        assert ann is not None, f"{name} is missing annotations"
        assert ann.readOnlyHint is False, f"{name} should be readOnlyHint=False"
        assert ann.destructiveHint is False, f"{name} should be destructiveHint=False"
        assert ann.idempotentHint is False, f"{name} should be idempotentHint=False"


@pytest.mark.anyio
async def test_idempotent_write_tools(client):
    tools = await client.list_tools()
    tool_map = {t.name: t for t in tools}
    for name in IDEMPOTENT_WRITE_TOOLS:
        tool = tool_map[name]
        ann = tool.annotations
        assert ann is not None, f"{name} is missing annotations"
        assert ann.readOnlyHint is False, f"{name} should be readOnlyHint=False"
        assert ann.destructiveHint is False, f"{name} should be destructiveHint=False"
        assert ann.idempotentHint is True, f"{name} should be idempotentHint=True"


@pytest.mark.anyio
async def test_all_tools_have_descriptions(client):
    tools = await client.list_tools()
    for tool in tools:
        assert tool.description, f"{tool.name} is missing a description"
        assert len(tool.description) >= 20, f"{tool.name} description is too short"


@pytest.mark.anyio
async def test_tool_names_are_snake_case(client):
    tools = await client.list_tools()
    for tool in tools:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", tool.name), (
            f"{tool.name} is not snake_case"
        )


# ---------------------------------------------------------------------------
# Input ID validation (synchronous — no client needed)
# ---------------------------------------------------------------------------


def test_id_accepts_valid_hex():
    assert _id("abc123def456", "test") == "abc123def456"


def test_id_accepts_uuid():
    assert _id("550e8400-e29b-41d4-a716-446655440000", "test")


def test_id_accepts_underscores_dots():
    assert _id("my_folder.v2", "test") == "my_folder.v2"


def test_id_rejects_empty():
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Invalid test"):
        _id("", "test")


def test_id_rejects_spaces():
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _id("abc 123", "test")


def test_id_rejects_slashes():
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _id("../../etc/passwd", "test")


def test_id_rejects_too_long():
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _id("a" * 201, "test")


def test_ids_validates_all():
    assert _ids(["abc", "def"], "test") == ["abc", "def"]


def test_ids_rejects_any_invalid():
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _ids(["valid", "has space"], "test")


# ---------------------------------------------------------------------------
# Mocked happy-path tests
# ---------------------------------------------------------------------------

VALID_ID = "a" * 32


@pytest.mark.anyio
async def test_get_video_happy_path(client):
    mock_video = {
        "id": VALID_ID,
        "name": "Test Video",
        "createdAt": "2024-01-01",
        "playable_duration": 120,
        "owner": {"display_name": "Alice"},
        "views": {"total": 10},
    }
    with patch(
        "loom_mcp.server.LoomClient.get_video",
        new_callable=AsyncMock,
        return_value=mock_video,
    ):
        result = await client.call_tool("get_video", {"video_id": VALID_ID})
    text = result.content[0].text
    assert "Test Video" in text
    assert VALID_ID in text


@pytest.mark.anyio
async def test_get_transcript_happy_path(client):
    mock_text = "00:00 [Alice] Hello world\n00:05 [Bob] Hi there"
    with patch(
        "loom_mcp.server.LoomClient.get_transcript_text",
        new_callable=AsyncMock,
        return_value=mock_text,
    ):
        result = await client.call_tool("get_transcript", {"video_id": VALID_ID})
    assert "Hello world" in result.content[0].text


@pytest.mark.anyio
async def test_get_transcript_empty(client):
    with patch(
        "loom_mcp.server.LoomClient.get_transcript_text",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await client.call_tool("get_transcript", {"video_id": VALID_ID})
    assert "No transcript" in result.content[0].text


@pytest.mark.anyio
async def test_search_videos_happy_path(client):
    mock_result = {
        "videos": [{"id": VALID_ID, "name": "Demo"}],
        "endCursor": None,
        "hasNextPage": False,
    }
    with patch(
        "loom_mcp.server.LoomClient.search_videos",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await client.call_tool("search_videos", {"query": "demo"})
    assert "Demo" in result.content[0].text


@pytest.mark.anyio
async def test_search_videos_empty(client):
    mock_result = {
        "videos": [],
        "endCursor": None,
        "hasNextPage": False,
    }
    with patch(
        "loom_mcp.server.LoomClient.search_videos",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        result = await client.call_tool("search_videos", {"query": "nonexistent"})
    assert "No videos matching" in result.content[0].text


@pytest.mark.anyio
async def test_get_comments_happy_path(client):
    mock_comments = [
        {
            "user_name": "Alice",
            "content": "Great video!",
            "time_stamp": 5,
            "children_comments": [],
        },
    ]
    with patch(
        "loom_mcp.server.LoomClient.get_comments",
        new_callable=AsyncMock,
        return_value=mock_comments,
    ):
        result = await client.call_tool("get_comments", {"video_id": VALID_ID})
    assert "Alice" in result.content[0].text
    assert "Great video!" in result.content[0].text


@pytest.mark.anyio
async def test_get_comments_empty(client):
    with patch(
        "loom_mcp.server.LoomClient.get_comments",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await client.call_tool("get_comments", {"video_id": VALID_ID})
    assert "No comments" in result.content[0].text


@pytest.mark.anyio
async def test_update_video_name_happy_path(client):
    with patch(
        "loom_mcp.server.LoomClient.update_video_name",
        new_callable=AsyncMock,
        return_value={"name": "New Name"},
    ):
        result = await client.call_tool(
            "update_video_name", {"video_id": VALID_ID, "name": "New Name"}
        )
    assert "Renamed to: New Name" in result.content[0].text


@pytest.mark.anyio
async def test_delete_video_happy_path(client):
    with patch(
        "loom_mcp.server.LoomClient.delete_video",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await client.call_tool("delete_video", {"video_id": VALID_ID})
    assert "Video deleted" in result.content[0].text


# ---------------------------------------------------------------------------
# Mocked error-path tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_video_api_error(client):
    with patch(
        "loom_mcp.server.LoomClient.get_video",
        new_callable=AsyncMock,
        side_effect=LoomAPIError("Session expired"),
    ):
        result = await client.call_tool(
            "get_video", {"video_id": VALID_ID}, raise_on_error=False
        )
    assert result.is_error
    assert "Session expired" in result.content[0].text


@pytest.mark.anyio
async def test_get_video_private(client):
    with patch(
        "loom_mcp.server.LoomClient.get_video",
        new_callable=AsyncMock,
        return_value={"message": "Private video"},
    ):
        result = await client.call_tool(
            "get_video", {"video_id": VALID_ID}, raise_on_error=False
        )
    assert result.is_error
    assert "Private video" in result.content[0].text


@pytest.mark.anyio
async def test_get_video_invalid_id(client):
    result = await client.call_tool(
        "get_video", {"video_id": "../../etc/passwd"}, raise_on_error=False
    )
    assert result.is_error
    assert "Invalid video ID" in result.content[0].text


@pytest.mark.anyio
async def test_delete_comment_api_error(client):
    with patch(
        "loom_mcp.server.LoomClient.delete_comment",
        new_callable=AsyncMock,
        side_effect=LoomAPIError("Not found"),
    ):
        result = await client.call_tool(
            "delete_comment", {"comment_id": VALID_ID}, raise_on_error=False
        )
    assert result.is_error
    assert "Not found" in result.content[0].text


@pytest.mark.anyio
async def test_archive_videos_invalid_ids(client):
    result = await client.call_tool(
        "archive_videos", {"video_ids": ["valid123", "has space"]}, raise_on_error=False
    )
    assert result.is_error
    assert "Invalid video ID" in result.content[0].text


@pytest.mark.anyio
async def test_get_download_url_happy_path(client):
    with patch(
        "loom_mcp.server.LoomClient.get_download_url",
        new_callable=AsyncMock,
        return_value="https://cdn.loom.com/video.mp4?token=abc",
    ):
        result = await client.call_tool("get_download_url", {"video_id": VALID_ID})
    assert "https://cdn.loom.com" in result.content[0].text


@pytest.mark.anyio
async def test_get_download_url_no_url(client):
    with patch(
        "loom_mcp.server.LoomClient.get_download_url",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await client.call_tool("get_download_url", {"video_id": VALID_ID})
    text = result.content[0].text
    assert "regenerate_mp4" in text
    assert "30 seconds" in text


@pytest.mark.anyio
async def test_regenerate_mp4_happy_path(client):
    with patch(
        "loom_mcp.server.LoomClient.regenerate_mp4",
        new_callable=AsyncMock,
        return_value={"__typename": "RegenerateMP4Payload", "success": True},
    ):
        result = await client.call_tool("regenerate_mp4", {"video_id": VALID_ID})
    assert '"success": true' in result.content[0].text


@pytest.mark.anyio
async def test_regenerate_mp4_not_found(client):
    with patch(
        "loom_mcp.server.LoomClient.regenerate_mp4",
        new_callable=AsyncMock,
        return_value={"__typename": "VideoNotFoundError", "message": "Video not found"},
    ):
        result = await client.call_tool(
            "regenerate_mp4", {"video_id": VALID_ID}, raise_on_error=False
        )
    assert result.is_error
    assert "Video not found" in result.content[0].text


@pytest.mark.anyio
async def test_regenerate_mp4_generic_error(client):
    with patch(
        "loom_mcp.server.LoomClient.regenerate_mp4",
        new_callable=AsyncMock,
        return_value={"__typename": "GenericError", "message": "Something went wrong"},
    ):
        result = await client.call_tool(
            "regenerate_mp4", {"video_id": VALID_ID}, raise_on_error=False
        )
    assert result.is_error
    assert "Something went wrong" in result.content[0].text


@pytest.mark.anyio
async def test_regenerate_mp4_invalid_id(client):
    result = await client.call_tool(
        "regenerate_mp4", {"video_id": "bad id"}, raise_on_error=False
    )
    assert result.is_error
    assert "Invalid video ID" in result.content[0].text
