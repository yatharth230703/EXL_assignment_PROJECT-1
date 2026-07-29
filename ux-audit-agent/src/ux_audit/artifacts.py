"""A local-disk artifact service (spec §8.3).

ADK ships `InMemoryArtifactService` (dies with the process) and
`GcsArtifactService` (cloud dependency). Neither fits a local crawl whose
screenshots are consumed by a *different session* later — the advisor chats about
a run that finished hours ago in another process.

Screenshots stay where the crawl wrote them: `runs/<run_id>/screenshots/`. This
service just exposes that directory through the artifact API so the advisor can
pull one into context with `LoadArtifactsTool` and actually discuss the picture.

Filenames are the screenshot paths relative to `runs/`, optionally with the
`user:` namespace prefix — session-scoped artifacts written by the crawl would be
invisible to the advisor's session, which is the whole problem being avoided.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Optional, Union

from google.adk.artifacts import BaseArtifactService
from google.genai import types

try:  # ArtifactVersion moved around across 2.x; tolerate either location.
    from google.adk.artifacts.base_artifact_service import ArtifactVersion
except ImportError:  # pragma: no cover
    ArtifactVersion = None  # type: ignore[assignment]

USER_NS = "user:"


class LocalDirArtifactService(BaseArtifactService):
    """Read-mostly artifact view over a directory tree.

    Versioning is nominal: files on disk have exactly one version (0). The crawl
    overwrites rather than versioning, so pretending otherwise would be a lie the
    advisor could trip over.
    """

    def __init__(self, root: str | Path = "runs"):
        self.root = Path(root)

    # -- path plumbing ------------------------------------------------------

    def _strip_ns(self, filename: str) -> str:
        return filename[len(USER_NS):] if filename.startswith(USER_NS) else filename

    def _resolve(self, filename: str) -> Path:
        rel = self._strip_ns(filename).lstrip("/")
        # Keep the advisor inside runs/ — it takes filenames from finding metadata,
        # and a traversal there would read arbitrary files off disk.
        candidate = (self.root / rel).resolve()
        root = self.root.resolve()
        if not str(candidate).startswith(str(root)):
            raise ValueError(f"artifact path escapes {root}: {filename}")
        return candidate

    # -- BaseArtifactService ------------------------------------------------

    async def save_artifact(
        self, *, app_name: str, user_id: str, filename: str,
        artifact: Union[types.Part, dict[str, Any]],
        session_id: Optional[str] = None, custom_metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        path = self._resolve(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = None
        if isinstance(artifact, types.Part):
            if artifact.inline_data is not None:
                data = artifact.inline_data.data
        elif isinstance(artifact, dict):
            data = artifact.get("data")
        if data is None:
            raise ValueError("LocalDirArtifactService can only save inline binary data")
        path.write_bytes(data)
        return 0

    async def load_artifact(
        self, *, app_name: str, user_id: str, filename: str,
        session_id: Optional[str] = None, version: Optional[int] = None,
    ) -> Optional[types.Part]:
        try:
            path = self._resolve(filename)
        except ValueError:
            return None
        if not path.is_file():
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return types.Part(inline_data=types.Blob(mime_type=mime, data=path.read_bytes()))

    async def list_artifact_keys(
        self, *, app_name: str, user_id: str, session_id: Optional[str] = None
    ) -> list[str]:
        if not self.root.exists():
            return []
        out = [
            USER_NS + str(p.relative_to(self.root))
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")
        ]
        return out

    async def list_versions(
        self, *, app_name: str, user_id: str, filename: str, session_id: Optional[str] = None
    ) -> list[int]:
        try:
            return [0] if self._resolve(filename).is_file() else []
        except ValueError:
            return []

    async def list_artifact_versions(
        self, *, app_name: str, user_id: str, filename: str, session_id: Optional[str] = None
    ) -> list:
        if ArtifactVersion is None:
            return []
        try:
            path = self._resolve(filename)
        except ValueError:
            return []
        if not path.is_file():
            return []
        try:
            return [ArtifactVersion(version=0)]
        except Exception:
            return []

    async def get_artifact_version(
        self, *, app_name: str, user_id: str, filename: str,
        session_id: Optional[str] = None, version: Optional[int] = None,
    ):
        versions = await self.list_artifact_versions(
            app_name=app_name, user_id=user_id, filename=filename, session_id=session_id
        )
        return versions[0] if versions else None

    async def delete_artifact(
        self, *, app_name: str, user_id: str, filename: str, session_id: Optional[str] = None
    ) -> None:
        # Deliberately a no-op: the advisor is a reader. Deleting a screenshot
        # would destroy the evidence a finding points at.
        return None
