from types import SimpleNamespace

from src.image_utils import _is_safe_host, extract_image_url


def test_extract_image_url() -> None:
    message = [
        SimpleNamespace(type="text", data={"text": "看图"}),
        SimpleNamespace(type="image", data={"url": "https://example.com/a.jpg"}),
    ]
    assert extract_image_url(message) == "https://example.com/a.jpg"


def test_extract_image_url_ignores_local_file() -> None:
    message = [SimpleNamespace(type="image", data={"file": "C:/tmp/a.jpg"})]
    assert extract_image_url(message) is None


def test_safe_host_blocks_private_ranges() -> None:
    assert _is_safe_host("example.com")
    assert _is_safe_host("8.8.8.8")
    assert not _is_safe_host("localhost")
    assert not _is_safe_host("127.0.0.1")
    assert not _is_safe_host("10.0.0.1")
    assert not _is_safe_host("192.168.1.1")
    assert _is_safe_host("127.0.0.1", allow_private=True)
