# Indexer Data Directory

This directory is mounted as `/data` inside the indexer Docker container.

## Purpose

- Used by the indexer service for file processing and incremental loading
- Contains the [BedtimeNews-Transcripts](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts) repository cloned from GitHub by the indexer service (at `BedtimeNews-Transcripts/`) and any other output files that may be created during the indexing process

## Git Ignore Rules

- All contents except this README file are ignored in git

## Run lock

`.indexer.lock` in this directory is the indexer's run lock: every run (the
scheduled one, a manual `python -m src.snapshots build`, or a second container
sharing this directory) holds an exclusive `flock` on it for its whole
duration, so only one run touches the git checkout and the database at a time.
Do not delete or create it by hand; a stale file is harmless (the lock is
released when the holding process exits).

## Note

Do not manually add or modify files to this directory - they will be managed by the indexer service.
