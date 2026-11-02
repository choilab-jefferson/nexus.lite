# Contributing

Issues are welcome. Pull requests are not merged on GitHub: development happens in a
private GitLab mirror, and this repository receives one squashed commit per release.

## Reporting a bug

1. Check the open and closed [issues](https://github.com/choilab-jefferson/nexus.lite/issues).
2. Open a new issue with the *Bug report* template: version, Python and OS, the smallest
   example that reproduces it, and the traceback.
3. Remove API keys, tokens, private paths and any patient data from what you paste.

## Feature requests and questions

Open an issue and describe the use case. A patch is welcome as a diff attached to the issue; a
maintainer may port it to the private repository, with credit.

## Security problems

Do not file a public issue: see [SECURITY.md](SECURITY.md).

## Running the tests

    pip install -e '.[dev,demos]' pytest-timeout
    python -m pytest -q

Some tests need `dtach`, `pgrep` and `pkill`.

## License

By contributing an idea or patch you agree it may be used under the project's MIT license.
