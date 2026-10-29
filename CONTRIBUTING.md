# Contributing

## Issues

Issues are welcome: bug reports, hardware that misbehaves, features you miss. Reports from hardware the maintainer doesn't have — Apple devices, for instance — are especially useful. Use the [bug report form](https://github.com/Kalinka-Player/KalinkaPlayer/issues/new/choose); problems with the phone or desktop app go to [KalinkaAI](https://github.com/Kalinka-Player/KalinkaAI/issues/new/choose).

## Code

Fixes that can be reproduced and verified without special hardware are made by Kalinka Bot, which works through the same issue list, so a pull request for one duplicates work already planned. Code is accepted only when both of these hold:

- it fixes an issue labelled [`help wanted`](https://github.com/Kalinka-Player/KalinkaPlayer/labels/help%20wanted);
- it was tested on the real hardware that issue is about, and the description proves it: which device, OS and version it ran on, what you checked, and the evidence — logs from the run, or a screenshot or screen recording for a UX change.

Any other pull request is closed without review.

### Sending a pull request

1. Fork the repository and create a feature branch.
2. Make changes with editable installs active so they take effect immediately.
3. Run `make test` (and any package-specific tests for the area you touched).
4. Test the change on the hardware the issue is about.
5. Open a pull request that links the issue, describes the change and the hardware test, and attaches the evidence.

A pull request runs [scripts/check_credential_logging.py](scripts/check_credential_logging.py), which flags code that could write a credential to a log — a credential-named value passed to a log call or a raised exception — as an annotation on the line. Log without the value; keep a false positive the review agrees on with `# log-safe: <why>` on the flagged call.

## AI-assisted development

Kalinka's server, renderer and bundled plugins are built with **substantial AI assistance**. The original core and architecture were developed manually. Much of the more recent implementation, refactoring and maintenance work has been produced with AI assistance, working from maintainer-defined requirements. Architecture, technical direction, review, testing and acceptance of changes remain the responsibility of the maintainer.

AI use is disclosed at the repository level rather than on individual commits, so commit messages and pull requests carry no co-authorship trailers or generated-with footers.

Changes are reviewed and tested to the same standard however they were produced. [CLAUDE.md](CLAUDE.md) records the conventions, including the expectation that the audio path ships with its tests run.

If AI assistance played a significant part in a pull request you send, a note in the description is welcome — it helps direct review.
