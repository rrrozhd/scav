---
description: Profile your Claude + Codex work, then rebuild Scav with only the plugin components you use and turn off the bulky source plugins
argument-hint: "[days=90] | find <need> | add <plugin or repo> <what you want from it> | unpin <id…> | eval [id…] | restore"
---

You are running Scav. `SCAV` means `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/scav.py`. `REFRESH` means `claude plugin marketplace update scav && claude plugin update scav@scav`.

If `$ARGUMENTS` is `restore`, run `SCAV restore`, then REFRESH, report the output and stop.

If `$ARGUMENTS` starts with `find`, follow the `scav:find-missing-skill` skill for the rest of the arguments and stop. Mid-task, the model invokes that skill on its own when it lacks a capability. `SCAV find "<need>" [--limit 5] [--catalogs] [--github]` ranks every installed component, including those of disabled plugins, by keyword overlap with the need. Each line shows id, plugin, status (`on`, `off`, `in-scav`, `not installed` for a plugin offered by a marketplace already on disk with `--catalogs`, `github ★N pushed DATE` for a SKILL.md found on GitHub with `--github`, `github ★N pushed DATE, listing only` for a GitHub plugin or `.mcp.json` offering MCP servers or hooks, or `fetched` for a component of a repo cloned by `SCAV fetch <owner>/<repo>`), tokens and description. MCP servers and hooks are described from disk without starting anything: the command or URL a server runs, the events, matchers, scripts and script comments of hooks, the plugin's description and the README lines naming a server. `SCAV add --trial <id…>` works like add but doesn't pin. Trials are listed under `trial` in `~/.scav/manifest.json`, and the next full run drops them if they went unused.

If `$ARGUMENTS` starts with `unpin`, run `SCAV unpin <ids>`, report the output and stop.

If `$ARGUMENTS` starts with `eval`, run `SCAV eval [--component <id>]… --max-cost-usd 5` in the background (it scores each skill, agent or command against a placebo skill via `claude plugin eval` and appends to `~/.scav/evals/history.jsonl`). With no ids it evaluates everything Scav holds: that is a long, paid run, so first state the budget and get a yes.
- MCP servers are scored only with `--run-servers`, which starts each server's real command, as the user, outside the eval sandbox. The note for an unevaluated server shows that command: show it and get a yes before adding the flag. A server is judged against the same eval's no-server arm on prompts where its tools were used before, with a Sonnet judge that sees the tool results; one that never connects in the sandbox (OAuth, network, local state) gets a note, not a verdict.
- Hooks get no verdict, because a hook acts across whole sessions and there is no single answer to judge. `hooks` in the output shows what each did in past sessions: runs that `ran`, printed `output`, `blocked` or hit an `error`, the number of `sessions`, and `shared_with` when another plugin ships the same command.
If the output's `calibration.ok` is false, prune verdicts are held back as `uncalibrated`: offer `SCAV eval --calibrate` with the same `--model`/`--judge-model` (`--judge-model sonnet` for MCP servers; about $2 on Sonnet). Report the calibration line, then per component: verdict, delta and n (distinct prompts it fired on), fired rate, the `notes` for anything unevaluated, and the `hooks` evidence. Then stop.

## Add mode: `$ARGUMENTS` starts with `add`

The first word after `add` is the source. Everything after it is what the user wants from it, in plain language. Example: `add ecc@ecc the django testing stuff and a security reviewer`.

1. **Install if needed.** Run `claude plugin list` to check.
   - If the source is a GitHub `owner/repo` or a URL, run `claude plugin marketplace add <source>` first. Then install the plugin it offers with `claude plugin install <plugin>@<marketplace>`.
   - Already-installed plugins are fine whether they are enabled or not.
2. **List.** Run `SCAV list <plugin>`. It prints one line per component: id, always-on tokens, file path and description.
3. **Match.** Pick the components that deliver what the user asked for.
   - Judge by description first. When a description is vague, or several components look alike, read the file at its path.
   - Follow dependencies. If a picked skill tells the model to invoke another skill or agent from the same plugin, or to run its scripts, pick those too.
   - Hooks and MCP servers count only if the request is about that behavior.
   - If nothing matches, say so and stop. Never pad the pick.
4. **Confirm.** Show the user, in under 10 lines:
   - each pick with a one-line reason and its token cost,
   - anything close that you left out,
   - whether to disable the source plugin. Default yes. Say no only if the user wants the rest of the plugin to stay live.

   Wait for a yes.
5. **Apply.** Run `SCAV add <id…> [--disable <plugin@marketplace>]`. This pins the picks, so later `/scav` runs keep them even with zero uses. Then run `claude plugin disable <plugin@marketplace>` if disabling, then REFRESH. Surface any warnings. Tell the user to start a new session, and that `/scav unpin <id>` lets a later run prune a pick again.

Stop here in add mode.

## Full run: anything else

Follow these steps in order. `DAYS` is the number in `$ARGUMENTS`, or 90 if there is none.

## 1. Profile (deterministic)

Run `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/scav.py profile --days DAYS`. It writes these files:
- `~/.scav/inventory.tsv`: every installed component, one per line. Columns are `id` (`kind:plugin:name`), `uses` (real invocations in Claude transcripts; for an MCP server, calls to its tools; for hooks, runs that blocked or printed something), `cx` (Codex sessions that opened a same-named skill from its own `~/.codex/skills` copy, which is a related signal, not proof), `kw` (how much the description's words overlap with the user's prompts), `ev` (eval verdict, mean side-by-side score versus a placebo skill and the number of distinct prompts behind it, e.g. `keep+0.25/n6`, from `scav.py eval`; `-` if never evaluated), `tok` (always-on context cost), `enabled`, and `desc`.
- `~/.scav/profile.md`: a deduplicated sample of what the user actually typed, from both Claude and Codex.
- `~/.scav/profile.json`: totals, top invocations, the Codex skills with no plugin counterpart (these are workflow signals: look for plugin components that serve the same job). Plugins synced from claude.ai (`<name>@synced`) are ordinary sources: absorb what's used and put the plugin in `disable` like any other.

## 2. Screen the user (LLM judgment)

Read `profile.md` in full. Write `~/.scav/screen.md`, keeping it short:
- **Tone and working style**: terse or verbose, how much they want plans or reviews, how autonomous.
- **Task categories**, ranked by share of prompts: for example research, trading or quant, backend Python, frontend, writing.
- **Stack**: the languages, frameworks and services that actually appear.
- **Recurring workflows**: sequences the user repeats, such as idea, then research, then report.

## 3. Select

Read `inventory.tsv` in chunks of 200 lines. Build `~/.scav/selection.json`:
```json
{"keep": ["skill:ecc:python-testing", "agent:ecc:planner", "hook:ecc:all", "mcp:ecc:chrome-devtools"],
 "disable": ["ecc@ecc"],
 "reasons": {"skill:ecc:python-testing": "Python stack, 14 uses"}}
```
Use these rules:
- Keep every component with `uses > 0` or a meaningful `cx`, unless it was clearly a one-off experiment.
- Keep components with `uses == 0` only when they serve a task category, stack item or workflow from `screen.md`. A high `kw` is a hint, not a reason.
- `ev` overrides usage only in one direction: drop a component whose `ev` starts with `prune` (calibrated evidence over distinct prompts that it brings no win worth its tokens, or that a cheaper look-alike scores as well), even if it has uses, and say so in step 4. `report…` is a pinned component that would be pruned, and `uncalibrated…` one whose prune is held back because the controls have not passed: keep both and mention them. `idle` (never fired on its own past tasks) is a hint for step 4, not a reason to drop. `keep`, `inconclusive`, `insufficient` and `-` change nothing.
- Drop stacks and domains absent from the profile, such as other languages, healthcare, logistics or homelab. Drop duplicates: when several plugins ship the same idea (`code-reviewer`), keep one.
- **Hooks** (`hook:<plugin>:all`) and **MCP servers** are behavior, not context. Keep them if the user relies on what they do (check `profile.md` and `top_uses`). If you drop them, list them in step 4 as "lost".
- `disable` lists the `plugin@marketplace` ids whose kept parts are now inside Scav. Include a plugin even if nothing from it is kept. Never disable a plugin whose kept components you did not take in full.
- Trials (`profile.json` `trials`, id to uses, added with `scav:find-missing-skill`): keep a trial with `uses > 0`, and leave out one with 0 uses. A trial is not pinned, so leaving it out of `keep` drops it.
- Never include Scav itself. Pinned ids (`~/.scav/pins.json`, added with `/scav add`) are kept automatically, so you need not list them. Mention them in step 4.

## 4. Confirm with the user

Show the following, in under 15 lines:
- the screen in 3 lines,
- kept count per plugin,
- the always-on token estimate before and after (`profile.json` against the sum of kept `tok`),
- the plugins to disable,
- the trials kept and dropped, with their uses,
- what is lost: dropped hooks and MCP servers.

Ask for a yes before changing anything. The user may edit the selection first.

## 5. Apply (only after a yes)

1. `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/scav.py apply ~/.scav/selection.json`. Surface any `warnings`.
2. For each id in `disable`, run `claude plugin disable <id>`.
3. `claude plugin marketplace update scav && claude plugin update scav@scav`
4. Tell the user to start a new session for the change to take effect, and that `/scav restore` undoes it.
