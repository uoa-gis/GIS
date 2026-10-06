"""Earth Engine login from ``EE_PROJECT`` and ``GOOGLE_APPLICATION_CREDENTIALS``.

Load repo-root ``.env`` first. If ``GOOGLE_APPLICATION_CREDENTIALS`` points at a
service-account JSON, use that key. Otherwise fall back to Application Default
Credentials / user login (``earthengine authenticate``).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parents[1]
load_dotenv(_REPO / ".env")

_EE_SCOPES = (
    "https://www.googleapis.com/auth/earthengine",
    "https://www.googleapis.com/auth/cloud-platform",
)


def project_id() -> str:
    return os.environ.get("EE_PROJECT") or "geog761-dongwook"


def credentials_path() -> Path | None:
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw or not str(raw).strip():
        return None
    return Path(raw).expanduser()


def initialize_ee():
    """``ee.Initialize`` with service-account JSON when set, else default creds."""
    import ee

    project = project_id()
    path = credentials_path()
    if path is not None:
        if not path.is_file():
            raise FileNotFoundError(
                f"GOOGLE_APPLICATION_CREDENTIALS is not a file: {path}"
            )
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            str(path),
            scopes=_EE_SCOPES,
        )
        ee.Initialize(credentials=credentials, project=project)
    else:
        ee.Initialize(project=project)
    return project
