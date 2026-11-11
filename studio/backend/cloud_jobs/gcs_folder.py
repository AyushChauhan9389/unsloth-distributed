"""A job folder in a GCS bucket (gs://bucket/jobs/<id>), read and written by the central server.
Cloud GPUs see the same prefix through a SkyPilot bucket mount, so the runner only does file I/O."""
_client = None


def client():
    global _client
    if _client is None:
        from google.cloud import storage  # optional dep: only needed when a gs:// bucket is configured
        _client = storage.Client()
    return _client


def split_uri(uri: str) -> tuple[str, str]:
    bucket, _, prefix = uri.removeprefix("gs://").partition("/")
    return bucket, prefix.strip("/")


class GcsFolder:
    def __init__(self, uri: str):
        self.uri = uri.rstrip("/")
        name, self.prefix = split_uri(self.uri)
        self.bucket = client().bucket(name)

    def _blob(self, name: str):
        return self.bucket.blob(f"{self.prefix}/{name}")

    def read_text(self, name: str) -> str:
        return self._blob(name).download_as_text()

    def write_text(self, name: str, text: str) -> None:
        self._blob(name).upload_from_string(text, content_type = "application/json")

    def read_bytes(self, name: str) -> bytes:
        return self._blob(name).download_as_bytes()

    def write_bytes(self, name: str, data: bytes) -> None:
        self._blob(name).upload_from_string(data)

    def put_file(self, name: str, src) -> None:
        self._blob(name).upload_from_filename(str(src))

    def exists(self, name: str) -> bool:
        return self._blob(name).exists()

    def list(self, sub: str) -> list[str]:
        base = f"{self.prefix}/{sub.strip('/')}/"
        names = (b.name[len(base):] for b in client().list_blobs(self.bucket, prefix = base))
        return sorted(n for n in names if n and "/" not in n and not n.endswith(".tmp"))
