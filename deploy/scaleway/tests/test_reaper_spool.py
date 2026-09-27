import base64
import hashlib

from documenso_scw import reaper


def test_spool_hashes_and_rewinds_the_stream():
    chunks = [b"%PDF-1.7\n", b"x" * 3_000_000, b"%%EOF"]
    data = b"".join(chunks)

    spooled = reaper.spool(iter(chunks))

    assert spooled.size == len(data)
    assert spooled.sha256 == hashlib.sha256(data).hexdigest()
    assert spooled.md5_base64 == base64.b64encode(hashlib.md5(data).digest()).decode()
    assert spooled.file.read() == data
    spooled.file.close()


def test_spool_of_an_empty_stream():
    spooled = reaper.spool(iter([]))

    assert spooled.size == 0
    assert spooled.sha256 == hashlib.sha256(b"").hexdigest()
    spooled.file.close()


def test_read_chunks_splits_by_chunk_size():
    import io

    stream = io.BytesIO(b"a" * (reaper.CHUNK * 2 + 5))

    sizes = [len(chunk) for chunk in reaper._read_chunks(stream)]

    assert sizes == [reaper.CHUNK, reaper.CHUNK, 5]
