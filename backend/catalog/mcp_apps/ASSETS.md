# MCP app catalog assets

The app catalog keeps local, hash-pinned artwork so setup discovery remains available offline and fails closed when packaged bytes drift. Product names remain visible next to every decorative image; these marks identify the corresponding client and do not imply vendor endorsement of dtxWiki.

Vendor marks are not relicensed under dtxWiki's source-code license. Their inclusion is nominative, remains subject to each vendor's trademark and brand terms at the cited source, and grants no right to modify or reuse the artwork outside this catalog. A maintainer replacing a vendor asset must re-check the current upstream terms, preserve the adjacent product/vendor label and non-endorsement presentation, record the exact archive member, and update the pinned hash in the same change. If redistribution is no longer permitted or provenance cannot be verified, replace the mark with a dtxWiki-authored neutral icon instead of silently retaining it.

## Claude

- Catalog mapping: `claude-code` -> `icons/claude-code.svg`
- Source: Anthropic Newsroom, **Download press kit** (`https://www.anthropic.com/news`, retrieved 2026-08-05)
- Source member: `Anthropic media resources/Anthropic logos/Claude logos/3 Claude Spark/SVG/Claude Spark - Clay.svg`
- Use: unmodified vendor-provided Claude mark adjacent to the text `Claude Code`
- Rights note: Anthropic retains the Claude mark; the press-kit asset is used only to identify the compatible client, without modification or an endorsement claim

## Codex

- Catalog mapping: `codex` -> `icons/codex-cli.svg`
- Source: OpenAI Design Guidelines (`https://openai.com/brand/`, retrieved 2026-08-05)
- Download: `https://cdn.openai.com/brand/openai-logos.zip`
- Source member: `OpenAI-logos/SVGs/OAI_OpenAI-Blossom_Black.svg`
- Use: unmodified OpenAI Blossom adjacent to the text `Codex CLI`; the containing UI supplies a neutral light icon surface so the black artwork is not recolored in dark mode
- Rights note: use follows the linked OpenAI Design Guidelines, including clear product attribution and no implication that OpenAI sponsors or endorses dtxWiki

## Gemini CLI and Google Antigravity

- Catalog mappings: `gemini-cli` -> `icons/gemini-cli.svg`; `antigravity` -> `icons/antigravity.svg`
- Source: original neutral compatibility artwork authored for dtxWiki on 2026-08-21
- Use: generic terminal and orbit motifs adjacent to the plain-text product names; neither asset copies a Google, Gemini, or Antigravity logo
- Rights note: product names identify compatible clients only. The artwork and catalog text do not claim Google sponsorship, endorsement, certification, or partnership.

The corresponding SHA-256 values are stored in `catalog.v1.json` and validated by `CatalogRepository` for both the source tree and packaged resources.
