"""Commons metadata and geocoding contracts independent of quiz state."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from msu_hub_bot.providers import geoguess as source
from msu_hub_bot.providers.exceptions import ExternalServiceError
from quiz_helpers import PHOTO


def sample():
    return {
        "query": {
            "pages": {
                "1": {
                    "pageid": 1,
                    "imageinfo": [
                        {
                            "mime": "image/jpeg",
                            "width": 1000,
                            "height": 800,
                            "url": PHOTO.url,
                            "extmetadata": {
                                k: {"value": v}
                                for k, v in {
                                    "GPSLatitude": "60.397",
                                    "GPSLongitude": "5.325",
                                    "Artist": '<a href="https://example.com">Alice &amp; Bob</a>',
                                    "LicenseShortName": "CC BY 3.0",
                                    "LicenseUrl": PHOTO.license_url,
                                }.items()
                            },
                        }
                    ],
                }
            }
        }
    }


def test_geography_author_license():
    result = source.candidates(sample())
    assert len(result) == 1
    assert result[0].photo.author == "Alice & Bob"
    assert result[0].latitude == 60.397
    assert result[0].longitude == 5.325
    assert result[0].photo.country == ""


@pytest.mark.parametrize("field,value", [("mime", "image/svg+xml"), ("width", 40), ("url", "https://evil.example/x")])
def test_unsuitable_media_filtered(field, value):
    data = sample()
    data["query"]["pages"]["1"]["imageinfo"][0][field] = value
    assert source.candidates(data) == []


def test_invalid_location_filtered():
    data = sample()
    data["query"]["pages"]["1"]["imageinfo"][0]["extmetadata"]["GPSLatitude"]["value"] = "nan"
    assert source.candidates(data) == []


@pytest.mark.parametrize("field", ["LicenseUrl", "Artist", "GPSLatitude"])
@pytest.mark.parametrize("value", [None, 12, {"unexpected": "value"}])
def test_malformed_metadata_does_not_discard_valid_later_photo(field, value):
    import copy

    data = sample()
    pages = data["query"]["pages"]
    pages["2"] = copy.deepcopy(pages["1"])
    pages["2"]["pageid"] = 2
    pages["1"]["imageinfo"][0]["extmetadata"][field]["value"] = value
    result = source.candidates(data)
    assert len(result) == 1
    assert result[0].photo.source.endswith("curid=2")


@pytest.mark.parametrize("data", [None, {"query": None}, {"query": {"pages": []}}])
def test_malformed_photo_response_has_no_candidates(data):
    assert source.candidates(data) == []


@pytest.mark.parametrize(
    "width,height,accepted",
    [(960, 768, True), (5000, 5000, True), (5001, 5000, False), (400, 8000, True), (400, 8001, False), (None, 768, False)],
)
def test_selected_thumbnail_respects_telegram_dimensions(width, height, accepted):
    data = sample()
    info = data["query"]["pages"]["1"]["imageinfo"][0]
    info.update(width=6000, height=6000, thumburl="https://upload.wikimedia.org/thumb.jpg", thumbwidth=width, thumbheight=height)
    result = source.candidates(data)
    assert bool(result) is accepted
    if accepted:
        assert result[0].photo.url == info["thumburl"]


@pytest.mark.parametrize("original_width,original_height,accepted", [(1000, 800, True), (6000, 6000, False), (600, 12600, False)])
def test_missing_thumbnail_dimensions_fall_back_to_original(original_width, original_height, accepted):
    data = sample()
    info = data["query"]["pages"]["1"]["imageinfo"][0]
    info.update(width=original_width, height=original_height, thumburl="https://upload.wikimedia.org/thumb.jpg")
    assert bool(source.candidates(data)) is accepted


@pytest.mark.parametrize("latitude,longitude", [(0, 0), (-33.9, 18.4), (27.7, 85.3), (-17.7, 178.4)])
def test_coordinates_worldwide_are_not_restricted(latitude, longitude):
    data = sample()
    metadata = data["query"]["pages"]["1"]["imageinfo"][0]["extmetadata"]
    metadata["GPSLatitude"]["value"] = str(latitude)
    metadata["GPSLongitude"]["value"] = str(longitude)
    assert len(source.candidates(data)) == 1


def test_country_uses_code_not_untrusted_display_name():
    assert source.location({"address": {"country_code": "np", "country": "Nepal", "city": "Катманду"}}) == ("Непал", "Катманду")
    assert len(source.COUNTRIES) > 200


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"error": "Unable to geocode"},
        {"address": None},
        {"address": []},
        {"address": {"country": "Atlantis", "country_code": "zz"}},
        {"address": {"country": "Nepal", "country_code": None}},
        {"address": {"country": 123, "country_code": "np"}},
        {"address": {"country": "Nepal", "country_code": "np", "city": 123}},
    ],
)
def test_unknown_country_rejected(data):
    with pytest.raises(source.UnknownLocation):
        source.location(data)


def test_unknown_photo_skipped_before_delivery(monkeypatch):
    import copy

    data = sample()
    second = copy.deepcopy(data["query"]["pages"]["1"])
    second["pageid"] = 2
    data["query"]["pages"]["2"] = second
    monkeypatch.setattr(source.random, "shuffle", lambda photos: None)
    fetch = AsyncMock(return_value=data)
    reverse = AsyncMock(side_effect=[source.UnknownLocation("unknown"), ("Непал", "Катманду")])
    monkeypatch.setattr(source, "request_json", fetch)
    monkeypatch.setattr(source, "reverse_location", reverse)
    photo = asyncio.run(source.random_photo())
    assert photo.country == "Непал" and photo.source.endswith("curid=2")
    params = fetch.call_args.args[2]
    assert params["generator"] == "random" and params["grnnamespace"] == 6
    assert "gsrsearch" not in params and "ggscoord" not in params
    assert reverse.await_count == 2


def test_malformed_geocoder_location_skips_to_next_photo(monkeypatch):
    import copy

    data = sample()
    second = copy.deepcopy(data["query"]["pages"]["1"])
    second["pageid"] = 2
    data["query"]["pages"]["2"] = second
    monkeypatch.setattr(source.random, "shuffle", lambda photos: None)
    monkeypatch.setattr(source, "_geocoder_lock", None)
    monkeypatch.setattr(source, "_geocoder_next", 0)
    monkeypatch.setattr(source, "_geocoder_cache", {})
    fetch = AsyncMock(side_effect=[data, {"address": None}, {"address": {"country": "Nepal", "country_code": "np"}}])
    monkeypatch.setattr(source, "request_json", fetch)
    photo = asyncio.run(source.random_photo())
    assert photo.country == "Непал" and photo.source.endswith("curid=2")
    assert fetch.await_count == 3


def test_no_country_means_no_photo(monkeypatch):
    monkeypatch.setattr(source, "request_json", AsyncMock(return_value=sample()))
    monkeypatch.setattr(source, "reverse_location", AsyncMock(side_effect=source.UnknownLocation("unknown")))
    with pytest.raises(ExternalServiceError):
        asyncio.run(source.random_photo())


def test_geocoder_cache_and_throttle(monkeypatch):
    monkeypatch.setattr(source, "_geocoder_lock", None)
    monkeypatch.setattr(source, "_geocoder_next", 0)
    monkeypatch.setattr(source, "_geocoder_cache", {})
    request = AsyncMock(return_value={"address": {"country_code": "np", "country": "Nepal"}})
    monkeypatch.setattr(source, "request_json", request)
    starts = []

    async def record(*args):
        starts.append(source.time.monotonic())
        return {"address": {"country_code": "np", "country": "Nepal"}}

    request.side_effect = record

    async def scenario():
        await asyncio.gather(source.reverse_location(None, 27, 85), source.reverse_location(None, 28, 85))
        await source.reverse_location(None, 27, 85)

    asyncio.run(scenario())
    assert request.await_count == 2
    assert starts[1] - starts[0] >= 1
