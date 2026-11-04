---
name: pr-quiz
description: Quiz the user on a GitHub pull request, answered in the browser, to verify they understand it well enough to own it before merging. Use when the user runs /pr-quiz [github-pr-url] or asks to be quizzed on a PR, typically right after /pr-review in the same session.
---

# pr-quiz

Usage: `/pr-quiz [<github-pr-url>]`. Without a URL, quiz the PR this
conversation has been about (e.g. the one `/pr-review` just ran on), else the
PR for the current branch (`gh pr view --json url`, or `github_access.py`'s
`find_open_pr()` where `gh` is not installed).

Purpose: passing this quiz is the user's certificate that they understand a PR
well enough to take ownership of it, and have not unknowingly ceded control of
the code to an AI. Be a rigorous but fair examiner. The goal is the user's real
understanding, not a score.

The user answers on a claude.ai Artifact page built from `quiz.html` in this
skill's directory. The page only ever holds the questions, the user's answers
and the graded results; the answer key stays on this machine.

Paths below are relative to the devenv_utils directory this skill lives under
(`subtrees/devenv_utils/` in a consumer repo; the repo root in devenv_utils'
own working clone).

## 1. Pin down the PR

Settle the following:

**Head SHA.** Fetch it fresh even when this conversation already knows the PR,
since `/pr-review` may have pushed fixes since: `gh pr view <url> --json
headRefOid,baseRefName,title`, or, where `gh` is not installed,
`github_access.py`'s `api("GET", "/repos/<slug>/pulls/<number>")` (`head.sha`,
`base.ref`, `title`). Then `git fetch origin`.

**The quiz covers what is pushed.** If the current checkout is the PR's branch,
`HEAD` must equal the head SHA and the tree must be clean. If anything is
unpushed or uncommitted, stop and say so, since the certificate would describe
code that is not on the PR. Never switch the user's branch; the subagent reads
files at the head SHA with `git show <sha>:<path>`.

## 2. Find prior attempts

Attempts live in `<main>/.pr-quiz/<owner>-<repo>/pr-<number>/`, where `<main>`
is the main checkout: `dirname "$(git rev-parse --path-format=absolute
--git-common-dir)"`. Never put them in a PR worktree; `pr_flow.py cleanup`
deletes it after the merge, certificate included. devenv_utils and the repos
`scaffold_consumer.py` sets up ignore `.pr-quiz/` in `.gitignore`. In any
other repo, check with `git check-ignore -q .pr-quiz/`, and if it is not
ignored, append `.pr-quiz/` to `$(git rev-parse --git-common-dir)/info/exclude`
rather than editing a tracked file. (In a worktree `.git` is a file, so
`.git/info/exclude` does not exist.)

Each attempt N has `attempt-N.md` (the transcript, section 7) and
`attempt-N.key.json` (the answer key). If earlier attempts exist:

- Read the latest transcript. If it passed (either pass verdict) at the same
  head SHA, tell the user they already hold a valid certificate and stop,
  unless they ask to retake.
- If it passed at an older SHA, quiz only on what changed since. When the old
  SHA is an ancestor of the new head, that is `git diff <old>..<new>`. After a
  rebase it is not, and that diff would include unrelated changes from the
  base branch: compare with `git range-diff`, or quiz the whole PR.
- If it failed, this is a re-quiz: the attempt number increments, and the
  questions target the concepts previously missed, plus fresh ones.

## 3. Generate the quiz (use a subagent)

Spawn one subagent on the session's model tier. A wrong key destroys trust in
the tool, so quality pays here. Give it the PR URL, repo path, base and head
SHA, the delta range if any, the attempt number, the concept tags missed in
earlier attempts, the two output paths (`quiz.json` in the staging directory
from section 4, and `attempt-N.key.json`), and the formats from the reference
section below. It reads the diff and code itself; this session does not need
to, since grading works from the key's file:line references. It writes both
files and returns only a one-line summary (question count and how many are
critical). Do not read the key file until grading: what you have not read, you
cannot leak.

Question count scales with size and risk: 4 for small PRs, up to 8 for large
ones.

**Too big to own.** A pass must mean the user understands the whole change. If
8 questions cannot cover it, because it is too large or bundles unrelated
concerns, the subagent writes no files and returns that verdict with a
suggested split instead of its summary. Size means substance: lockfiles,
generated or vendored code, and fixture data do not count. Relay the verdict
("Too big to own as one unit. Consider splitting," with the split) and do not
quiz.

**Tiers.** Tag every question:
- `critical`: behavior, invariants, failure modes, concurrency, error
  handling, data or API contract changes, rollback or blast radius. The risky
  part of the change.
- `supporting`: secondary paths, structure, naming that carries meaning, test
  coverage.

At least half the questions must be critical. Skip formatting, trivial
renames, and trivia. Cover commits a review cycle pushed (e.g. `/pr-review`
fixes): they are the code the user has seen least.

**Types.** Mostly predict-the-behavior and trace-the-code questions with a
verifiable answer ("Given input X, what does the new function return?", "What
happens to in-flight requests when this changes?", "Which caller is affected
by the changed precondition?"). Include 1 or 2 open questions ("What breaks
first if assumption A is false?", "How would you roll this back?").
Multiple-choice distractors must be plausible misreadings of the diff, not
throwaways; the page also asks for a one-sentence reason with every choice.
Avoid questions answerable by elimination, and never let one question's
wording give away another's answer: the user sees them all at once.

Quote code in a question's `code` field when the question is about specific
lines; the user should not need to hunt for them.

**Verification.** Each question's answer must be checked against the code.
Where feasible, confirm by reading the actual implementation or running the
test suite or a small snippet. If a question's answer cannot be established
with confidence, drop it.

## 4. Administer the quiz in the browser

**Stage.** Use a staging directory per PR,
`<scratchpad>/pr-quiz/<owner>-<repo>-pr-<number>/` (without a scratchpad, a
temporary directory under the working directory: the Artifact tool only
publishes files from those two places). Copy `skills/pr-quiz/quiz.html` there
as `index.html`, and set its `<title>` to `<repo>#<number> quiz`.

**Publish.** On the first attempt for a PR, publish `index.html` with
`files: {"quiz.json": "<staging>/quiz.json"}`, `capabilities: {artifact: {},
comments: {}}`, `icon: "quiz"`, and the PR title as `description`. The
`artifact` capability lets the page save the user's answers into the artifact;
`comments` lets it wake this session, since a page's own save does not. On a
later attempt,
publish to the artifact URL recorded in the previous transcript (read it
first if this session has not published it), with the new `quiz.json` and
`answers.json` and `results.json` set to `null`. Give the
user the link.

**Wait.** When the user submits, the page saves `answers.json` into the
artifact and sends Claude a short comment ("pr-quiz: answers submitted..."),
which starts a turn in this session. If the page could not send it, it tells
the user to say so in the terminal. Either way, read the artifact itself
(`action: "read"`, `url`): the page's save is a newer version, and a publish
not built on it is refused. Then read the answers (`path: "answers.json"`); if
the file is missing or from another attempt, the save may still be landing, so
read once more after a few seconds. The harness may already have auto-replied
to the comment; if so, post no reply of your own, and resolve the thread
(`ArtifactComments`) once the results are published. If the page cannot save
at all (a view without write access), it shows the answers as JSON for the
user to paste into the terminal instead.

While the quiz is open, do not discuss the PR in the terminal. If the user
asks about the code, say it can wait until they submit.

## 5. Grade

Read the key and grade every answer as `right`, `partial` or `wrong`, with a
brief explanation and file:line references. For open questions, say which
rubric points were hit or missed. For multiple choice, a correct choice whose
reason misreads the code is `partial`. "I don't know" and unanswered
questions are `wrong`; never shame the user for them.

**Verdict.** It is one of `pass`, `pass_with_notes` or `fail`:

1. Any critical question not fully right (partial included) fails the quiz.
   This rule has no exceptions; it is the core of the certificate.
2. Otherwise it is your judgment, made from the specific answers, not a
   percentage. Supporting misses fail the quiz when together they show the
   user could not maintain the change: could not say how it is wired in,
   where it is tested, or what a name in it promises. Isolated or cosmetic
   misses pass, `with_notes` when there is something worth rereading.

The rationale, two to four sentences, must cite specific answers ("You traced
the retry path and the cancellation case correctly; your answer on the cache
key was vague, but it is cosmetic here"). This is the check on generosity: a
verdict you cannot ground in what the user wrote is the wrong verdict.
`notes` lists the files, line ranges and concepts to reread.

Write `results.json` to the staging directory and publish it to the artifact
(`url`, `file_path` of the staged `index.html`, and `files: {"results.json":
...}`; a publish needs the page even when only a data file changed). The
user's page reloads to show it.
Save the transcript (section 7), then report the verdict in the terminal in a
line or two with the transcript path.

**Disputes** happen in the terminal: the page tells the user to raise any
disagreement with the session, whether with an answer in the key, with how
their own answer was marked, or with the verdict. Re-check the code. If the user is right,
concede plainly: regrade the question, revisit the verdict, note the regrade in
that question's explanation, republish `results.json`, and update the
transcript.

**On pass:** the page states the head SHA covered. New commits to the PR
invalidate the certificate for the changes they bring.

**On fail:** offer a re-quiz now, or let the user come back later. The page
has shown the explanations, so re-quiz questions must test the missed
concepts from a different angle, not ask for the same fact again. Resample
the rest of the diff too. Never repeat a question.

If the Artifact tool is not available, give all questions in one terminal
message and take the answers in one reply; everything else is unchanged.

## 6. Tone

Direct and collegial. No lecturing, no padding. Celebrate nothing; just
report. If the user is repeatedly missing the same area, say so once and
suggest reading it before the next attempt.

## 7. Save the transcript

Write `attempt-N.md` in the attempt directory from section 2:

```
---
pr: <url>
head_sha: <sha>
attempt: <N>
date: <ISO date>
verdict: pass | pass_with_notes | fail
critical: <right>/<total>
delta_from: <old sha or none>
artifact: <artifact url>
---

## Verdict
<rationale>

<notes, one per line>

## Q1 [critical] <concept tag>
**Question:** ...
**User answer:** ...
**Result:** right | partial | wrong
**Explanation:** ... (file:line)
**Dispute:** ... (omit if none)

(repeat for each question)
```

Questions carry their tag in the heading so future tooling can aggregate
recurring weak spots across transcripts.

## Reference: file formats

`quiz.json` (written by the subagent; the page renders it). `kind` is
`choice` or `open`; `code` is optional; `start` is the first line's number.

```json
{
  "pr": {"url": "...", "repo": "owner/repo", "number": 25, "title": "..."},
  "head_sha": "<full sha>",
  "attempt": 1,
  "delta_from": null,
  "questions": [
    {"id": "q1", "kind": "choice", "prompt": "Markdown-lite: paragraphs, `code`, ``` fences.",
     "code": [{"path": "src/client.py", "start": 120, "text": "..."}],
     "choices": ["...", "...", "..."]},
    {"id": "q2", "kind": "open", "prompt": "..."}
  ]
}
```

`attempt-N.key.json` (written by the subagent; never published). For choice
questions `answer` is the letter.

```json
{"q1": {"tier": "critical", "tag": "retry-backoff", "answer": "B",
        "rubric": ["points an open answer must make"], "refs": ["src/client.py:120-134"]}}
```

`answers.json` (written by the page). It carries the quiz's `attempt` and
`head_sha`; the page ignores answers and results from another attempt.

```json
{"attempt": 1, "head_sha": "...", "submitted_at": "...",
 "answers": {"q1": {"choice": "B", "reason": "..."}, "q2": {"text": "..."}, "q3": {"dont_know": true}}}
```

`results.json` (written by you):

```json
{"attempt": 1, "head_sha": "...", "verdict": "pass_with_notes",
 "rationale": "...", "notes": ["Reread the retry path, src/client.py:120-160."],
 "questions": {"q1": {"result": "right", "explanation": "...", "refs": ["src/client.py:128"]}}}
```
