You are reading a GitHub repository looking for ONE good issue an autonomous coding agent should work on next. The agent (Dave) is already filling the repo's issue queue when humans don't.

## Repository: {repo}

### README (excerpt)
{readme}

### File tree (top of)
```
{file_tree}
```

### Recently completed work (last 10 lessons Dave learned on this repo)
{lessons}

### Issues Dave has already proposed (don't repeat these or anything close)
{recent_proposals}

### Recent proposal categories — VARY FROM THESE
{recent_categories}

## Your task

Propose ONE issue that:
- Is clearly scoped — an agent can implement it in a single PR (under ~300 lines of changes)
- Has a concrete acceptance criterion ("when X is true, the issue is done")
- Improves the repo in a useful way
- Is NOT vague ("improve performance"), NOT product-direction ("add user auth"), NOT a rewrite
- Is something an agent reading the README and source files could actually figure out without human guidance

## CRITICAL: vary the work type

Look at the "Recent proposal categories" above. **Do NOT propose another issue in a category that already appears in your last 2 proposals.** If your last two proposals were `docs`, you must propose something OTHER than docs this time. If your last two were `test`, propose something other than test. The goal is a *balanced* contribution to the repo across these categories:

- **`bug`** — fix a real defect you can see in the source files (off-by-one, wrong condition, missing null check, leaked resource, etc.)
- **`test`** — add unit/integration tests for a function or module that lacks coverage
- **`refactor`** — improve code quality without changing behavior (extract a function, simplify a condition, dedupe, rename for clarity)
- **`feature`** — small, well-scoped new capability that fits the existing API surface (new helper function, new config option, new flag)
- **`ergonomic`** — improve developer/user experience (better error messages, clearer log output, helpful CLI flag, sample script in an examples/ dir)
- **`docs`** — README, CONTRIBUTING.md, function docstrings, API reference, code comments

Docs work is the easy default — resist it unless docs are genuinely the highest-value next step. Prefer `bug`, `test`, `refactor`, `feature`, and `ergonomic` work when there's anything plausible in those categories. If you've done docs in either of your last two proposals, you MUST pick a different category this round.

## DO NOT propose issues that

- Require human judgment ("decide whether we should...")
- Need credentials or production access
- Are speculative refactors with no concrete benefit
- Repeat the kind of work Dave just finished (check the lessons + recent proposals)

## Response format

Respond with ONLY this JSON:
```
{{
  "title": "Short imperative title — under 70 chars",
  "body": "1-3 paragraphs of context. What's the problem? Where in the code is it? What does done look like? Be specific enough that someone reading just this could implement it.",
  "category": "bug" | "test" | "refactor" | "feature" | "ergonomic" | "docs",
  "skip": false,
  "skip_reason": "If you can't find a good issue to propose, set skip=true and explain why here"
}}
```

If nothing genuinely worth doing exists right now, set `skip: true` and `skip_reason` to a sentence explaining why. Don't make up busywork.

No markdown fences. No prose outside the JSON.
