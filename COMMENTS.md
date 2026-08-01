# Comments and documentation

> **Audience: the coding agent.** The doctrine for writing and maintaining
> comments and documentation in a repo that uses devenv_utils — a consumer's
> `CLAUDE.md` links here instead of restating it.

Comments and documentation serve the purpose of helping human expert
developers to better understand the code: how it works, why certain
architecture/implementation decisions were made, where to find the logic
controlling some specific behavior, where to add new functionality, what
disciplines must be followed when extending the code.

Such comments and documentation will naturally also be helpful to AI agents.
But, keep the human in mind first and foremost. Humans benefit from
succinctness and organization.

Comments and documentation are NOT for the purpose of giving an AI agent a
spec from which they could re-implement the code.

## The courtroom test

A good litmus test for comments: imagine for each comment, a courtroom battle
is waged. The plaintiff argues for the comment's removal. The defendant should
be able to argue why removing the comment would diminish an expert developer's
ability to understand the code, to be able to add features to the code, or to
improve the code. If a convincing argument cannot be put forth, the defendant
loses, and so the comment should be removed.

Note that the courtroom test is about whether a comment earns its place, not
about how short it is. Once a comment has earned its place, write it as a
readable sentence; compressing it into a terse fragment costs the reader and
saves nothing. Short example usage is welcome on the same terms — a couple of
lines that spare someone reverse-engineering a calling convention have earned
their place.

## The research-audit test

Another good litmus test: whenever you finish implementing something the user
requested, go back and audit what research you had to do. Then ask yourself:
are there any changes that could be made to the documentation that would have
helped you get to the end goal more efficiently? Not "teaching to the test"
documentation, but rather documentation that is more likely to be applicable
to future tasks.

For example, if the code entails non-obvious performance optimizations, or has
special considerations for multi-threading or backwards-compatibility, or is
utilized in some sort of framework or pipeline that imposes requirements from
outside of the code in question — if these things are not well documented,
then you might expend more effort than you otherwise would need to. Identify
such things, and be proactive about making the necessary documentation changes
(keeping in mind the courtroom test). Constantly leave documentation in a
better place than you found it, whether that means removing details, adding
details, or moving them from one file to another. Folding such changes into
the PR that prompted them is fine, within reason; if they would take the PR
too far from its main purpose, do them separately and say so.

## Structure comments vs. why comments

Ideally, code is simple and straightforward enough to be self-documenting.
When a comment exists to explain STRUCTURE — invariants tying several data
members together, a non-obvious relationship between functions — take a step
back and ask whether the code could be refactored to become more
self-documenting. For example, maybe a class has a collection of private data
members that require nontrivial invariants to be held between them, warranting
comments. Can those data members be abstracted out into a class with a clear
nameable responsibility? Then those comments can be removed from the parent
class.

This does not apply to comments that explain WHY — a rationale for choosing
one approach over another, an argument that some bound is sound, a constraint
imposed from outside the code. No amount of refactoring makes those
unnecessary, so do not contort the code trying.

Note: reference material a reader looks things up in — on-disk formats, tensor
layouts, bit-field encodings — is worth spelling out itemized even though it
restates the code. The reader wants one specific detail, and an organized
table makes it findable without reading the code that produces it.

## Placement and locality

Placement matters as much as content. A header should say what a thing is and
why it exists; the "how" belongs at the implementation site, beside the code
it describes. So when trimming a header, "how" that genuinely earns its keep
moves down rather than being deleted. Watch for cross-references as it moves —
a "see the class comment" left behind in an implementation file goes stale
silently.

Locality rule of thumb: changing an implementation detail should usually force
a comment change in AT MOST one spot, within a close radius of the code
changed. If altering a detail inside a function body demands edits to comments
in two places, at least one of them was likely misplaced to begin with.

## Reactionary comments and history

Do not word comments in a "reactionary" way based on conversation with the
user. For example, if the user requests, "A is bad because of X, can you
change to B", then the implementation of B does NOT need a comment saying,
"This does B. It does not do A, since that would suffer from X." That's a
reactionary comment. Principle: imagine if the entire codebase was written
one-shot from scratch. Would this comment still be written like this? If not,
it's probably not appropriate. The same test rules out references to the
code's own history — "we replaced", "previously", "the old X", "now uses",
"formerly". That belongs in commit messages, not in the code.
