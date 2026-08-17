"""Short-lived read tokens for Azure blob sources that refuse anonymous reads.

NASA's HLS collection lives on an Azure storage account with public access
turned off: every request, GDAL's included, comes back
``409 Public access is not permitted on this storage account``. Microsoft
Planetary Computer hands out read-only SAS tokens for exactly these containers,
so the viewer can sign the URL itself instead of showing the user a 409.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blob_tokens import TokenStore, container_of, is_signed, refused_access  # noqa: E402


HLS = (
    "https://hls2euwest.blob.core.windows.net/hls2/L30/43/P/GS/2026/08/14/"
    "HLS.L30.T43PGS.2026226T051556.v2.0/HLS.L30.T43PGS.2026226T051556.v2.0.B10.tif"
)


def test_an_azure_blob_url_names_its_account_and_container():
    assert container_of(HLS) == ("hls2euwest", "hls2")


def test_other_hosts_are_not_azure_blobs():
    assert container_of("https://maxar-opendata.s3.amazonaws.com/events/x/y.tif") is None
    assert container_of("https://example.com/hls2/a.tif") is None
    assert container_of("C:/Users/Admin/Downloads/a.tif") is None


def test_a_blob_url_with_no_container_is_not_signable():
    assert container_of("https://hls2euwest.blob.core.windows.net/") is None


def test_a_url_that_already_carries_a_signature_is_left_alone():
    assert is_signed(HLS + "?st=2026-08-16&sig=abc%3D")
    assert not is_signed(HLS)
    assert not is_signed(HLS + "?foo=bar")


@pytest.mark.parametrize(
    "message",
    [
        "RasterioIOError: HTTP response code: 409",
        "HTTP response code: 403",
        "HTTP response code: 401",
        "Public access is not permitted on this storage account",
    ],
)
def test_an_access_refusal_is_recognised(message):
    assert refused_access(message)


@pytest.mark.parametrize(
    "message", ["HTTP response code: 404", "HTTP response code: 500", "not a COG"]
)
def test_an_ordinary_failure_is_not_mistaken_for_a_refusal(message):
    # A missing file must not send us off to fetch a token and retry: the retry
    # would fail identically and cost the user a second wait.
    assert not refused_access(message)


def in_an_hour() -> str:
    """The endpoint reports wall-clock expiry, so tests have to as well."""
    stamp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


class Fetches:
    """A stand-in for the token endpoint that counts calls."""

    def __init__(self, expiry=None):
        self.calls: list[tuple[str, str]] = []
        self.expiry = in_an_hour() if expiry is None else expiry

    def __call__(self, account, container):
        self.calls.append((account, container))
        return f"sv=2025-07-05&sig=tok{len(self.calls)}", self.expiry


def test_signing_appends_the_token_as_a_query_string():
    store = TokenStore(fetch=Fetches(), now=lambda: 0.0)
    assert store.sign(HLS) == HLS + "?sv=2025-07-05&sig=tok1"


def test_signing_keeps_any_query_the_url_already_had():
    store = TokenStore(fetch=Fetches(), now=lambda: 0.0)
    assert store.sign(HLS + "?a=1") == HLS + "?a=1&sv=2025-07-05&sig=tok1"


def test_one_token_serves_every_object_in_the_container():
    # A SAS token is issued per container, so fetching one per tile would be
    # both slower and pointless.
    fetch = Fetches()
    store = TokenStore(fetch=fetch, now=lambda: 0.0)
    store.sign(HLS)
    store.sign(HLS.replace("B10.tif", "B11.tif"))
    assert fetch.calls == [("hls2euwest", "hls2")]


def test_a_token_is_refetched_once_it_is_close_to_expiring():
    fetch = Fetches()
    clock = [0.0]
    store = TokenStore(fetch=fetch, now=lambda: clock[0])
    assert store.sign(HLS).endswith("tok1")
    clock[0] = 3000.0  # 50 min in, still 10 min of validity left
    assert store.sign(HLS).endswith("tok1")
    clock[0] = 3500.0  # inside the refresh margin
    assert store.sign(HLS).endswith("tok2")


def test_an_unreadable_expiry_still_yields_a_usable_lifetime():
    fetch = Fetches(expiry="whenever")
    clock = [0.0]
    store = TokenStore(fetch=fetch, now=lambda: clock[0])
    assert store.sign(HLS).endswith("tok1")
    clock[0] = 60.0
    assert store.sign(HLS).endswith("tok1")


def test_nothing_is_signed_until_the_source_has_actually_refused_us():
    # Plenty of Azure containers are public. Signing them anyway would add a
    # round trip to the token endpoint before every one of those opens.
    store = TokenStore(fetch=Fetches(), now=lambda: 0.0)
    assert store.signed_if_needed(HLS) == HLS


def test_a_refusal_teaches_the_store_to_sign_that_container():
    fetch = Fetches()
    store = TokenStore(fetch=fetch, now=lambda: 0.0)
    assert store.learn(HLS, "HTTP response code: 409") is True
    assert store.signed_if_needed(HLS) == HLS + "?sv=2025-07-05&sig=tok1"
    # Tiles come later and must not each pay for the lesson again.
    assert fetch.calls == [("hls2euwest", "hls2")]


def test_a_signed_source_can_be_reported_as_such():
    # The user is told a token was fetched on their behalf, so the descriptor
    # needs to be able to ask.
    store = TokenStore(fetch=Fetches(), now=lambda: 0.0)
    assert not store.needs_signing(HLS)
    store.learn(HLS, "HTTP response code: 409")
    assert store.needs_signing(HLS)
    assert not store.needs_signing("https://maxar-opendata.s3.amazonaws.com/a.tif")


def test_learning_covers_the_rest_of_the_container():
    store = TokenStore(fetch=Fetches(), now=lambda: 0.0)
    store.learn(HLS, "HTTP response code: 409")
    sibling = HLS.replace("B10.tif", "B04.tif")
    assert store.signed_if_needed(sibling).startswith(sibling + "?")


def test_there_is_nothing_to_learn_from_a_non_azure_refusal():
    store = TokenStore(fetch=Fetches(), now=lambda: 0.0)
    assert store.learn("https://example.com/a.tif", "HTTP response code: 403") is False


def test_there_is_nothing_to_learn_from_an_ordinary_error():
    store = TokenStore(fetch=Fetches(), now=lambda: 0.0)
    assert store.learn(HLS, "HTTP response code: 404") is False


def test_an_already_signed_url_teaches_nothing():
    # Otherwise a genuinely forbidden object would loop: sign, fail, sign again.
    store = TokenStore(fetch=Fetches(), now=lambda: 0.0)
    assert store.learn(HLS + "?sig=abc", "HTTP response code: 403") is False


def test_a_token_endpoint_that_is_down_does_not_become_our_error():
    def broken(account, container):
        raise OSError("token endpoint unreachable")

    store = TokenStore(fetch=broken, now=lambda: 0.0)
    assert store.learn(HLS, "HTTP response code: 409") is False
    assert store.signed_if_needed(HLS) == HLS
