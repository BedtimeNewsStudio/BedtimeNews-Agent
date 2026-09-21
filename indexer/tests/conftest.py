"""Shared test fixtures/config for the indexer.

Some modules under test (e.g. change_detector -> vector_db -> settings) import
`settings` transitively, and `embeddings` builds its OpenAI client at import
time. Provide dummy endpoint values via environment overrides so those imports
succeed without a real config.yml; `setdefault` leaves any real value in place.
"""

import os

os.environ.setdefault("CONFIG_YAML", "/nonexistent/config.yml")
os.environ.setdefault("EMBEDDING__API_KEY", "test-key-not-used")
os.environ.setdefault("EMBEDDING__MODEL", "test-embedding-model")
