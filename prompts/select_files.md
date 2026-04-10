You are picking which files an autonomous coding agent needs to read in order to implement a GitHub issue.

## Issue #{issue_id}: {title}

{body}

## Repository: {repo}

## All tracked files
```
{file_list}
```

Your job: return the minimal set of files (between 1 and 10) the agent should read to implement this issue correctly. Include:
- Files that contain the code being modified
- Files that define types/functions/symbols the issue references
- Files that show conventions to match (similar features, neighboring tests)
- Configuration or schema files if the issue affects them

Do NOT include:
- Lockfiles, generated files, or large data files
- Files unrelated to the change
- The README (the agent already has it)

Respond ONLY with a JSON object of this exact shape:
```
{{"files": ["path/one.py", "path/two.py"], "reasoning": "1-2 sentences on why these files"}}
```

No markdown fences. No prose outside the JSON.
