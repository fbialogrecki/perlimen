from io import BytesIO
from pathlib import Path

import pytest
from perlimen_lite.media_storage import LocalMediaStorage

ASSET = "10000000-0000-4000-8000-000000000001"
KEY = f"assets/{ASSET}/post_480.webp"


def test_storage_reopens_a_private_blob_and_deletes_idempotently(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    root.mkdir(mode=0o700)
    LocalMediaStorage(root).put_object(
        bucket="lite-local", key=KEY, body=BytesIO(b"fixture"), content_type="image/webp"
    )
    storage = LocalMediaStorage(root)
    assert storage.ready()
    assert storage.get_object(bucket="lite-local", key=KEY) == (b"fixture", "image/webp")
    [blob] = root.iterdir()
    assert blob.stat().st_mode & 0o777 == 0o600
    for _ in range(2):
        storage.delete_object(bucket="lite-local", key=KEY)
    assert not list(root.iterdir())
    with pytest.raises(KeyError):
        storage.get_object(bucket="lite-local", key=KEY)


def test_storage_does_not_follow_links_or_use_a_nonprivate_root(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    root.mkdir(mode=0o700)
    target = tmp_path / "outside"
    target.write_bytes(b"outside fixture")
    blob = root / f"{ASSET}.post_480.webp"
    blob.symlink_to(target)
    storage = LocalMediaStorage(root)
    with pytest.raises(OSError):
        storage.get_object(bucket="lite-local", key=KEY)
    with pytest.raises(FileExistsError):
        storage.put_object(bucket="lite-local", key=KEY, body=b"new", content_type="image/webp")
    with pytest.raises(KeyError):
        storage.delete_object(bucket="lite-local", key=f"assets/{ASSET}/../original")
    storage.delete_object(bucket="lite-local", key=KEY)
    assert not blob.is_symlink() and target.read_bytes() == b"outside fixture"
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert not LocalMediaStorage(alias).ready()
    root.chmod(0o755)
    assert not storage.ready()
    with pytest.raises(PermissionError):
        storage.put_object(bucket="lite-local", key=KEY, body=b"new", content_type="image/webp")
