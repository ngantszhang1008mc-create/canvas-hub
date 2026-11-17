"""Gemini on Vertex AI using Google Application Default Credentials (ADC).

Only constructed Google endpoints receive credentials. Tokens are refreshed in
memory before each model turn; neither credentials nor tokens are saved here.
https://cloud.google.com/vertex-ai/generative-ai/docs/migrate/openai/auth-and-credentials
"""
from __future__ import annotations

import re

from ..util import InvalidInput
from .providers import OpenAICompatibleProvider, ProviderError
from .usage import normalize_usage

VERTEX_FIELDS = {"vertex_project", "vertex_location"}


def validate_vertex(cfg: dict) -> None:
    project = cfg.get("vertex_project", "")
    location = cfg.get("vertex_location", "")
    if not isinstance(project, str) or not re.fullmatch(r"(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{1,30})", project):
        raise InvalidInput("Enter a Google Cloud project ID (6–30 lowercase letters, digits or hyphens) or project number.")
    if not isinstance(location, str) or not re.fullmatch(r"(?:global|[a-z]+(?:-[a-z]+)+[0-9]+)", location):
        raise InvalidInput("Enter a Vertex AI location such as global, us-central1 or asia-southeast1.")
    model = cfg.get("model", "")
    if not isinstance(model, str) or (model and not re.fullmatch(r"(?:google/)?gemini-[A-Za-z0-9._-]+", model)):
        raise InvalidInput("Enter a Gemini model ID, optionally prefixed with google/.")


def vertex_base_url(cfg: dict) -> str:
    validate_vertex(cfg)
    location = cfg["vertex_location"]
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{cfg['vertex_project']}/locations/{location}/endpoints/openapi/"


class VertexAIProvider(OpenAICompatibleProvider):
    name = "vertex_ai"

    def __init__(self, cfg: dict):
        import google.auth
        from google.auth.exceptions import GoogleAuthError

        try:
            base_url = vertex_base_url(cfg)
        except InvalidInput as e:
            raise ProviderError(str(e)) from e
        model = cfg.get("model") or ""
        if not model:
            raise ProviderError("Enter a Gemini model name in Assistant settings.")
        try:
            self.credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except (GoogleAuthError, OSError, ValueError) as e:
            raise ProviderError("Google Application Default Credentials are unavailable. Run "
                                "`gcloud auth application-default login` on the computer running Canvas Hub, "
                                "or set GOOGLE_APPLICATION_CREDENTIALS to your credential file, then try again.") from e
        # Explicit values prevent OPENAI_API_KEY / OPENAI_BASE_URL and another
        # setup's saved API key from changing Vertex authentication or routing.
        super().__init__({**cfg, "preset": "vertex_ai", "base_url": base_url,
                          "model": model if model.startswith("google/") else f"google/{model}",
                          "api_key": "adc-pending"})
        self.usage_provider = "gemini"
        self.include_usage = True
        if self.credentials.quota_project_id:
            self.client = self.client.with_options(default_headers={"x-goog-user-project": self.credentials.quota_project_id})

    def complete(self, system, transcript, tools, on_text=None):
        import requests
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request

        self.last_usage = normalize_usage(None, self.usage_provider, final=False)
        try:
            if not self.credentials.valid:
                with requests.Session() as session:
                    self.credentials.refresh(Request(session=session))
            if not self.credentials.valid or not self.credentials.token:
                raise ProviderError("Google credentials did not provide a valid access token. "
                                    "Run `gcloud auth application-default login` and try again.")
        except (GoogleAuthError, requests.RequestException, OSError, ValueError) as e:
            raise ProviderError("Could not refresh Google credentials for Vertex AI. Check your connection and "
                                "run `gcloud auth application-default login` again if needed.") from e
        self.client.api_key = self.credentials.token
        try:
            return super().complete(system, transcript, tools, on_text=on_text)
        except ProviderError as e:
            if isinstance(e.__cause__, self._sdk.AuthenticationError):
                raise ProviderError("Vertex AI rejected the Google credentials. Run "
                                    "`gcloud auth application-default login` again and check your account.") from e
            if isinstance(e.__cause__, self._sdk.PermissionDeniedError):
                raise ProviderError("Vertex AI access was denied. Check that the Vertex AI API and billing are "
                                    "enabled for this project, and your Google account has permission to use the model.") from e
            if isinstance(e.__cause__, self._sdk.NotFoundError):
                raise ProviderError("Vertex AI model or project not found. Check the project ID, location and "
                                    "Gemini model name in Assistant settings.") from e
            raise
