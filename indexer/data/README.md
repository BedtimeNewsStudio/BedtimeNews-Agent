# Indexer Data Directory

This directory is mounted as `/data` inside the indexer Docker container.

## Purpose

- Used by the indexer service for file processing and incremental loading
- Contains the [BedtimeNews-Transcripts](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts) repository cloned from GitHub by the indexer service (at `BedtimeNews-Transcripts/`) and any other output files that may be created during the indexing process

## Git Ignore Rules

- All contents except this README file are ignored in git

## Note

Do not manually add or modify files to this directory - they will be managed by the indexer service.
