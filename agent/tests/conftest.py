"""Test environment for the agent package.

`src.graph` builds chat models and `src.retriever` builds embeddings at import
time, so endpoint values must exist before any `src.*` module is imported.
These overrides run with no real credentials, no network and no database;
real environment variables keep precedence over config.yml, and a stray
config.yml in the working tree is pointed away via CONFIG_YAML.
"""

import os

os.environ.setdefault("CONFIG_YAML", "/nonexistent/config.yml")
os.environ.setdefault("GENERATION__API_KEY", "test-key-not-used")
os.environ.setdefault("GENERATION__MODEL", "test-generation-model")
os.environ.setdefault("GENERATION__FAST_MODEL", "test-fast-model")
os.environ.setdefault("EMBEDDING__API_KEY", "test-key-not-used")
os.environ.setdefault("EMBEDDING__MODEL", "test-embedding-model")
