# Contributing

## Issues

Issues are welcome: bug reports, hardware that misbehaves, features you miss. Reports from hardware the maintainer doesn't have — Apple devices, for instance — are especially useful. Use the [bug report form](https://github.com/Kalinka-Player/KalinkaPlayer/issues/new/choose); problems with the phone or desktop app go to [KalinkaAI](https://github.com/Kalinka-Player/KalinkaAI/issues/new/choose).

## Code

Pull requests are taken only for an open issue labelled [`help wanted`](https://github.com/Kalinka-Player/KalinkaPlayer/labels/help%20wanted). That label marks what needs someone else's hands, usually hardware the maintainer doesn't have: NVMe storage, Apple devices, uncommon arm64 boards. Everything else is fixed by Kalinka Bot, which works through the same issue list, so a pull request for it would duplicate work already planned.

If the issue you want to fix isn't labelled `help wanted`, or there is no issue yet, open one or comment on it asking for the label. Once it has it, send the pull request. A pull request that doesn't fix a `help wanted` issue is closed automatically.

The description must show the change was tested on the hardware the issue is about: which device, OS and version it ran on, what you checked, and the evidence. That is logs or terminal output from the run for a change to the server, the renderer or an image, and a screenshot or screen recording for a change to something you see.

### Sending a pull request

1. Make sure the issue is labelled `help wanted`.
2. Fork the repository and create a feature branch.
3. Make changes with editable installs active so they take effect immediately.
4. Run `make test` (and any package-specific tests for the area you touched).
5. Test the change on the hardware the issue is about.
6. Open a pull request with `Fixes #<number>` in its description, describing the change and the hardware test, with the evidence attached.

A pull request runs [scripts/check_credential_logging.py](scripts/check_credential_logging.py), which flags code that could write a credential to a log — a credential-named value passed to a log call or a raised exception — as an annotation on the line. Log without the value; keep a false positive the review agrees on with `# log-safe: <why>` on the flagged call.

## AI-assisted development

Kalinka's server, renderer and bundled plugins are built with **substantial AI assistance**. The original core and architecture were developed manually. Much of the more recent implementation, refactoring and maintenance work has been produced with AI assistance, working from maintainer-defined requirements. Architecture, technical direction, review, testing and acceptance of changes remain the responsibility of the maintainer.

AI use is disclosed at the repository level rather than on individual commits, so commit messages and pull requests carry no co-authorship trailers or generated-with footers.

Changes are reviewed and tested to the same standard however they were produced. [CLAUDE.md](CLAUDE.md) records the conventions, including the expectation that the audio path ships with its tests run.

If AI assistance played a significant part in a pull request you send, a note in the description is welcome — it helps direct review.
