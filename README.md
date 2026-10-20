# Scav

A Claude Code plugin that rebuilds itself from the plugins you already have.
Scav reads your recent Claude Code and Codex conversations, finds which skills,
agents, commands, hooks and MCP servers you actually use, copies only those into
itself and turns off the bulky source plugins. Fewer always-on components means
less context spent on things you never call.

## Install

```bash
claude plugin marketplace add rrrozhd/scav
claude plugin install scav@scav
```

Requires [uv](https://docs.astral.sh/uv/) to run `scripts/scav.py`.

## Use

| Command | What it does |
|---|---|
| `/scav [days=90]` | Profile recent usage, confirm the picks, rebuild Scav, disable absorbed sources |
| `/scav find <need>` | Rank installed components, disabled plugins included, against a need |
| `/scav add <plugin or repo> <what you want>` | Pull specific components from a plugin or GitHub repo and pin them |
| `/scav unpin <id…>` | Let a later run prune a pinned component |
| `/scav eval [id…]` | Score components against a placebo skill with `claude plugin eval` (paid) |
| `/scav restore` | Undo: re-enable source plugins and drop ingested components |

Ingested components (`skills/`, `agents/`, `hooks/`, `.mcp.json`, `commands/*`
except `scav.md`) are generated and not tracked. Scav's own skills live in
`skills-src/`. State is kept in `~/.scav/`.

## Development

```bash
uv run --with pytest pytest -q tests
```

## License

MIT, see [LICENSE](LICENSE).
