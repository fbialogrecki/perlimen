import pytest
from perlimen_lite.database import endpoint


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://127.0.0.1:5432/legacy",
        "postgresql://postgres.default:5432/perlimen_lite",
        "postgresql://root@127.0.0.1:5432/perlimen_lite",
        "postgresql://127.0.0.1:5432/perlimen_lite?host=legacy",
        "sqlite:///perlimen_lite",
    ],
)
def test_reject_non_lite_endpoint(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("PERLIMEN_LITE_DATABASE_URL", url)
    with pytest.raises(ValueError, match="without credentials"):
        endpoint()
