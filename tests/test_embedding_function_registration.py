"""Regression test: custom embedding functions must be registered with ChromaDB.

ChromaDB >=1.x reconstructs a collection's embedding function *by name* from the
persisted config when it reopens a collection (e.g. the ``collection.configuration``
property used inside the upsert path). It looks the name up in
``chromadb.utils.embedding_functions.known_embedding_functions`` and calls that
class's ``build_from_config``.

Our custom embedding functions report names that either collide with ChromaDB's
own built-ins (``"openai"``, ``"huggingface"``) or are absent from the registry
(``"gemini"``). If they are not registered, ChromaDB resolves ``"openai"`` to its
*built-in* OpenAIEmbeddingFunction, whose ``build_from_config`` expects an
``api_key_env_var`` key and asserts on our ``{model_name, base_url}`` config:

    Could not build embedding function openai from config
    {'base_url': None, 'model_name': 'text-embedding-3-small'}:
    This code should not be reached

which surfaced as "19 errors" on every ``update-db`` against an existing index.

Fix: decorate the custom classes with ``@register_embedding_function`` so the
registry maps their names to *our* classes (and our compatible build_from_config).
"""

import pytest

# chromadb is an optional extra (``[semantic]``); skip where it isn't installed.
chromadb = pytest.importorskip("chromadb")  # noqa: F841

from chromadb.utils.embedding_functions import (  # noqa: E402
    known_embedding_functions,
)

from zotero_mcp import chroma_client  # noqa: E402


@pytest.mark.parametrize(
    "name, cls_attr",
    [
        ("openai", "OpenAIEmbeddingFunction"),
        ("gemini", "GeminiEmbeddingFunction"),
        ("huggingface", "HuggingFaceEmbeddingFunction"),
        ("ollama", "OllamaEmbeddingFunction"),
    ],
)
def test_custom_embedding_functions_are_registered(name, cls_attr):
    """Importing chroma_client must register our EFs under their names.

    Without this, ``known_embedding_functions["openai"]`` resolves to ChromaDB's
    incompatible built-in and breaks reload/upsert of an existing collection.
    """
    assert name in known_embedding_functions, (
        f"{name!r} not registered; ChromaDB cannot rebuild the embedding function from a persisted collection's config."
    )
    assert known_embedding_functions[name] is getattr(chroma_client, cls_attr)


def test_openai_build_from_config_handles_persisted_config(monkeypatch):
    """The exact operation that failed for existing indexes must now succeed.

    ChromaDB stores our OpenAI EF config as ``{"model_name": ..., "base_url": ...}``
    (see ``OpenAIEmbeddingFunction.get_config``). Rebuilding from that config via
    the registry previously hit ChromaDB's built-in and raised
    "This code should not be reached".
    """
    pytest.importorskip("openai")  # __init__ constructs an openai.OpenAI client
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-no-network")

    persisted = {"model_name": "text-embedding-3-small", "base_url": None}
    ef = known_embedding_functions["openai"].build_from_config(persisted)

    assert isinstance(ef, chroma_client.OpenAIEmbeddingFunction)
    # Configs persisted before request_batch_size/rate_limit_rps existed must
    # still rebuild, falling back to defaults for the new fields.
    cfg = ef.get_config()
    assert cfg["model_name"] == "text-embedding-3-small"
    assert cfg["base_url"] is None
    assert cfg["request_batch_size"] == chroma_client.OpenAIEmbeddingFunction.DEFAULT_REQUEST_BATCH_SIZE
    assert cfg["rate_limit_rps"] is None
    # Configs persisted before the dimensions field existed must rebuild with
    # dimensions=None (model default), not raise.
    assert cfg["dimensions"] is None


def test_openai_build_from_config_preserves_dimensions(monkeypatch):
    """A persisted config carrying dimensions must round-trip through build_from_config."""
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-no-network")

    persisted = {"model_name": "text-embedding-3-large", "base_url": None, "dimensions": 1024}
    ef = known_embedding_functions["openai"].build_from_config(persisted)

    assert isinstance(ef, chroma_client.OpenAIEmbeddingFunction)
    assert ef.dimensions == 1024
    cfg = ef.get_config()
    assert cfg["dimensions"] == 1024


def test_dimension_probe_triggers_reset_on_dimension_change(tmp_path, monkeypatch):
    """Regression: switching dimensions (e.g. 3072→1024) must auto-reset the
    collection on ChromaClient init, NOT silently leave the old-dim collection
    in place (which causes a hard InvalidArgumentError on the next upsert).

    Root cause was numpy-array truthiness in ``stored_embs = probe.get(...) or []``
    raising ValueError inside a bare ``except Exception`` that swallowed it.
    """
    pytest.importorskip("chromadb")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-no-network")

    from zotero_mcp.chroma_client import ChromaClient, OpenAIEmbeddingFunction

    persist = str(tmp_path / "chroma")
    coll_name = "test_dim_probe_coll"

    # Mock __call__ to return controlled-dimension vectors (no network).
    def fake_call(self, input):
        dims = getattr(self, "dimensions", None) or 3072
        return [[0.0] * dims for _ in input]

    monkeypatch.setattr(OpenAIEmbeddingFunction, "__call__", fake_call)

    # Step 1: build with default 3072 dims, write one vector.
    c1 = ChromaClient(
        embedding_model="openai",
        embedding_config={"model_name": "text-embedding-3-large", "api_key": "test"},
        persist_directory=persist,
        collection_name=coll_name,
    )
    c1.add_documents(["test doc"], [{"has_fulltext": False}], ["d1"])
    assert c1.collection.count() == 1

    # Step 2: reinit with dimensions=1024, same model_name.
    c2 = ChromaClient(
        embedding_model="openai",
        embedding_config={"model_name": "text-embedding-3-large", "api_key": "test", "dimensions": 1024},
        persist_directory=persist,
        collection_name=coll_name,
    )
    # Probe should have detected 3072≠1024 and reset → count=0.
    assert c2.collection.count() == 0, "dimension probe failed to reset collection"

    # Step 3: writing a 1024-dim vector must now succeed (no dimension error).
    c2.add_documents(["test doc 2"], [{"has_fulltext": False}], ["d2"])
    assert c2.collection.count() == 1
