"""File and directory paths (inside container view)."""

import os
from pathlib import Path

# Bind-mounted indexer data directory (INDEXER_DATA_DIR on the host).
DATA_DIR = Path(os.environ.get("INDEXER_DATA_PATH", "/data"))

# Local clone of the upstream transcript repository
# (https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts).
BEDTIMENEWS_TRANSCRIPTS_DIR = DATA_DIR / "BedtimeNews-Transcripts"

# Run lock held for the whole of every indexer run; never create or delete it
# by hand.
LOCK_FILE = DATA_DIR / ".indexer.lock"

# Transcripts live at contents/<栏目>/<百期文件夹>/<期号>.md. A document's URI —
# the identifier used as doc_id throughout the system — is its path relative to
# this directory, including the .md suffix (e.g. "ShuiQianXiaoXi/0501-0600/0588.md").
CONTENTS_DIR = BEDTIMENEWS_TRANSCRIPTS_DIR / "contents"

# Authoritative URI -> 标准化标题 table maintained upstream.
URI_MAPPING_FILE = BEDTIMENEWS_TRANSCRIPTS_DIR / "URI映射.md"

INDEX_CONFIG_FILE = Path(os.environ.get("INDEX_CONFIG_FILE", "/app/index_config.yml"))
