"""Canonical lookup keys for portable, case-insensitive managed paths."""
import unicodedata
import hashlib


def normalized_object_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\\", "/")).casefold()


def object_key_default(context):
    return object_key_lookup_key(context.get_current_parameters()["object_key"])


def object_key_lookup_key(value: str) -> str:
    # Unicode casefold can expand an old legal path beyond the original VARCHAR
    # budget or PostgreSQL's B-tree entry size. Index a bounded canonical digest.
    return hashlib.sha256(normalized_object_key(value).encode("utf-8")).hexdigest()
