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

## Your task

Propose ONE issue that:
- Is clearly scoped — an agent can implement it in a single PR (under ~300 lines of changes)
- Has a concrete acceptance criterion ("when X is true, the issue is done")
- Improves the repo in a useful way (bug fix, missing test, missing doc, refactor, small feature, dev ergonomic)
- Is NOT vague ("improve performance"), NOT product-direction ("add user auth"), NOT a rewrite
- Is something an agent reading the README and source files could actually figure out without human guidance

DO NOT propose issues that:
- Require human judgment ("decide whether we should...")
- Need credentials or production access
- Are speculative refactors with no concrete benefit
- Repeat the kind of work Dave just finished (check the lessons + recent proposals)

Respond with ONLY this JSON:
```
{{
  "title": "Short imperative title — under 70 chars",
  "body": "1-3 paragraphs of context. What's the problem? Where in the code is it? What does done look like? Be specific enough that someone reading just this could implement it.",
  "category": "bug" | "test" | "docs" | "refactor" | "feature" | "ergonomic",
  "skip": false,
  "skip_reason": "If you can't find a good issue to propose, set skip=true and explain why here"
}}
```

If nothing genuinely worth doing exists right now, set `skip: true` and `skip_reason` to a sentence explaining why. Don't make up busywork.

No markdown fences. No prose outside the JSON.
