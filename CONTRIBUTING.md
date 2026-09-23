# Contributing

1. Fork the repository and create a feature branch.
2. Make changes with editable installs active so they take effect immediately.
3. Run `make test` (and any package-specific tests for the area you touched).
4. Open a pull request describing the change and which plugin or server area it affects.

A pull request runs [scripts/check_credential_logging.py](scripts/check_credential_logging.py), which flags code that could write a credential to a log — a credential-named value passed to a log call or a raised exception — as an annotation on the line. Log without the value; keep a false positive the review agrees on with `# log-safe: <why>` on the flagged call.

## AI-assisted development

Kalinka's server, renderer and bundled plugins are built with **substantial AI assistance**. The original core and architecture were developed manually. Much of the more recent implementation, refactoring and maintenance work has been produced with AI assistance, working from maintainer-defined requirements. Architecture, technical direction, review, testing and acceptance of changes remain the responsibility of the maintainer.

AI use is disclosed at the repository level rather than on individual commits, so commit messages and pull requests carry no co-authorship trailers or generated-with footers.

Changes are reviewed and tested to the same standard however they were produced. [CLAUDE.md](CLAUDE.md) records the conventions, including the expectation that the audio path ships with its tests run.

If AI assistance played a significant part in a pull request you send, a note in the description is welcome — it helps direct review.
