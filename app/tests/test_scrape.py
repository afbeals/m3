# Tests for app/scrape.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.scrape import (
    ScrapeError,
    SelectorMissingError,
    fetch_html,
    _MAX_RESPONSE_BYTES,
)


def _make_response(
    status_code: int = 200,
    content_type: str = "text/html; charset=utf-8",
    text: str = "<html><body>Hello</body></html>",
) -> MagicMock:
    """Build a mock httpx.Response so raise_for_status() works without a real request."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}
    resp.content = text.encode("utf-8")
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Successful fetch
# ---------------------------------------------------------------------------

def test_fetch_html_returns_body_on_success():
    with patch("app.scrape.httpx.Client") as MockClient:
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.return_value = _make_response(text="<html>ok</html>")
        result = fetch_html("https://example.com/page")
    assert "<html>ok</html>" in result


def test_fetch_html_sets_user_agent():
    with patch("app.scrape.httpx.Client") as MockClient:
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.return_value = _make_response()
        fetch_html("https://example.com/page")
    call_kwargs = MockClient.call_args.kwargs
    assert "User-Agent" in call_kwargs["headers"]
    ua = call_kwargs["headers"]["User-Agent"]
    assert "Mozilla" in ua


# ---------------------------------------------------------------------------
# Content-type rejection
# ---------------------------------------------------------------------------

def test_fetch_html_raises_on_json_content_type():
    with patch("app.scrape.httpx.Client") as MockClient:
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.return_value = _make_response(
            content_type="application/json",
            text='{"key": "value"}',
        )
        with pytest.raises(ScrapeError, match="text/html"):
            fetch_html("https://example.com/api")


def test_fetch_html_raises_on_missing_content_type():
    with patch("app.scrape.httpx.Client") as MockClient:
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.return_value = _make_response(content_type="")
        with pytest.raises(ScrapeError, match="text/html"):
            fetch_html("https://example.com/page")


# ---------------------------------------------------------------------------
# Empty body
# ---------------------------------------------------------------------------

def test_fetch_html_raises_on_empty_body():
    with patch("app.scrape.httpx.Client") as MockClient:
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.return_value = _make_response(text="   ")
        with pytest.raises(ScrapeError, match="Empty response"):
            fetch_html("https://example.com/page")


# ---------------------------------------------------------------------------
# Size limit
# ---------------------------------------------------------------------------

def test_fetch_html_raises_when_response_too_large():
    big_body = "x" * (_MAX_RESPONSE_BYTES + 1)
    with patch("app.scrape.httpx.Client") as MockClient:
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.return_value = _make_response(text=big_body)
        with pytest.raises(ScrapeError, match="too large"):
            fetch_html("https://example.com/page")


# ---------------------------------------------------------------------------
# HTTP error → retry → final failure
# ---------------------------------------------------------------------------

def test_fetch_html_raises_after_max_retries_on_network_error():
    with patch("app.scrape.httpx.Client") as MockClient, \
         patch("app.utils.time.sleep"):  # skip actual waits in tests
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.side_effect = httpx.ConnectError("connection refused")
        with pytest.raises(ScrapeError, match="after 3 attempts"):
            fetch_html("https://example.com/page", max_attempts=3)


def test_fetch_html_retries_then_succeeds():
    """Succeeds on the 3rd attempt after two network errors."""
    good_response = _make_response(text="<html>ok</html>")
    with patch("app.scrape.httpx.Client") as MockClient, \
         patch("app.utils.time.sleep"):
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.side_effect = [
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            good_response,
        ]
        result = fetch_html("https://example.com/page", max_attempts=3)
    assert "ok" in result


def test_fetch_html_raises_on_http_4xx():
    with patch("app.scrape.httpx.Client") as MockClient, \
         patch("app.utils.time.sleep"):
        mock_client = MockClient.return_value.__enter__.return_value
        # httpx raises HTTPStatusError on raise_for_status() for 4xx
        bad_resp = httpx.Response(404, content=b"Not Found")
        mock_client.get.return_value = bad_resp
        # 404 causes raise_for_status() to throw, which triggers retry logic
        with pytest.raises(ScrapeError):
            fetch_html("https://example.com/page", max_attempts=1)


# ---------------------------------------------------------------------------
# ScrapeError not retried (our own validation errors)
# ---------------------------------------------------------------------------

def test_fetch_html_does_not_retry_scrape_error():
    """A ScrapeError raised during validation must not be retried."""
    with patch("app.scrape.httpx.Client") as MockClient, \
         patch("app.utils.time.sleep") as mock_sleep:
        mock_client = MockClient.return_value.__enter__.return_value
        mock_client.get.return_value = _make_response(content_type="text/plain")
        with pytest.raises(ScrapeError):
            fetch_html("https://example.com/page", max_attempts=3)
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# SelectorMissingError is a subclass of ScrapeError
# ---------------------------------------------------------------------------

def test_selector_missing_error_is_scrape_error():
    exc = SelectorMissingError("h1.title not found")
    assert isinstance(exc, ScrapeError)
    assert "h1.title" in str(exc)
