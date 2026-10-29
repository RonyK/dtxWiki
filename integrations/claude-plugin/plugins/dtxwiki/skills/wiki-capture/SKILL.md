---
name: wiki-capture
description: Orchestrate capture of conversation knowledge into dtxWiki by composing the conventions, retrieval, and authoring-review skills. Use when a user asks to capture, preserve, or turn a discussion into dtxWiki knowledge; this skill adds no independent policy or write semantics.
---

# Wiki Capture

This skill is orchestration only. Apply `dtxwiki-conventions`, `dtxwiki-retrieval`, and `dtxwiki-authoring-review` as the sources of truth instead of restating or weakening their rules.

1. Call `guide` to resolve the workspace, effective capabilities, and live policies; choose a placement with `get_index(format='nodes')`.
2. Identify the durable claims, decisions, sources, uncertainties, and requested destination in the conversation. Ask for a missing material choice; do not invent it.
3. Retrieve existing knowledge to avoid an uninformed duplicate and to obtain the current base for any update.
4. Read complete evidence, obtain write-ready citations where factual claims need them, and upload any new image before referring to it.
5. Create or update a Context draft with a concise reason and accurate review intent.
6. Report the draft outcome, warnings, objective defects, and the next human action. Never describe the capture as published or approved unless returned human-authored state proves it.

Do not run maintenance as part of capture. Use `dtxwiki-maintenance` only when maintenance is explicitly requested.
