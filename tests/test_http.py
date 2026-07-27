from unittest.mock import AsyncMock, MagicMock

import pytest
from curl_cffi.curl import CurlError

# Import your function
from bunkr_api.utils.http import execute_request_with_retry_async


@pytest.mark.asyncio
async def test_retry_success_on_second_attempt():
    """Verify that a failing initial attempt retries and returns response on success."""
    mock_session = MagicMock()

    # Fail once with CurlError, then succeed on second attempt
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None

    mock_session.get = AsyncMock(
        side_effect=[CurlError("Network timeout", 28), mock_response]
    )

    res = await execute_request_with_retry_async(
        mock_session,
        "https://example.com/api",
        retries=2,
        delay=0.01,
    )

    assert res == mock_response
    assert mock_session.get.call_count == 2


@pytest.mark.asyncio
async def test_retry_raises_after_max_retries():
    """Verify that CurlError is raised after exhausting all retry attempts."""
    mock_session = MagicMock()
    mock_session.get = AsyncMock(side_effect=CurlError("Connection refused", 7))

    with pytest.raises(CurlError):
        await execute_request_with_retry_async(
            mock_session,
            "https://example.com/api",
            retries=3,
            delay=0.01,
        )

    assert mock_session.get.call_count == 3