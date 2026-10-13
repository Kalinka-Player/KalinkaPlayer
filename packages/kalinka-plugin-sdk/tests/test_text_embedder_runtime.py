"""An embedder written before SDK 3.12 names no missing runtime: its server
installed the runtime with itself."""

from kalinka_plugin_sdk.embedding import TextEmbedder


class _OlderEmbedder(TextEmbedder):
    """Implements the protocol as it stood before 3.12."""

    model_id = "all-MiniLM-L6-v2"
    model_version = 1
    dim = 384

    async def available(self):
        return True

    async def embed(self, texts):
        return []


def test_the_default_names_nothing_missing():
    assert _OlderEmbedder().missing_runtime() == []
