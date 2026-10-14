# dtxWiki

**Give your AI the knowledge you've already built.**

[Website](https://www.dtxwiki.com/) · [Documentation](http://docs.dtxwiki.com/) · [PyPI](https://pypi.org/project/dtxwiki/) · [한국어](https://github.com/dtxSuite/dtxWiki/blob/main/README.ko.md)

dtxWiki connects your notes, source materials, and decisions in a knowledge base
that you and your AI tools can use together. Keep your reasoning and references
within reach, and build on them when the next task begins.

Write and connect Markdown pages in **Context**, organize reference materials
in **Library**, and connect supported AI apps through MCP (Model Context Protocol).
They can find, read, and update the same knowledge you work with, within the
access you grant.

[![Actual dtxWiki Context screen showing an onboarding plan, interview notes, and product decisions.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/context-preview.en.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/context-preview.en.png)

*Context preview: an actual dtxWiki screen with sample project content.*

## Less explaining, more building

- **Reuse the background you've already prepared.** Keep research, requirements,
  and earlier decisions available across tasks. Ask your connected AI tool to
  find the relevant context before helping with the next step.
- **Keep the reasoning with the result.** Connect a plan to the interviews,
  notes, and decisions behind it. When you return to a project, you can understand
  how it got there as well as what needs to happen next.
- **Explore the connections.** Open a Context page's Local Graph to follow
  links, citations, and documents read together. A workspace owner can enable
  the Workspace Knowledge Map in **Settings > Workspace > Knowledge graph**.
- **See what your AI did.** **Chats** shows the searches, reads, and updates
  performed through dtxWiki's MCP tools. Follow the materials consulted and
  inspect changes in document history. Chats records tool actions, not full
  conversations from your AI app.

[![Actual dtxWiki Chats screen showing recorded MCP tool activity for a sample project.](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/chats-preview.en.png)](https://raw.githubusercontent.com/dtxSuite/dtxWiki/main/.github/assets/readme/chats-preview.en.png)

*Chats preview: recorded tool activity from a sample MCP session.*

Run dtxWiki on your own computer or a server you manage. Share a workspace with
teammates and choose what people and connected AI tools can access. You can also
write, organize, and search without an AI account or API key.

## A practical example: update a product plan

1. **Keep the background together.** Record customer interviews and past
   decisions, then link them to your product plan.
2. **Use it in the next task.** Ask a connected AI tool to find the relevant
   feedback and help revise the plan.
3. **Check the work.** Review the tool activity in Chats and the changes in
   document history. The updated plan and its background stay available for
   the next iteration.

The same approach works for research reports, team handbooks, course materials,
and creative projects.

## Get started

You need **Python 3.12 or newer** and [pipx](https://pipx.pypa.io/) or
[uv](https://docs.astral.sh/uv/).

Install and start with pipx:

```bash
pipx install dtxwiki
dtxwiki
```

Or with uv:

```bash
uv tool install dtxwiki
dtxwiki
```

Open **[http://127.0.0.1:21080/](http://127.0.0.1:21080/)** if your browser does not open automatically.
Create an account on your first visit. Keep the command running while using
dtxWiki; press `Ctrl+C` to stop it.

If you prefer npm, run `npx @dtxsuite/dtxwiki`. This requires Node.js 22 or newer
and uv or pipx; with pipx, Python 3.12 or newer must also be installed.

Start with one Context page about your current project. Link the background,
then connect an AI tool through **Settings > Integrations** when you want help.

**[Open the documentation →](http://docs.dtxwiki.com/)** for installation details,
AI connections, team setup, updates, and everyday workflows.

## Get involved

Share a use case, suggest an improvement, or report a problem on
[GitHub](https://github.com/dtxSuite/dtxWiki/issues).
For code and documentation contributions, see the
[contribution guide](https://github.com/dtxSuite/dtxWiki/blob/main/CONTRIBUTING.md).

## License

dtxWiki is available under your choice of **AGPL-3.0-only OR Elastic-2.0**.
See [LICENSE](https://github.com/dtxSuite/dtxWiki/blob/main/LICENSE) for the terms,
third-party notices, and commercial licensing options.
