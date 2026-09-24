import asyncio
import json
from pathlib import Path

import httpx

GRAPHQL_URL = "https://www.loom.com/graphql"


class LoomAPIError(Exception):
    """Raised when the Loom GraphQL API returns an error."""


class LoomClient:
    def __init__(self, cookies: str | None = None, auth_file: str | Path | None = None):
        if cookies:
            self.cookies = cookies
        elif auth_file:
            state = json.loads(Path(auth_file).read_text())
            self.cookies = "; ".join(
                f"{c['name']}={c['value']}"
                for c in state["cookies"]
                if "loom.com" in c.get("domain", "")
            )
        else:
            raise ValueError("Either cookies or auth_file must be provided")
        self._http = httpx.AsyncClient(timeout=30)
        self._semaphore = asyncio.Semaphore(5)

    @property
    def _headers(self) -> dict:
        return {
            "content-type": "application/json",
            "apollographql-client-name": "web",
            "x-loom-request-source": "loom_web",
            "cookie": self.cookies,
        }

    async def aclose(self):
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def graphql(
        self, operation: str, query: str, variables: dict | None = None
    ) -> dict:
        async with self._semaphore:
            try:
                r = await self._http.post(
                    GRAPHQL_URL,
                    headers=self._headers,
                    json={
                        "operationName": operation,
                        "query": query,
                        "variables": variables or {},
                    },
                )
            except httpx.ConnectError:
                raise LoomAPIError(
                    "Cannot connect to Loom. Check your internet connection."
                )
            if r.status_code == 401:
                raise LoomAPIError(
                    "Loom session expired. Get a fresh connect.sid cookie from your browser."
                )
            if r.status_code == 403:
                raise LoomAPIError(
                    "Access denied. Your session may lack permissions for this operation."
                )
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise LoomAPIError(f"Loom API returned HTTP {e.response.status_code}.")
            body = r.json()
            if body.get("errors"):
                messages = [e.get("message", "Unknown error") for e in body["errors"]]
                raise LoomAPIError("; ".join(messages))
            return body["data"]

    async def list_videos(self, limit: int = 50, cursor: str | None = None) -> dict:
        data = await self.graphql(
            "GetLoomsForLibrary",
            """query GetLoomsForLibrary($limit: Int!, $cursor: String, $source: LoomsSource!, $sortType: LoomsSortType!, $sortOrder: LoomsSortOrder!, $filters: [[LoomsCollectionFilter!]!]) {
              getLooms {
                __typename
                ... on GetLoomsPayload {
                  videos(first: $limit, after: $cursor, source: $source, sortType: $sortType, sortOrder: $sortOrder, filters: $filters) {
                    edges { cursor node { id name visibility } }
                    pageInfo { endCursor hasNextPage }
                  }
                }
              }
            }""",
            {
                "source": "MINE",
                "sortType": "RECENT",
                "sortOrder": "DESC",
                "filters": [[{"type": "CREATED_BY_ME"}], [{"type": "NOT_IN_FOLDER"}]],
                "limit": limit,
                "cursor": cursor,
            },
        )
        looms = data["getLooms"]
        if "videos" not in looms:
            raise LoomAPIError(
                f"Loom session expired or unauthorized ({looms.get('__typename', 'unknown')}). "
                "Get a fresh connect.sid cookie from your browser."
            )
        videos = looms["videos"]
        return {
            "videos": [e["node"] for e in videos["edges"]],
            "endCursor": videos["pageInfo"]["endCursor"],
            "hasNextPage": videos["pageInfo"]["hasNextPage"],
        }

    async def search_videos(
        self, query: str, limit: int = 50, cursor: str | None = None
    ) -> dict:
        """Text search with pagination."""
        data = await self.graphql(
            "SearchVideos",
            """query SearchVideos($searchQuery: String!, $first: Int, $after: String) {
              searchVideos {
                __typename
                ... on SearchVideosPayload {
                  videoResults(searchQuery: $searchQuery, first: $first, after: $after) {
                    edges { node { ... on VideoSearchResult { video { id name createdAt playable_duration } } } }
                    pageInfo { endCursor hasNextPage }
                  }
                }
              }
            }""",
            {"searchQuery": query, "first": limit, "after": cursor},
        )
        search = data["searchVideos"]
        if "videoResults" not in search:
            raise LoomAPIError(
                f"Loom session expired or unauthorized ({search.get('__typename', 'unknown')}). "
                "Get a fresh connect.sid cookie from your browser."
            )
        results = search["videoResults"]
        return {
            "videos": [e["node"]["video"] for e in results["edges"]],
            "endCursor": results["pageInfo"]["endCursor"],
            "hasNextPage": results["pageInfo"]["hasNextPage"],
        }

    async def get_video(self, video_id: str) -> dict:
        data = await self.graphql(
            "fetchVideoData",
            """query fetchVideoData($id: ID!, $password: String) {
              video: getVideo(id: $id, password: $password) {
                ... on RegularUserVideo {
                  id name createdAt playable_duration
                  tags complete comments_enabled
                  owner { id display_name avatars { thumb } }
                  views { total distinct }
                  totalComments totalReactions
                  video_properties { width height screen_type }
                  organization { id name }
                  calendarMeetingGuid storage
                }
                ... on VideoPasswordMissingOrIncorrect { id message }
                ... on PrivateVideo { id message }
              }
            }""",
            {"id": video_id, "password": None},
        )
        return data["video"]

    async def get_transcript_details(self, video_id: str) -> dict | None:
        data = await self.graphql(
            "fetchVideoTranscript",
            """query fetchVideoTranscript($videoId: ID!) {
              fetchVideoTranscript(videoId: $videoId) {
                ... on VideoTranscriptDetails {
                  idv2 video_id version
                  transcript_url captions_url source_url captions_source_url
                  transcription_status processing_service language
                }
              }
            }""",
            {"videoId": video_id},
        )
        return data.get("fetchVideoTranscript")

    async def get_transcript(self, video_id: str) -> list[dict] | None:
        details = await self.get_transcript_details(video_id)
        if not details or not details.get("source_url"):
            return None
        r = await self._http.get(details["source_url"])
        r.raise_for_status()
        phrases = r.json()["phrases"]
        return [
            {"timestamp": p["ts"], "speaker": p.get("speakerName"), "text": p["value"]}
            for p in phrases
        ]

    async def get_transcript_text(self, video_id: str) -> str | None:
        phrases = await self.get_transcript(video_id)
        if not phrases:
            return None
        lines = []
        for p in phrases:
            ts = _fmt_ts(p["timestamp"])
            speaker = f"[{p['speaker']}] " if p.get("speaker") else ""
            lines.append(f"{ts} {speaker}{p['text'].strip()}")
        return "\n".join(lines)

    async def get_captions(self, video_id: str) -> str | None:
        details = await self.get_transcript_details(video_id)
        if not details or not details.get("captions_source_url"):
            return None
        r = await self._http.get(details["captions_source_url"])
        if r.status_code != 200:
            return None
        return r.text

    async def get_download_url(self, video_id: str) -> str | None:
        data = await self.graphql(
            "GetVideoTranscodedUrl",
            """query GetVideoTranscodedUrl($videoId: ID!, $forceOriginal: Boolean) {
              getVideoTranscodedUrl(videoId: $videoId, forceOriginal: $forceOriginal) {
                ... on VideoSource { url }
              }
            }""",
            {"videoId": video_id, "forceOriginal": False},
        )
        return (data.get("getVideoTranscodedUrl") or {}).get("url")

    async def get_cdn_url(self, video_id: str, mime: str = "DASH") -> str | None:
        """Get the signed raw CDN URL for a video (DASH/M3U8 manifest or MP4).

        Unlike get_download_url, this works even when MP4 export is disabled.
        """
        if mime not in ("DASH", "M3U8", "MP4"):
            raise ValueError("mime must be one of DASH, M3U8, MP4")
        data = await self.graphql(
            "GetVideoCdnUrl",
            """query GetVideoCdnUrl($id: ID!, $password: String) {
              video: getVideo(id: $id, password: $password) {
                ... on RegularUserVideo {
                  nullableRawCdnUrl(acceptableMimes: [%s], password: $password) {
                    url
                  }
                }
              }
            }"""
            % mime,
            {"id": video_id, "password": None},
        )
        video = data.get("video") or {}
        return (video.get("nullableRawCdnUrl") or {}).get("url")

    async def download_media(
        self,
        video_id: str,
        out_path: str | Path,
        kind: str = "audio",
        quality: str = "best",
        start: float | None = None,
        end: float | None = None,
    ) -> Path:
        """Download a video's audio or video track to a local file via DASH."""
        from loom_mcp import media

        url = await self.get_cdn_url(video_id, "DASH")
        if not url:
            raise media.MediaError(
                "No DASH manifest available for this video (no access, or still processing)."
            )
        return await media.download_media(
            self._http,
            url,
            out_path,
            kind=kind,
            quality=quality,
            start=start,
            end=end,
        )

    async def get_chapters(self, video_id: str) -> dict | None:
        data = await self.graphql(
            "FetchChapters",
            """query FetchChapters($videoId: ID!, $password: String) {
              fetchVideoChapters(videoId: $videoId, password: $password) {
                ... on VideoChapters { id video_id content schema_version updatedAt edited_at auto_chapter_status }
                ... on EmptyChaptersPayload { content }
              }
            }""",
            {"videoId": video_id, "password": None},
        )
        return data.get("fetchVideoChapters")

    async def get_summary(self, video_id: str) -> dict | None:
        data = await self.graphql(
            "GetAutoSummaryStatus",
            """query GetAutoSummaryStatus($videoId: ID!, $password: String) {
              getAutoFeatureStatuses(videoId: $videoId, password: $password) {
                ... on AutoFeatureStatuses { id autoDescription autoDescriptionStatus }
                ... on Error { message }
              }
            }""",
            {"videoId": video_id, "password": None},
        )
        return data.get("getAutoFeatureStatuses")

    async def get_comments(self, video_id: str) -> list[dict]:
        data = await self.graphql(
            "fetchVideoComments",
            """query fetchVideoComments($id: ID!, $password: String) {
              video: getVideo(id: $id, password: $password) {
                ... on RegularUserVideo {
                  id
                  video_comments(includeDeleted: false) {
                    id content(withMentionMarkups: false) time_stamp(password: $password)
                    user_name createdAt
                    children_comments {
                      id content(withMentionMarkups: false) time_stamp(password: $password)
                      user_name createdAt
                    }
                  }
                }
              }
            }""",
            {"id": video_id, "password": None},
        )
        return (data.get("video") or {}).get("video_comments", [])

    async def get_tasks(self, video_id: str) -> list[dict]:
        data = await self.graphql(
            "GetVideoTasks",
            """query GetVideoTasks($videoId: ID!, $password: String) {
              getVideoTasks(videoId: $videoId, password: $password) {
                ... on GetVideoTasksPayload {
                  tasks {
                    id video_id content(withMentionMarkups: false)
                    time_stamp activity_type source
                    createdAt approved_at resolved_at
                    owner { id display_name }
                    responses {
                      id responded_at
                      user { id display_name }
                    }
                  }
                }
                ... on Error { message }
              }
            }""",
            {"videoId": video_id, "password": None},
        )
        result = data.get("getVideoTasks")
        if not result or "tasks" not in result:
            return []
        return result["tasks"]

    async def get_reactions(self, video_id: str) -> list[dict]:
        data = await self.graphql(
            "fetchVideoReactions",
            """query fetchVideoReactions($id: ID!, $password: String) {
              videoReactionsForVideo(videoId: $id, password: $password) {
                ... on VideoReactionsSuccessPayload {
                  reactions {
                    id time reaction extended_reaction category
                    user { id display_name }
                    anon_user_id anon_user_name
                  }
                }
              }
            }""",
            {"id": video_id, "password": None},
        )
        result = data.get("videoReactionsForVideo")
        if not result or "reactions" not in result:
            return []
        return result["reactions"]

    async def get_meeting_notes_url(self, video_id: str) -> str | None:
        data = await self.graphql(
            "GetMeetingNotesPage",
            """query GetMeetingNotesPage($videoId: ID!, $password: String) {
              getVideo(id: $videoId, password: $password) {
                ... on RegularUserVideo {
                  id calendarMeetingGuid
                  meetingNotesPage { pageUrl }
                }
              }
            }""",
            {"videoId": video_id, "password": None},
        )
        video = data.get("getVideo") or {}
        notes = video.get("meetingNotesPage") or {}
        return notes.get("pageUrl")

    async def list_folders(self, limit: int = 50, cursor: str | None = None) -> dict:
        data = await self.graphql(
            "GetPublishedFolders",
            """query GetPublishedFolders($first: Int!, $after: String, $source: FolderSource!, $sortType: LoomsSortType!, $sortOrder: LoomsSortOrder!, $filters: [LoomsCollectionFilter!]) {
              getPublishedFolders {
                ... on GetPublishedFoldersPayload {
                  folders(first: $first, after: $after, source: $source, sortType: $sortType, sortOrder: $sortOrder, filters: $filters) {
                    edges { cursor node { id name visibility } }
                    pageInfo { endCursor hasNextPage }
                  }
                }
              }
            }""",
            {
                "first": limit,
                "after": cursor,
                "source": "ACTIVE",
                "sortType": "RECENT",
                "sortOrder": "DESC",
                "filters": [{"type": "CREATED_BY_ME"}],
            },
        )
        folders = data["getPublishedFolders"]["folders"]
        return {
            "folders": [e["node"] for e in folders["edges"]],
            "endCursor": folders["pageInfo"]["endCursor"],
            "hasNextPage": folders["pageInfo"]["hasNextPage"],
        }

    async def list_spaces(self, limit: int = 50, cursor: str | None = None) -> dict:
        data = await self.graphql(
            "GetMySpaceMemberships",
            """query GetMySpaceMemberships($first: Int!, $after: String) {
              result: getMySpaceMemberships {
                ... on GetMySpaceMembershipsPayload {
                  memberships(first: $first, after: $after) {
                    edges {
                      node {
                        id unread
                        space { id name privacy is_primary }
                      }
                    }
                    pageInfo { endCursor hasNextPage }
                  }
                }
                ... on Error { message }
              }
            }""",
            {"first": limit, "after": cursor},
        )
        memberships = data["result"]["memberships"]
        return {
            "spaces": [e["node"]["space"] for e in memberships["edges"]],
            "endCursor": memberships["pageInfo"]["endCursor"],
            "hasNextPage": memberships["pageInfo"]["hasNextPage"],
        }

    async def get_backlinks(self, video_id: str) -> list[dict]:
        data = await self.graphql(
            "GetVideoBacklinks",
            """query GetVideoBacklinks($videoId: ID!, $password: String) {
              getVideoBacklinks(videoId: $videoId, password: $password) {
                ... on GetVideoBacklinksPayload {
                  backlinks { id source sourceLink title isSynced }
                }
              }
            }""",
            {"videoId": video_id, "password": None},
        )
        result = data.get("getVideoBacklinks")
        if not result or "backlinks" not in result:
            return []
        return result["backlinks"]

    async def get_key_takeaways(self, video_id: str) -> list[str]:
        data = await self.graphql(
            "GetKeyTakeaways",
            """query GetKeyTakeaways($videoId: ID!) {
              getKeyTakeaways(videoId: $videoId) {
                ... on GetKeyTakeawaysPayload { takeaways }
              }
            }""",
            {"videoId": video_id},
        )
        result = data.get("getKeyTakeaways")
        if not result:
            return []
        return result.get("takeaways") or []

    async def get_tags(self, video_id: str) -> list[str]:
        data = await self.graphql(
            "GetTagsByVideoId",
            """query GetTagsByVideoId($videoId: ID!) {
              getTagsByVideoId(videoId: $videoId) {
                ... on GetTagsByVideoIdPayload { tags }
              }
            }""",
            {"videoId": video_id},
        )
        result = data.get("getTagsByVideoId")
        if not result:
            return []
        return result.get("tags") or []

    async def get_confluence_pages(self, video_id: str) -> list[dict]:
        data = await self.graphql(
            "GetVideoConfluencePages",
            """query GetVideoConfluencePages($videoId: ID!) {
              getVideoConfluencePages(videoId: $videoId) {
                ... on GetVideoConfluencePagesPayload {
                  pages { url title }
                }
              }
            }""",
            {"videoId": video_id},
        )
        result = data.get("getVideoConfluencePages")
        if not result:
            return []
        return result.get("pages") or []

    async def get_description(self, video_id: str) -> str | None:
        data = await self.graphql(
            "GetVideoDescription",
            """query GetVideoDescription($videoId: ID!) {
              getVideo(id: $videoId) {
                ... on RegularUserVideo { id description }
              }
            }""",
            {"videoId": video_id},
        )
        return (data.get("getVideo") or {}).get("description")

    async def get_space(self, space_id: str) -> dict | None:
        data = await self.graphql(
            "GetSpace",
            """query GetSpace($spaceId: ID!) {
              getSpace(spaceId: $spaceId) {
                ... on GetSpacePayload {
                  space { id name privacy is_primary }
                }
              }
            }""",
            {"spaceId": space_id},
        )
        result = data.get("getSpace")
        if not result:
            return None
        return result.get("space")

    async def search_folders(self, query: str) -> list[dict]:
        data = await self.graphql(
            "SearchFolders",
            """query SearchFolders($searchQuery: String!) {
              searchFolders(searchQuery: $searchQuery) {
                ... on SearchFoldersPayload {
                  folders { id name }
                }
              }
            }""",
            {"searchQuery": query},
        )
        result = data.get("searchFolders")
        if not result:
            return []
        return result.get("folders") or []

    async def get_last_watch_time(self, video_id: str) -> float | None:
        data = await self.graphql(
            "GetLastWatchTime",
            """query GetLastWatchTime($videoId: ID!) {
              getLastWatchTime(videoId: $videoId) {
                ... on GetLastWatchTimePayload { lastWatchTime }
              }
            }""",
            {"videoId": video_id},
        )
        result = data.get("getLastWatchTime")
        if not result:
            return None
        return result.get("lastWatchTime")

    async def get_watch_later_count(self) -> int:
        data = await self.graphql(
            "GetUserWatchLaterListCount",
            """query GetUserWatchLaterListCount {
              getUserWatchLaterListCount {
                ... on WatchLaterListVideoCount { count }
              }
            }""",
        )
        result = data.get("getUserWatchLaterListCount")
        return (result or {}).get("count", 0)

    async def get_total_videos_count(self, user_id: str) -> int:
        data = await self.graphql(
            "GetTotalVideosCountByUser",
            """query GetTotalVideosCountByUser($userId: ID!) {
              getTotalVideosCountByUser(userId: $userId) {
                ... on TotalVideosCountByUserPayload { videos_count }
              }
            }""",
            {"userId": user_id},
        )
        result = data.get("getTotalVideosCountByUser")
        return (result or {}).get("videos_count", 0)

    async def get_frequent_reactions(self) -> list[str]:
        data = await self.graphql(
            "GetRecentlyFrequentUserReactions",
            """query GetRecentlyFrequentUserReactions {
              getRecentlyFrequentUserReactions {
                ... on getRecentlyFrequentUserReactionsPayload { reactions }
              }
            }""",
        )
        result = data.get("getRecentlyFrequentUserReactions")
        return (result or {}).get("reactions") or []

    async def get_comment_reactions(
        self, comment_id: str, comment_type: str = "COMMENT"
    ) -> list[dict]:
        data = await self.graphql(
            "GetCommentReactions",
            """query GetCommentReactions($commentGuid: ID!, $commentType: CommentType!) {
              getCommentReactions(commentGuid: $commentGuid, commentType: $commentType) {
                ... on GetCommentReactionsPayload {
                  commentReactions { id userName extendedReaction createdAt }
                }
                ... on GenericError { message }
              }
            }""",
            {"commentGuid": comment_id, "commentType": comment_type},
        )
        result = data.get("getCommentReactions")
        if not result or "commentReactions" not in result:
            return []
        return result["commentReactions"]

    async def get_folder(self, folder_id: str) -> dict | None:
        data = await self.graphql(
            "GetFolder",
            """query GetFolder($folderId: ID!) {
              folder(id: $folderId) {
                __typename
                ... on SharedFolder {
                  id name visibility createdAt updatedAt
                  created_by { id display_name }
                }
              }
            }""",
            {"folderId": folder_id},
        )
        return data.get("folder")

    # --- Mutations ---

    async def update_video_name(self, video_id: str, name: str) -> dict:
        data = await self.graphql(
            "UpdateVideoName",
            """mutation UpdateVideoName($id: ID!, $name: String!) {
              updateVideoName(id: $id, name: $name) {
                ... on RegularUserVideo { id name }
              }
            }""",
            {"id": video_id, "name": name},
        )
        return data.get("updateVideoName") or {}

    async def update_video_description(self, video_id: str, description: str) -> dict:
        data = await self.graphql(
            "UpdateVideoDescription",
            """mutation UpdateVideoDescription($id: ID!, $description: String!) {
              updateVideoDescription(id: $id, description: $description) {
                ... on RegularUserVideo { id description }
              }
            }""",
            {"id": video_id, "description": description},
        )
        return data.get("updateVideoDescription") or {}

    async def create_comment(
        self, video_id: str, content: str, timestamp: int = 0
    ) -> dict:
        data = await self.graphql(
            "CreateVideoComment",
            """mutation CreateVideoComment($videoId: ID!, $content: String!, $timestamp: Int!) {
              createVideoComment(videoId: $videoId, content: $content, timestamp: $timestamp) {
                ... on PublicVideoComment { id content time_stamp user_name createdAt }
              }
            }""",
            {"videoId": video_id, "content": content, "timestamp": timestamp},
        )
        return data.get("createVideoComment") or {}

    async def archive_videos(self, video_ids: list[str], archive: bool = True) -> dict:
        data = await self.graphql(
            "ArchiveVideos",
            """mutation ArchiveVideos($videoIds: [ID!]!, $isArchived: Boolean!) {
              archiveVideos(videoIds: $videoIds, isArchived: $isArchived) {
                __typename
              }
            }""",
            {"videoIds": video_ids, "isArchived": archive},
        )
        return data.get("archiveVideos") or {}

    async def duplicate_video(self, video_id: str) -> dict:
        data = await self.graphql(
            "DuplicateVideo",
            """mutation DuplicateVideo($videoId: ID!) {
              duplicateVideo(videoId: $videoId) {
                __typename
              }
            }""",
            {"videoId": video_id},
        )
        return data.get("duplicateVideo") or {}

    async def edit_comment(
        self,
        comment_id: str,
        video_id: str,
        content: str,
        comment_type: str = "COMMENT",
    ) -> dict:
        data = await self.graphql(
            "EditComment",
            """mutation EditComment($id: ID!, $videoId: ID!, $content: String!, $type: PublicVideoCommentType!) {
              editComment(id: $id, videoId: $videoId, content: $content, type: $type) {
                __typename
              }
            }""",
            {
                "id": comment_id,
                "videoId": video_id,
                "content": content,
                "type": comment_type,
            },
        )
        return data.get("editComment") or {}

    async def delete_comment(
        self, comment_id: str, comment_type: str = "COMMENT"
    ) -> bool:
        data = await self.graphql(
            "DeleteComment",
            """mutation DeleteComment($id: ID!, $type: PublicVideoCommentType!) {
              deleteComment(id: $id, type: $type)
            }""",
            {"id": comment_id, "type": comment_type},
        )
        return data.get("deleteComment", False)

    async def create_task(
        self, video_id: str, content: str, timestamp: int = 0
    ) -> dict:
        data = await self.graphql(
            "CreateVideoTask",
            """mutation CreateVideoTask($videoId: ID!, $content: String!, $timestamp: Int!) {
              createVideoTask(videoId: $videoId, content: $content, timestamp: $timestamp) {
                ... on CreateVideoTaskPayload {
                  task { id content time_stamp }
                }
              }
            }""",
            {"videoId": video_id, "content": content, "timestamp": timestamp},
        )
        result = data.get("createVideoTask")
        return (result or {}).get("task") or result or {}

    async def delete_task(self, task_id: str) -> dict:
        data = await self.graphql(
            "DeleteVideoTask",
            """mutation DeleteVideoTask($id: ID!) {
              deleteVideoTask(id: $id) {
                __typename
              }
            }""",
            {"id": task_id},
        )
        return data.get("deleteVideoTask") or {}

    async def approve_task(self, task_id: str) -> dict:
        data = await self.graphql(
            "ApproveVideoTask",
            """mutation ApproveVideoTask($id: ID!) {
              approveVideoTask(id: $id) {
                __typename
              }
            }""",
            {"id": task_id},
        )
        return data.get("approveVideoTask") or {}

    async def respond_to_task(self, task_id: str, responded: bool = True) -> dict:
        data = await self.graphql(
            "RespondToVideoTask",
            """mutation RespondToVideoTask($id: ID!, $responded: Boolean!) {
              respondToVideoTask(id: $id, responded: $responded) {
                __typename
              }
            }""",
            {"id": task_id, "responded": responded},
        )
        return data.get("respondToVideoTask") or {}

    async def add_reaction(self, video_id: str, time: int, reaction_type: str) -> dict:
        data = await self.graphql(
            "AddVideoReaction",
            """mutation AddVideoReaction($videoId: ID!, $time: Int!, $type: String!) {
              addVideoReaction(videoId: $videoId, time: $time, type: $type) {
                ... on PublicVideoReaction { id time reaction extended_reaction category }
                ... on InvalidRequestWarning { message }
              }
            }""",
            {"videoId": video_id, "time": time, "type": reaction_type},
        )
        return data.get("addVideoReaction") or {}

    async def delete_reaction(self, reaction_id: str) -> bool:
        data = await self.graphql(
            "DeleteVideoReaction",
            """mutation DeleteVideoReaction($reactionId: ID!) {
              deleteVideoReaction(reactionId: $reactionId)
            }""",
            {"reactionId": reaction_id},
        )
        return data.get("deleteVideoReaction", False)

    async def toggle_following(self, video_id: str, follow: bool) -> dict:
        data = await self.graphql(
            "ToggleFollowingVideo",
            """mutation ToggleFollowingVideo($videoId: String!, $follow: Boolean!) {
              toggleFollowingVideo(videoId: $videoId, follow: $follow) {
                ... on UserFollowsStream { id follow }
              }
            }""",
            {"videoId": video_id, "follow": follow},
        )
        return data.get("toggleFollowingVideo") or {}

    async def delete_video(self, video_id: str) -> bool:
        data = await self.graphql(
            "DeleteVideo",
            """mutation DeleteVideo($id: ID!) {
              deleteVideo(id: $id)
            }""",
            {"id": video_id},
        )
        return data.get("deleteVideo", False)

    async def add_to_watch_later(
        self, video_id: str, minutes_from_utc: int = 0
    ) -> dict:
        data = await self.graphql(
            "AddVideoToWatchLaterList",
            """mutation AddVideoToWatchLaterList($videoId: ID!, $minutesFromUTC: Int!) {
              addVideoToWatchLaterList(videoId: $videoId, minutesFromUTC: $minutesFromUTC) {
                __typename
              }
            }""",
            {"videoId": video_id, "minutesFromUTC": minutes_from_utc},
        )
        return data.get("addVideoToWatchLaterList") or {}

    async def remove_from_watch_later(self, video_id: str) -> dict:
        data = await self.graphql(
            "RemoveVideoFromWatchLaterList",
            """mutation RemoveVideoFromWatchLaterList($videoId: ID!) {
              removeVideoFromWatchLaterList(videoId: $videoId) {
                __typename
              }
            }""",
            {"videoId": video_id},
        )
        return data.get("removeVideoFromWatchLaterList") or {}

    async def create_folder(self, name: str) -> dict:
        data = await self.graphql(
            "CreateFolder",
            """mutation CreateFolder($name: String!) {
              createFolder(name: $name) {
                ... on CreateFolderPayload { __typename }
              }
            }""",
            {"name": name},
        )
        return data.get("createFolder") or {}

    async def rename_folder(self, folder_id: str, name: str) -> dict:
        data = await self.graphql(
            "RenameFolder",
            """mutation RenameFolder($folderId: ID!, $name: String!) {
              renameFolder(folderId: $folderId, name: $name) {
                __typename
              }
            }""",
            {"folderId": folder_id, "name": name},
        )
        return data.get("renameFolder") or {}

    async def delete_folders(self, folder_ids: list[str]) -> dict:
        data = await self.graphql(
            "BulkDeleteFolders",
            """mutation BulkDeleteFolders($folderIds: [ID!]!) {
              bulkDeleteFolders(folderIds: $folderIds) {
                __typename
              }
            }""",
            {"folderIds": folder_ids},
        )
        return data.get("bulkDeleteFolders") or {}

    async def get_user_by_id(self, user_id: str) -> dict | None:
        data = await self.graphql(
            "GetUserById",
            """query GetUserById($userId: ID!) {
              getUserById(userId: $userId) {
                ... on RegularUserPayload {
                  user {
                    id display_name first_name last_name email
                    company_name companyPosition
                    avatars { thumb large }
                  }
                }
                ... on GenericError { message }
              }
            }""",
            {"userId": user_id},
        )
        result = data.get("getUserById")
        if not result:
            return None
        return result.get("user")

    async def search_workspace_tags(self, query: str) -> list[dict]:
        data = await self.graphql(
            "SearchWorkspaceTags",
            """query SearchWorkspaceTags($query: String!) {
              searchWorkspaceTags(query: $query) {
                ... on MatchedTags { tags { __typename } }
                ... on GenericError { message }
              }
            }""",
            {"query": query},
        )
        result = data.get("searchWorkspaceTags")
        if not result:
            return []
        return result.get("tags") or []

    async def bulk_move_videos(
        self, video_ids: list[str], new_parent_folder_id: str
    ) -> dict:
        data = await self.graphql(
            "BulkMoveVideos",
            """mutation BulkMoveVideos($videoIds: [ID!]!, $newParentFolderId: ID!) {
              bulkMoveVideos(videoIds: $videoIds, newParentFolderId: $newParentFolderId) {
                __typename
              }
            }""",
            {"videoIds": video_ids, "newParentFolderId": new_parent_folder_id},
        )
        return data.get("bulkMoveVideos") or {}

    async def bulk_move_folders(
        self, folder_ids: list[str], new_parent_folder_id: str
    ) -> dict:
        data = await self.graphql(
            "BulkMoveFolders",
            """mutation BulkMoveFolders($folderIds: [ID!]!, $newParentFolderId: ID!) {
              bulkMoveFolders(folderIds: $folderIds, newParentFolderId: $newParentFolderId) {
                __typename
              }
            }""",
            {"folderIds": folder_ids, "newParentFolderId": new_parent_folder_id},
        )
        return data.get("bulkMoveFolders") or {}

    async def recover_video(self, video_id: str) -> dict:
        data = await self.graphql(
            "RecoverVideo",
            """mutation RecoverVideo($videoId: ID!, $force: Boolean!) {
              recoverVideo(videoId: $videoId, force: $force) {
                __typename
              }
            }""",
            {"videoId": video_id, "force": False},
        )
        return data.get("recoverVideo") or {}

    async def update_video_pin_status(self, video_id: str, pinned: bool) -> dict:
        data = await self.graphql(
            "UpdateVideoPinStatus",
            """mutation UpdateVideoPinStatus($input: UpdateVideoPinStatusInput!) {
              updateVideoPinStatus(input: $input) {
                __typename
              }
            }""",
            {"input": {"videoId": video_id, "newStatus": pinned, "context": "library"}},
        )
        return data.get("updateVideoPinStatus") or {}

    async def add_comment_reaction(
        self, comment_guid: str, extended_reaction: str, comment_type: str = "COMMENT"
    ) -> dict:
        data = await self.graphql(
            "AddCommentReaction",
            """mutation AddCommentReaction($input: AddCommentReactionInput!) {
              addCommentReaction(input: $input) {
                __typename
              }
            }""",
            {
                "input": {
                    "commentGuid": comment_guid,
                    "commentType": comment_type,
                    "extendedReaction": extended_reaction,
                }
            },
        )
        return data.get("addCommentReaction") or {}

    async def toggle_following_tag(self, tag: str, follow: bool) -> dict:
        data = await self.graphql(
            "ToggleFollowingTag",
            """mutation ToggleFollowingTag($tag: String!, $follow: Boolean!) {
              toggleFollowingTag(tag: $tag, follow: $follow) {
                ... on UserFollowsStream { id follow }
              }
            }""",
            {"tag": tag, "follow": follow},
        )
        return data.get("toggleFollowingTag") or {}

    async def batch_share_videos_to_spaces(
        self, video_ids: list[str], space_ids: list[str]
    ) -> dict:
        data = await self.graphql(
            "BatchShareVideosToSpaces",
            """mutation BatchShareVideosToSpaces($videoIds: [ID!]!, $spaceIds: [ID!]!) {
              batchShareVideosToSpaces(videoIds: $videoIds, spaceIds: $spaceIds) {
                ... on BatchShareVideosToSpacesPayload { __typename }
                ... on GenericError { message }
              }
            }""",
            {"videoIds": video_ids, "spaceIds": space_ids},
        )
        return data.get("batchShareVideosToSpaces") or {}

    async def update_video_settings(self, video_id: str, settings: dict) -> dict:
        data = await self.graphql(
            "UpdateVideoSettings",
            """mutation UpdateVideoSettings($videoId: ID!, $settings: VideoSettingsInput!) {
              updateVideoSettings(videoId: $videoId, settings: $settings) {
                ... on UpdateVideoSettingsResponse { __typename }
                ... on GenericError { message }
                ... on InvalidRequestWarning { message }
              }
            }""",
            {"videoId": video_id, "settings": settings},
        )
        return data.get("updateVideoSettings") or {}

    async def update_video_task(self, task_id: str, content: str) -> dict:
        data = await self.graphql(
            "UpdateVideoTask",
            """mutation UpdateVideoTask($id: ID!, $content: String!) {
              updateVideoTask(id: $id, content: $content) {
                ... on GenericError { message }
                __typename
              }
            }""",
            {"id": task_id, "content": content},
        )
        return data.get("updateVideoTask") or {}

    async def regenerate_mp4(self, video_id: str) -> dict:
        data = await self.graphql(
            "RegenerateMP4",
            """mutation RegenerateMP4($input: RegenerateMP4Input!) {
              regenerateMP4(input: $input) {
                ... on RegenerateMP4Payload {
                  __typename
                  success
                }
                ... on GenericError {
                  __typename
                  message
                }
                ... on VideoNotFoundError {
                  __typename
                  message
                }
                __typename
              }
            }""",
            {"input": {"videoId": video_id, "regenerationType": "DOWNLOAD"}},
        )
        return data.get("regenerateMP4") or {}


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"
