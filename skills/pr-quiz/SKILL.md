---
name: pr-quiz
description: Quiz the user on a GitHub pull request to verify they understand it well enough to own it before merging. Use when the user runs /pr-quiz <github-pr-url> or asks to be quizzed on a PR.
---

# pr-quiz

Usage: `/pr-quiz <github-pr-url>`

Purpose: passing this quiz is the user's certificate that they understand a PR well enough to take ownership of it, and have not unknowingly ceded control of the code to an AI. Be a rigorous but fair examiner. The goal is the user's real understanding, not a score.

## 1. Fetch the PR

Parse owner, repo, and PR number from the URL, then run:

- `gh pr view <url> --json title,body,headRefOid,baseRefName,headRefName,files,additions,deletions`
- `gh pr diff <url>`

Record the head SHA (`headRefOid`). Read surrounding code in the local checkout when the diff alone is not enough to understand a change (callers, invariants, tests). If the local checkout is not on this PR's branch, use `gh api` or `gh pr diff` to read the relevant files rather than switching the user's branch.

**Size check:** if the PR is too large to quiz meaningfully (roughly more than 1,500 changed lines, or many unrelated concerns), stop and say so. Verdict: "Too big to own as one unit. Consider splitting," with a suggested split. Do not quiz.

## 2. Check for prior attempts

Transcripts live under `.pr-quiz/<owner>-<repo>/pr-<number>/` in the current repo (see section 7). If earlier attempts exist:

- Read the latest one. If it passed at the same head SHA, tell the user they already hold a valid certificate and stop (unless they ask to retake).
- If it passed at an older SHA, quiz only on the delta between that SHA and the current head SHA (`git diff <old>..<new>`, or the GitHub compare API).
- If it failed, this is a re-quiz: attempt number increments, and questions target the concepts previously missed plus fresh questions.

## 3. Generate the quiz (use a subagent)

Spawn a subagent with the PR metadata and diff (or delta) and have it produce the quiz and an answer key. Keep the key out of the conversation until after the user has committed to an answer.

Question count scales with size and risk: 4 for small PRs, up to 8 for large ones.

**Tiers.** Tag every question:
- `critical`: behavior, invariants, failure modes, concurrency, error handling, data or API contract changes, rollback or blast radius. The risky part of the change.
- `supporting`: secondary paths, structure, naming that carries meaning, test coverage.

At least half the questions must be critical. Skip formatting, trivial renames, and trivia.

**Types.** Mostly predict-the-behavior and trace-the-code questions with a verifiable answer ("Given input X, what does the new function return?", "What happens to in-flight requests when this changes?", "Which caller is affected by the changed precondition?"). Include 1 or 2 open questions ("What breaks first if assumption A is false?", "How would you roll this back?"). Multiple choice is allowed but distractors must be plausible misreadings of the diff, not throwaways. Avoid questions answerable by elimination.

**Verification.** Each question's answer must be checked against the code. Where feasible, confirm by reading the actual implementation or running the test suite or a small snippet. If a question's answer cannot be established with confidence, drop it. A wrong key destroys trust in the tool.

Each key entry contains: the correct answer, a grading rubric (for open questions: the specific points that must be present), the tier, a short tag for the concept, and a pointer to the file and lines it concerns.

## 4. Administer the quiz

- Ask one question at a time. Show the question number, tier is not shown to the user.
- Wait for the answer. Do not hint, and do not reveal anything about correctness until they answer.
- After each answer, grade it, state right or wrong, and give a brief explanation with a file and line reference. For open questions, say which rubric points were hit or missed.
- If an answer is partially right, mark it wrong for critical questions and give partial credit (0.5) on supporting ones.
- If the user disputes a grade with a good argument grounded in the code, re-check the code and concede openly if they are right. Update the transcript accordingly.
- If the user says "I don't know", record it as wrong, explain, and move on. Never shame them.

## 5. Verdict

Pass requires both:
1. Every critical question correct.
2. Overall score of at least 80%.

**On pass:** say so plainly, state the head SHA covered, attempt number, and critical and overall scores. Note that new commits to the PR invalidate the certificate for the delta.

**On fail:** do not reveal answers to anything not already graded. Give pointers only: which files and line ranges to reread and which concepts were weak ("revisit the retry path in client.cc"). Offer an immediate re-quiz, or let the user come back later. The re-quiz uses fresh questions: the missed concepts reframed, plus resampled questions from the rest of the diff. Never repeat identical questions.

## 6. Tone

Direct and collegial. No lecturing, no padding. Celebrate nothing; just report. If the user is repeatedly missing the same area, say so once and suggest reading it before the next attempt.

## 7. Save the transcript

Write each attempt to `.pr-quiz/<owner>-<repo>/pr-<number>/attempt-<N>.md` in the current repository's root.

First time in a repo: ensure `.pr-quiz/` is ignored without modifying tracked files by appending `.pr-quiz/` to `.git/info/exclude` if it is not already present there or in `.gitignore`.

Transcript format:

```
---
pr: <url>
head_sha: <sha>
attempt: <N>
date: <ISO date>
result: pass | fail
critical: <right>/<total>
overall: <score>/<total>
delta_from: <old sha or none>
---

## Q1 [critical] <concept tag>
**Question:** ...
**User answer:** ...
**Result:** right | wrong | partial
**Explanation:** ... (file:line)

(repeat for each question)
```

Questions also carry their tag in the heading so future tooling can aggregate recurring weak spots across transcripts.

After saving, print the path to the transcript so the user can open it.
