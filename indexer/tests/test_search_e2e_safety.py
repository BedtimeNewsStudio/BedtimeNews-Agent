"""E2E scope guards must survive optimized Python, without touching Docker."""

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/test_keyword_search_e2e.py"


@pytest.mark.parametrize(
    "scope,dbname", [("full", "production_local"), ("sample", "production")]
)
def test_container_oracle_rejects_non_sample_database_even_with_optimization(
    scope, dbname
):
    code = runpy.run_path(str(SCRIPT))["ORACLE_CODE"]
    # Isolate the container preflight from real sources, databases and Docker.
    program = (
        f"scope={scope!r}\ndbname={dbname!r}\n"
        + """
import sys, types
modules = {
    'src': {},
    'src.settings': {'settings': types.SimpleNamespace(indexer_scope=scope, postgres_db=dbname)},
    'src.file_scanner': {'scan_files': lambda: []},
    'src.document_loader': {'load_indexable_source': lambda uri: None},
    'src.search_projection': {'project_search': lambda source: None},
}
for name, attributes in modules.items():
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
"""
        + f"\nexec({code!r})\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", program],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, result.stdout
    assert "sample" in result.stderr
    assert "_local" in result.stderr


def test_e2e_refuses_optimization_before_any_docker_operation():
    program = f"""
import runpy, subprocess, sys
def forbid_docker(*args, **kwargs):
    raise RuntimeError('Docker was called before checking optimization')
subprocess.check_output = forbid_docker
sys.argv = [{str(SCRIPT)!r}, '--lifecycle']
runpy.run_path({str(SCRIPT)!r}, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", program],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "without -O" in result.stderr, result.stderr


@pytest.mark.parametrize(
    "url",
    ["file://localhost/tmp/data", "ftp://localhost:21", "https://example.invalid"],
)
def test_e2e_rejects_nonlocal_or_non_http_urls_before_docker(url):
    program = f"""
import runpy, subprocess, sys
def forbid_docker(*args, **kwargs):
    raise RuntimeError('Docker was called before URL validation')
subprocess.check_output = forbid_docker
sys.argv = [{str(SCRIPT)!r}, '--url', {url!r}]
runpy.run_path({str(SCRIPT)!r}, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "local HTTP only" in result.stderr, result.stderr
