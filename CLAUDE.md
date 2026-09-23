# Kalinka Player

## Git

**Never push to a remote unless the user asks for it in that message.** No
`git push`, no PRs, no releases, no tags — publishing is always a separate,
explicit decision, and permission to commit is never permission to push.

Committing locally is fine when asked for. When work is ready, say so and let
the user decide whether it goes out.

**No AI attribution footers.** Commit messages end at their last body
paragraph and PR bodies at their last content section — no
`Co-Authored-By: Claude`, no "Generated with Claude Code". AI use is disclosed
at the repository level in [CONTRIBUTING.md](CONTRIBUTING.md), not per
commit.

## Credentials

**A credential never reaches a log in plain text** — at no level, not
through an exception's text, a model's repr, a URL carrying a key, or a
library logger. A config field holding one takes the `password` widget: the
server then never sends it to a client (only whether it is set) and logs it
as `<secret>` (`kalinka_server/config_secrets.py`). Code that handles a
credential anywhere else redacts it itself.

## Comments

No AI slop. The default is **no comment**: code that needs a comment to be
understood usually needs a better name instead. When a comment does earn its
place — a non-obvious invariant, a workaround, a reason — keep it to one short
line that says *why*, never *what*. Do not narrate the code, do not restate the
signature, do not leave section banners. This applies to every language in the
repo.

Declarations are the exception: classes and the interfaces they expose get
Doxygen comments — a `/** @brief ... */` block, with `@param`, `@return` and
`@note` only where they carry something, a `///` one-liner where that is
enough. Document the contract — what the thing is for, ownership, threading,
what a caller may rely on — still never a restatement of the signature.

## No TODOs

Never leave `TODO`, `FIXME`, or `XXX` behind. Either do the work now, or leave
the code correct as-is and tell the user in your reply what remains — the
conversation is the backlog, not the source.

## Design

Follow SOLID. Classes get one reason to change, depend on abstractions rather
than concrete collaborators, and stay substitutable for the interfaces they
implement.

When a change would be easier by bolting onto something that already breaks the
design, prefer refactoring the hack away over deepening it. But **ask first if
the refactoring is massive** — touching many files, changing a public interface,
or reshaping a subsystem. Small, local cleanups on the path of the change need
no permission.

Don't repeat yourself once the fragment is big enough to name. A block that
would be copied and then edited in two places belongs in one — a function, a
base class, a shared module — so there is a single place to fix when it turns
out to be wrong. Two lines that merely look alike are not duplication:
extracting those costs more than it saves.

## Ownership (C++)

Raw pointers are for short-lived local work — a buffer walked inside one
function, a handle a C API hands back. Nothing else. A `T *` member aimed at
another of our objects is a lifetime bug waiting for its owner to be replaced:
say what the ownership is instead — `unique_ptr` for a single owner,
`shared_ptr` where the lifetime is genuinely shared, and a reference where the
callee only borrows for the duration of the call.

## Tests

Unit tests are mandatory, and especially so in the C++ code — a change to
`packages/kalinka-renderer/src/native_player/` ships with tests in
`packages/kalinka-renderer/tests/native_player/` in the same commit.

The audio graph is the fragile part: any change reaching `AudioGraphNode`,
`AudioPlayer`, `AudioStreamSwitcher`, the decoders, or `AlsaAudioEmitter` must
be reviewed for regressions against the existing suite, and the suite must be
run (`ctest` on the `kalinka-renderer-tests` target), not assumed to pass.

Exception: the server's playqueue suite is slow and flaky — skip
it when that code is untouched.

## Review

Always review the code once the work is done. Read the finished diff back as a
reviewer would, against these rules — SOLID, no duplication worth naming, no
slop comments, no TODOs, tests present and run. Fix what the review turns up
before saying the work is ready; if something is deliberate, say why in your
reply rather than leaving it unexplained.
