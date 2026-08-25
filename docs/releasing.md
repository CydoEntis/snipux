# Releasing snipux to PyPI

Publishing needs a PyPI account with ownership of the `snipux` project name
and an API token for it — only the account owner has that token, and it must
never be committed anywhere in this repository. This page assumes both
already exist; it records the two commands that turn a checkout into a
published release, nothing else.

## 1. Build clean artifacts

```sh
rm -rf dist build *.egg-info
python -m build
```

`build` reads `[project]` in `pyproject.toml` and produces both a wheel and
an sdist under `dist/`. The `rm -rf` first matters: `python -m build` does
not prune stale files from a previous version out of `dist/`, so skipping it
risks uploading an old artifact alongside the new one.

Before uploading, sanity-check what got built:

```sh
twine check dist/*
```

`twine check` catches the two things PyPI itself rejects at upload time — a
`long_description` that fails to render, and metadata missing fields PyPI
requires. Fix and rebuild rather than uploading and finding out from a failed
upload.

## 2. Upload

```sh
twine upload dist/*
```

`twine` prompts for credentials; use `__token__` as the username and the
PyPI API token as the password. To skip the prompt, export
`TWINE_USERNAME=__token__` and `TWINE_PASSWORD=<token>` in the shell before
running the command — never write the token itself into a file in this
repository.

## Before either command

Bump `version` in `pyproject.toml` (and `__version__` in
`snipux/__init__.py`, which is not read from it). PyPI refuses to accept a
second upload of a version number that has already been published, even if
the previous upload was later deleted.
