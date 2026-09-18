"""File and directory paths (inside container view)."""

import os
from pathlib import Path

# Local clone of the upstream transcript repository
# (https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts).
BEDTIMENEWS_TRANSCRIPTS_DIR = Path("/data/BedtimeNews-Transcripts")

# Transcripts live at contents/<栏目>/<百期文件夹>/<期号>.md. A document's URI —
# the identifier used as doc_id throughout the system — is its path relative to
# this directory, including the .md suffix (e.g. "ShuiQianXiaoXi/0501-0600/0588.md").
CONTENTS_DIR = BEDTIMENEWS_TRANSCRIPTS_DIR / "contents"

# Authoritative URI -> 标准化标题 table maintained upstream.
URI_MAPPING_FILE = BEDTIMENEWS_TRANSCRIPTS_DIR / "URI映射.md"

INDEX_CONFIG_FILE = Path(os.environ.get("INDEX_CONFIG_FILE", "/app/index_config.yml"))
