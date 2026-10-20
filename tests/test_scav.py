import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scav  # noqa: E402


def line(**d):
    return json.dumps(d)


def test_parser_counts_real_invocations_and_ignores_schema_text():
    uses, prompts = Counter(), []
    lines = [
        line(type="assistant", message={"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "ecc:python-testing"}},
            {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "ecc:planner"}}]}),
        line(type="user", message={"content": "<command-name>/hackathon-kit:hack-brief</command-name>"}),
        line(type="user", message={"content": "fix the flaky pytest in numerai"}, timestamp="2026-10-01T00:00:00Z"),
        # false positive: a system prompt that quotes the Skill tool schema and the <command-name> tag
        line(type="system", content='{"name":"Skill","input":{"skill":"ecc:django-tdd"}} <command-name>` block'),
        line(type="user", isMeta=True, message={"content": "<command-name>/model</command-name>"}),
        line(type="user", message={"content": [{"type": "tool_result", "content": "ok"}]}),
    ]
    for ln in lines:
        scav.parse_claude_line(ln, uses, prompts)
    assert uses == Counter({"ecc:python-testing": 1, "ecc:planner": 1, "hackathon-kit:hack-brief": 1})
    assert [p[3] for p in prompts] == ["fix the flaky pytest in numerai"]


def make_plugin(root, name, files):
    for rel, text in files.items():
        p = root / name / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root / name


def test_apply_renames_collisions_rewrites_paths_and_carries_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    cache, claude, dest = tmp_path / "cache", tmp_path / "claude", tmp_path / "scavsrc"
    a = make_plugin(cache, "a", {
        "skills/helper/SKILL.md": "---\nname: helper\ndescription: helps\n---\nrun ${CLAUDE_PLUGIN_ROOT}/x.sh then a:other\n"
                                  "see `../../references/check.md#top` and ../example-dir\n",
        "references/check.md": "checklist",
        "agents/code-reviewer.md": "---\nname: code-reviewer\ndescription: reviews\n---\n",
        "hooks/hooks.json": json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/s.js"}]}]}}),
        ".mcp.json": json.dumps({"mcpServers": {"dev": {"command": "${CLAUDE_PLUGIN_ROOT}/srv"}}}),
    })
    make_plugin(cache, "b", {"agents/code-reviewer.md": "---\nname: code-reviewer\ndescription: also reviews\n---\n"})
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {
        "a@m": [{"installPath": str(a)}], "b@m": [{"installPath": str(cache / "b")}]}}))
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"a@m": True}}))
    make_plugin(tmp_path, "scavsrc", {".claude-plugin/plugin.json": json.dumps({"name": "scav", "version": "0.1.0"})})
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"keep": ["skill:a:helper", "agent:a:code-reviewer", "agent:b:code-reviewer",
                                        "hook:a:all", "mcp:a:dev"], "disable": ["a@m", "b@m"]}))

    scav.apply(sel, claude=claude, dest=dest)

    skill = (dest / "skills/helper/SKILL.md").read_text()
    assert f"{a}/x.sh" in skill and "${CLAUDE_PLUGIN_ROOT}" not in skill
    assert "a:other" in skill  # not kept, so not renamed
    assert "`_ext/references/check.md#top`" in skill and "../example-dir" in skill
    assert (dest / "skills/helper/_ext/references/check.md").read_text() == "checklist"
    assert "name: a-code-reviewer" in (dest / "agents/a-code-reviewer.md").read_text()
    assert (dest / "agents/b-code-reviewer.md").exists()
    cmd = json.loads((dest / "hooks/hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd == f"export CLAUDE_PLUGIN_ROOT='{a}'; node {a}/s.js"
    mcp = json.loads((dest / ".mcp.json").read_text())["mcpServers"]["dev"]
    assert mcp["command"] == f"{a}/srv" and mcp["env"]["CLAUDE_PLUGIN_ROOT"] == str(a)
    assert json.loads((dest / ".claude-plugin/plugin.json").read_text())["version"] == "0.1.1"
    m = json.loads((tmp_path / "state/manifest.json").read_text())
    assert m["origin"]["scav:a-code-reviewer"] == "agent:a:code-reviewer"

    # re-apply with a smaller selection removes what is no longer kept
    sel.write_text(json.dumps({"keep": ["skill:a:helper"]}))
    scav.apply(sel, claude=claude, dest=dest)
    assert not (dest / "agents/a-code-reviewer.md").exists() and not (dest / "hooks/hooks.json").exists()
    assert (dest / "skills/helper/SKILL.md").exists()


def test_uses_for_maps_scav_alias_back_to_origin():
    c = {"id": "agent:a:code-reviewer", "plugin": "a", "name": "code-reviewer", "kind": "agent"}
    uses = Counter({"a:code-reviewer": 2, "scav:a-code-reviewer": 3})
    assert scav.uses_for(c, uses, {"scav:a-code-reviewer": "agent:a:code-reviewer"}, False) == 5


def test_codex_counts_only_model_tool_calls(tmp_path):
    f = tmp_path / "rollout.jsonl"
    f.write_text("\n".join([
        line(type="world_state", payload={"skills": "/Users/x/.codex/skills/pdf/SKILL.md"}),
        line(type="response_item", payload={"type": "custom_tool_call", "input": "cat /Users/x/.codex/skills/standards/SKILL.md"}),
        line(type="response_item", payload={"type": "function_call", "arguments": "sed -n 1,80p ~/.codex/skills/standards/SKILL.md"}),
    ]))
    assert scav.codex_skill_reads(f) == {"standards"}


def test_sample_spreads_across_weeks_and_caps_sessions():
    busy = [(f"2026-10-14T0{i}:00:00", "claude", "s1", f"busy prompt {i}") for i in range(9)]
    old = [("2026-09-01T00:00:00", "codex", "s2", "old research prompt")]
    sample, unique = scav.sample_prompts(busy + old, limit=3, per_session=4)
    assert unique == 10
    assert any("old research prompt" in s for s in sample)  # older week not crowded out
    assert len(sample) == 3


def test_bare_name_credit_and_sidechain_prompts_skipped():
    uses, prompts = Counter({"autopilot": 4}), []
    c = {"id": "skill:g:autopilot", "plugin": "g", "name": "autopilot", "kind": "skill"}
    assert scav.uses_for(c, uses, {}, True) == 4 and scav.uses_for(c, uses, {}, False) == 0
    scav.parse_claude_line(line(type="user", isSidechain=True, message={"content": "agent-written brief"}), uses, prompts)
    assert prompts == []


def test_lines_older_than_window_are_ignored():
    uses, prompts = Counter(), []
    scav.parse_claude_line(line(type="user", timestamp="2026-06-01T00:00:00Z", message={"content": "old"}),
                           uses, prompts, since="2026-09-15T00:00:00")
    scav.parse_claude_line(line(type="user", timestamp="2026-10-01T00:00:00Z", message={"content": "new"}),
                           uses, prompts, since="2026-09-15T00:00:00")
    assert [p[3] for p in prompts] == ["new"]


def test_added_components_are_pinned_through_later_selections(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    cache, claude, dest = tmp_path / "cache", tmp_path / "claude", tmp_path / "scavsrc"
    new = make_plugin(cache, "new", {"skills/charts/SKILL.md": "---\nname: charts\ndescription: draws charts\n---\n",
                                     "skills/other/SKILL.md": "---\nname: other\ndescription: unrelated\n---\n"})
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {"new@m": [{"installPath": str(new)}]}}))
    dest.mkdir()

    scav.add(["skill:new:charts"], ["new@m"], claude=claude, dest=dest)
    assert (dest / "skills/charts/SKILL.md").exists() and not (dest / "skills/other").exists()
    assert json.loads((tmp_path / "state/manifest.json").read_text())["disable"] == ["new@m"]

    # a later full /scav run that never mentions the pin still keeps it
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"keep": [], "disable": []}))
    scav.apply(sel, claude=claude, dest=dest)
    assert (dest / "skills/charts/SKILL.md").exists()

    scav.unpin(["skill:new:charts"])
    scav.apply(sel, claude=claude, dest=dest)
    assert not (dest / "skills/charts").exists()


def test_trial_adds_are_not_pinned_and_drop_when_the_next_selection_omits_them(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    cache, claude, dest = tmp_path / "cache", tmp_path / "claude", tmp_path / "scavsrc"
    new = make_plugin(cache, "new", {"skills/charts/SKILL.md": "---\nname: charts\ndescription: draws charts\n---\n",
                                     "skills/find-missing-skill/SKILL.md": "---\nname: find-missing-skill\ndescription: finds\n---\n"})
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {"new@m": [{"installPath": str(new)}]}}))
    dest.mkdir()
    manifest = lambda: json.loads((tmp_path / "state/manifest.json").read_text())

    scav.add(["skill:new:charts", "skill:new:find-missing-skill"], [], claude=claude, dest=dest, trial=True)
    assert (dest / "skills/charts/SKILL.md").exists() and scav.load_pins() == set()
    assert (dest / "skills/new-find-missing-skill").exists() and not (dest / "skills/find-missing-skill").exists()
    assert manifest()["trial"] == ["skill:new:charts", "skill:new:find-missing-skill"]

    sel = tmp_path / "state/selection.json"  # where a full /scav run writes its selection
    sel.write_text(json.dumps({"keep": ["skill:new:charts"], "disable": []}))  # kept: still a trial
    scav.apply(sel, claude=claude, dest=dest)
    assert manifest()["trial"] == ["skill:new:charts"] and not (dest / "skills/new-find-missing-skill").exists()

    scav.add(["skill:new:charts"], [], claude=claude, dest=dest)  # a plain add pins it: no longer a trial
    assert manifest()["trial"] == [] and scav.load_pins() == {"skill:new:charts"}

    scav.unpin(["skill:new:charts"])
    scav.add(["skill:new:charts"], [], claude=claude, dest=dest, trial=True)
    sel.write_text(json.dumps({"keep": [], "disable": []}))
    scav.apply(sel, claude=claude, dest=dest)
    assert not (dest / "skills/charts").exists() and manifest()["trial"] == []


def test_find_ranks_disabled_and_catalog_components_by_rare_need_words(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    cache, claude = tmp_path / "cache", tmp_path / "claude"
    a = make_plugin(cache, "a", {
        "skills/frontend-a11y/SKILL.md": "---\nname: frontend-a11y\ndescription: Accessibility patterns for forms "
                                         "and keyboard focus in frontend code\n---\n",
        "commands/epic-review.md": "---\ndescription: Mark epic review requested\n---\n",
        "agents/code-review.md": "---\ndescription: General code review of a diff\n---\n"})
    b = make_plugin(cache, "b", {"skills/charts/SKILL.md": "---\nname: charts\ndescription: draws charts\n---\n"})
    mkt = make_plugin(tmp_path, "mkt", {".claude-plugin/marketplace.json": json.dumps({"plugins": [
        {"name": "a11y-audit", "description": "Accessible frontend audits", "source": "./"},
        {"name": "a", "description": "already installed: accessibility frontend review", "source": "./"}]})})
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {
        "a@m": [{"installPath": str(a)}], "b@m": [{"installPath": str(b)}]}}))
    (claude / "plugins/known_marketplaces.json").write_text(json.dumps({
        "m": {"installLocation": str(mkt)}, "bare": {"installLocation": str(tmp_path / "nocatalog")}}))
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"b@m": True}}))
    (tmp_path / "state").mkdir()
    (tmp_path / "state/manifest.json").write_text(json.dumps({"origin": {"scav:code-review": "agent:a:code-review"}}))

    scav.find("review frontend accessibility", limit=3, claude=claude)
    rows = [r.split("\t") for r in capsys.readouterr().out.splitlines()]
    assert [r[0] for r in rows] == ["skill:a:frontend-a11y", "agent:a:code-review", "command:a:epic-review"]
    assert [r[2] for r in rows] == ["off", "in-scav", "off"]

    scav.find("review frontend accessibility", limit=5, catalogs=True, claude=claude)
    rows = [r.split("\t") for r in capsys.readouterr().out.splitlines()]
    assert ["plugin:a11y-audit@m", "a11y-audit", "not installed"] in [r[:3] for r in rows]
    assert "plugin:a@m" not in [r[0] for r in rows]  # installed plugins come from the inventory, not the catalog

    scav.find("review keyboard", limit=1, claude=claude)  # plain overlap would pick a short "review" match
    assert capsys.readouterr().out.startswith("skill:a:frontend-a11y")

    scav.find("kubernetes helm", claude=claude)
    assert capsys.readouterr().out.startswith("no component matches")


def test_fired_tasks_records_brief_args_last_prompt_and_slash(tmp_path):
    proj = tmp_path / "projects/p"
    proj.mkdir(parents=True)
    ts = "2099-01-01T00:00:00Z"
    (proj / "s.jsonl").write_text("\n".join([
        line(type="user", timestamp=ts, cwd="/r", message={"content": "turn these five rough notes into a ranked shortlist"}),
        line(type="assistant", timestamp=ts, cwd="/r", message={"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "a:idea-refine"}},
            {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "a:code-reviewer", "prompt": "review the brief"}}]}),
        line(type="user", timestamp=ts, cwd="/r", message={"content": "<command-name>/a:idea-refine</command-name><command-args>sharpen it</command-args>"}),
        line(type="user", timestamp=ts, message={"content": "ok go ahead with all of it"}),
        line(type="assistant", timestamp=ts, message={"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "a:idea-refine"}}]}),
        line(type="user", timestamp=ts, isSidechain=True, message={"content": "subagent chatter"}),
    ]))
    tasks = scav.fired_tasks(1, claude=tmp_path)
    t = lambda text, standalone, slash, cwd="/r": {"text": text, "standalone": standalone, "slash": slash, "cwd": cwd}
    assert tasks == {"a:idea-refine": [t("turn these five rough notes into a ranked shortlist", True, False),
                                       t("sharpen it", True, True), t("ok go ahead with all of it", False, False, "")],
                     "a:code-reviewer": [t("review the brief", True, False)]}


def task(text, standalone=True, cwd=""):
    return {"text": text, "standalone": standalone, "slash": False, "cwd": cwd}


def test_build_cases_skips_context_dependent_and_falls_back_to_keyword_prompts():
    c = {"name": "idea-refine", "desc": "refine raw product ideas into a ranked shortlist with risks"}
    tasks = {"a:idea-refine": [
        task("Refine these three product ideas for solo founders into a ranked shortlist"),  # self-contained
        task("Refine these three product ideas for solo founders into a ranked shortlist"),  # duplicate
        task("review the changes in /nonexistent/repo/src before merging please"),  # local path, nothing to inline
        task("look at notes/lessons-design.md and refine the product idea in it"),  # file name
        task("approving it in advance and going to sleep, finish everything", standalone=False),  # follow-up
        task("approve"),  # too short
    ]}
    prompts = [("1", "claude", "s1", "give me a ranked shortlist of product ideas with the main risks for each one"),
               ("2", "claude", "s1", "now also rank the product ideas shortlist by risks and refine them again"),  # follow-up
               ("1", "claude", "s2", "what is the weather like in tbilisi tomorrow afternoon, roughly speaking")]
    cases, skipped = scav.build_cases(c, {"a:idea-refine"}, tasks, prompts, limit=3, screen=lambda t: True)
    assert [x["source"] for x in cases] == ["fired", "kw"]
    assert skipped == 3
    assert "weather" not in json.dumps(cases) and "now also" not in json.dumps(cases)


def git_repo(path):
    run_git = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True).stdout
    path.mkdir(parents=True)
    run_git("init", "-q")
    run_git("config", "user.email", "t@t")
    run_git("config", "user.name", "t")
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    run_git("add", ".")
    run_git("commit", "-qm", "add calc")
    return run_git("rev-parse", "HEAD").strip()


def test_fixture_inlines_named_commit_diff_and_ignores_hex_words(tmp_path):
    repo = tmp_path / "repo"
    sha = git_repo(repo)
    fx = scav.fixture(f"Review commit {sha[:8]} for bugs", cwd=str(repo))
    assert "return a - b" in fx and fx.startswith(f"$ git show")
    # a hex-looking English word is not a commit
    assert scav.fixture("the defaced 1234567 pages", cwd=str(repo)) is None
    # a removed worktree's commits are found in its parent repo
    gone = repo / ".claude/worktrees/old-branch"
    assert "return a - b" in scav.fixture(f"In {gone} review commit {sha[:8]}")
    # a base or checkout hash is not the change under review, and an ellipsis is not a range
    assert scav.fixture(f"Checkout at commit {sha[:8]}... then compare against {sha[:8]}..HEAD", cwd=str(repo)) is None
    # repo-bound fired tasks become fixture cases
    cases, skipped = scav.build_cases({"name": "x", "desc": ""}, {"a:x"},
                                      {"a:x": [task(f"Review commit {sha[:8]} in this repo for bugs", cwd=str(repo))]}, [],
                                      screen=lambda t: True)
    assert [x["source"] for x in cases] == ["fixture"] and "return a - b" in cases[0]["prompt"] and skipped == 0
    # a task about the whole tree at a commit is not answerable from that commit's diff
    cases, skipped = scav.build_cases({"name": "x", "desc": ""}, {"a:x"}, {"a:x": [
        task(f"Use the checkout at commit {sha[:8]} and check layer 2 implements its spec", cwd=str(repo))]}, [],
        screen=lambda t: True)
    assert cases == [] and skipped == 1


def test_written_case_restates_request_to_judge_and_checks_firing_with_only(tmp_path):
    scav.write_case(tmp_path / "c", "do the thing", {"kind": "agent"}, "code-reviewer")
    fired = (tmp_path / "c/graders/fired.md").read_text()
    assert "tool: Agent" in fired and "arm: with-only" in fired and "subagent_type" in fired
    assert {p.stem for p in (tmp_path / "c/graders").iterdir()} == {"fired", *scav.RUBRIC}
    assert "<request>\ndo the thing\n</request>" in (tmp_path / "c/graders/done.md").read_text()
    prompt = (tmp_path / "c/prompt.md").read_text()
    assert prompt.endswith("do the thing\n") and "code-reviewer" not in prompt  # nudge names no component


def arm(score, fired=None, cost=0.1, error=None):
    graders = [{"name": "fired", "passed": fired}] if fired is not None else []
    return {"score": score, "costUsd": cost, "error": error, "graders": graders}


def test_summarize_keeps_raw_scores_per_prompt(tmp_path):
    (tmp_path / "SKILL.md").write_text("x" * 400)
    res = {"cases": [
        {"name": "fired-aaa", "arms": {"with": [arm(1, True), arm(0.5, False)], "without": [arm(0.25), arm(0.25)]}},
        {"name": "kw-bbb", "arms": {"with": [arm(0, True, error="timeout")], "without": [arm(0)]}},  # errored run dropped
    ]}
    c = {"id": "skill:a:x", "kind": "skill", "path": str(tmp_path), "tok": 30}
    r = scav.summarize(res, c, "r1", "skill:x", None, None, False)
    assert r["per_case"] == {"aaa": {"src": "fired", "fired": 1, "with": [1, 0.5], "without": [0.25, 0.25]},
                             "bbb": {"src": "kw", "fired": 0, "with": [], "without": [0]}}
    assert r["invoke_tok"] == 100 and (r["model"], r["judge"]) == ("default", "haiku") and r["fired"] == "1/2"


def mkrow(i, run, deltas, model="sonnet", judge="haiku", tok=50, group=None, explicit=False, fired=1, src="fired",
          base=0.5, offset=0.0):
    """One run; deltas[k] is prompt k's side-by-side score (rubric with = base + offset + delta)."""
    per_case = {f"p{k}": {"src": src, "fired": fired, "with": [base + offset + d], "without": [base], "pair": [d]}
                for k, d in enumerate(deltas)}
    return {"id": i, "run": run, "group": group or i, "model": model, "judge": judge, "explicit": explicit,
            "per_case": per_case, "tok": tok, "invoke_tok": 0}


CAL_OK = [mkrow("control:partial", "c1", [-0.3, -0.25, -0.4, -0.35]), mkrow("control:saboteur", "c1", [-0.5, -0.4, -0.6, -0.5]),
          mkrow("control:placebo", "c1", [0.0, 0.05, -0.05, 0.0])]


def test_stats_bounds_are_one_sided_t_over_values():
    lo, hi = scav.bounds([0.1, 0.3])
    assert round(lo, 3) == round(0.2 - 6.314 * 0.1414214 / 2 ** 0.5, 3) and round(hi - 0.2, 6) == round(0.2 - lo, 6)
    assert scav.bounds([0.2]) == (None, None)


def test_stats_reruns_of_the_same_prompts_do_not_add_evidence():
    rows = [mkrow("a", f"r{k}", [0.0, 0.0, 0.0]) for k in range(6)] + CAL_OK
    v = scav.verdicts(rows, set())["a"]
    assert v["verdict"] == "insufficient" and v["n"] == 3


def test_stats_verdicts_keep_prune_inconclusive_on_distinct_prompts():
    rows = CAL_OK + [mkrow("win", "r1", [0.3, 0.2, 0.25, 0.3]), mkrow("tie", "r1", [0.0, 0.05, -0.05, 0.0]),
                     mkrow("noisy", "r1", [0.5, -0.5, 0.5, -0.4])]
    v = scav.verdicts(rows, set())
    assert (v["win"]["verdict"], v["tie"]["verdict"], v["noisy"]["verdict"]) == ("keep", "prune", "inconclusive")
    # a component that never fired on a prompt gets no evidence from it
    v = scav.verdicts(CAL_OK + [mkrow("quiet", "r1", [0.0] * 6, fired=0, src="kw")], set())
    assert v["quiet"]["verdict"] == "insufficient"


def test_stats_latest_model_and_judge_decide():
    rows = CAL_OK + [mkrow("a", "r1", [0.0] * 4, model="haiku"), mkrow("a", "r2", [0.3, 0.2, 0.3, 0.25])]
    assert scav.verdicts(rows, set())["a"]["verdict"] == "keep"


def test_calibration_gate_blocks_prune_until_controls_pass():
    tie = mkrow("a", "r1", [0.0, 0.05, -0.05, 0.0])
    assert scav.verdicts([tie], set())["a"]["verdict"] == "uncalibrated"  # controls never run
    blind = [mkrow("control:partial", "c1", [0.0, 0.05, -0.05, 0.0])] + CAL_OK[1:]  # modest effect unseen: reported only
    assert scav.verdicts([tie] + blind, set())["a"]["verdict"] == "prune"
    assert scav.verdicts([tie] + CAL_OK[1:], set())["a"]["verdict"] == "prune"  # partial never run: still calibrated
    biased = CAL_OK[:2] + [mkrow("control:placebo", "c1", [0.3, 0.25, 0.4, 0.35])]  # placebo "wins": judge is biased
    assert scav.verdicts([tie] + biased, set())["a"]["verdict"] == "uncalibrated"
    no_loss = CAL_OK[:1] + [mkrow("control:saboteur", "c1", [0.0, 0.05, -0.05, 0.0])] + CAL_OK[2:]  # can't see a loss
    assert scav.verdicts([tie] + no_loss, set())["a"]["verdict"] == "uncalibrated"
    other_judge = [{**r, "judge": "sonnet"} for r in CAL_OK]
    assert scav.verdicts([tie] + other_judge, set())["a"]["verdict"] == "uncalibrated"
    assert scav.verdicts([tie] + CAL_OK, set())["a"]["verdict"] == "prune"
    ok, why = scav.calibration(CAL_OK, "sonnet", "haiku")
    assert ok and "power limit: partial -0.33" in why and why.startswith("saboteur -0.50")


def test_calibration_pinned_prune_is_reported():
    v = scav.verdicts(CAL_OK + [mkrow("a", "r1", [-0.3, -0.2, -0.3, -0.25])], {"a"})
    assert v["a"]["verdict"] == "report"


def test_idle_flags_components_that_never_fire_on_their_own_tasks():
    quiet = lambda **k: [mkrow("a", "r1", [0.0, 0.0], fired=0, **k), mkrow("a", "r2", [0.0, 0.0], fired=0, **k)]
    assert scav.verdicts(quiet(), set())["a"]["verdict"] == "idle"
    assert scav.verdicts(quiet(explicit=True), set())["a"]["verdict"] == "insufficient"  # called by slash on purpose
    assert scav.verdicts(quiet(src="kw"), set())["a"]["verdict"] == "insufficient"  # keyword prompts aren't its tasks


def test_lookalike_cheaper_and_as_good_wins_when_calibrated():
    rows = CAL_OK + [mkrow("x:big", "r1", [0.3, 0.25, 0.3, 0.35], tok=300, group="g"),
                     mkrow("x:small", "r1", [0.3, 0.2, 0.3, 0.3], tok=60, group="g")]
    v = scav.verdicts(rows, set())
    assert v["x:small"]["verdict"] == "keep" and v["x:big"]["verdict"] == "prune" and "x:small" in v["x:big"]["why"]
    # clearly better and dearer stays
    rows = CAL_OK + [mkrow("x:big", "r1", [0.5, 0.45, 0.5, 0.55], tok=300, group="g"),
                     mkrow("x:small", "r1", [0.1, 0.05, 0.15, 0.1], tok=60, group="g")]
    assert scav.verdicts(rows, set())["x:big"]["verdict"] == "keep"


def test_eval_column_shows_verdict_delta_and_prompt_count():
    col = scav.eval_column(CAL_OK + [mkrow("a", "r1", [0.3, 0.2, 0.3, 0.2])], set())
    assert col == {"a": "keep+0.25/n4"}


def test_bare_names_count_user_own_skills(tmp_path):
    (tmp_path / "skills/execute-plan").mkdir(parents=True)
    (tmp_path / "skills/execute-plan/SKILL.md").write_text("x")
    comps = [{"name": "execute-plan"}, {"name": "hack-ideas"}]
    bare = scav.bare_names(comps, claude=tmp_path)
    assert bare["execute-plan"] == 2 and bare["hack-ideas"] == 1


def judged(answer, fired=True):
    return {"score": 0.5, "costUsd": 0.1, "error": None, "graders": [
        {"name": "fired", "passed": fired, "evidence": ""}, {"name": "done", "evidence": answer}]}


def test_pairwise_swaps_order_so_position_bias_cancels():
    res = {"cases": [{"name": "cal-k1", "promptMarkdown": "name a prime above 10",
                      "arms": {"with": [judged("11"), judged("13", fired=False)], "without": [judged("primes"), judged("x")]}}]}
    seen = []
    def prefers_11(request, a, b, judge):
        seen.append((request, a, b))
        return "A" if a == "11" else "B"
    assert scav.pairwise(res, "haiku", ask=prefers_11) == {"k1": [1.0]}  # the unfired second run is not judged
    assert seen == [("name a prime above 10", "11", "primes"), ("name a prime above 10", "primes", "11")]
    assert scav.pairwise(res, "haiku", ask=lambda *a: "A") == {"k1": [0.0]}  # always-first judge: no signal
    assert scav.pairwise(res, "haiku", ask=lambda *a: "B") == {"k1": [0.0]}


def test_stats_pairwise_score_alone_decides():
    rows = [mkrow("a", "r1", [1.0, 0.5, 1.0, 0.5], base=0.5, offset=-0.75)]  # rubric says with loses
    v = scav.verdicts(rows, set())["a"]
    assert v["verdict"] == "keep" and v["delta"] == 0.75
    # a fired prompt with no side-by-side score (budget cut its without arm) adds nothing, not a rubric delta
    rows[0]["per_case"]["p9"] = {"src": "fired", "fired": 1, "with": [0.0], "without": [1.0]}
    assert scav.verdicts(rows, set())["a"]["n"] == 4


def test_answerable_screen_drops_prompts_that_need_unseen_context(tmp_path, monkeypatch):
    c = {"name": "pricing", "desc": "pricing strategy for plans"}
    tasks = {"a:pricing": [task("given our current work, what pricing plans should we offer to the users here?"),
                           task("compare freemium and usage-based pricing plans for a solo developer tool")]}
    needs_context = lambda t: "our current work" not in t
    cases, skipped = scav.build_cases(c, {"a:pricing"}, tasks, [], screen=needs_context)
    assert [x["prompt"][:7] for x in cases] == ["compare"] and skipped == 1
    # calibration takes the first answerable prompts of a fixed shuffle, and keeps that sample
    monkeypatch.setattr(scav, "STATE", tmp_path)
    openers = [("1", "claude", f"s{k}", f"prompt number {k} " + "x" * 80) for k in range(8)]
    seen = []
    picks = scav.calibration_prompts(openers, 3, screen=lambda t: seen.append(t) or int(t.split()[2]) % 2 == 0)
    assert len(picks) == 3 and len(seen) < 8  # stops screening once it has enough and all(int(p.split()[2]) % 2 == 0 for p in picks)
    newer = openers + [("0", "claude", f"n{k}", f"prompt number {k + 10} " + "x" * 80) for k in range(8)]
    assert picks == scav.calibration_prompts(newer, 3, screen=lambda t: int(t.split()[2]) % 2 == 0)  # new sessions
    assert scav.calibration_prompts(newer, 4, screen=lambda t: True)[:3] == picks  # growing n extends, keeps order
    # the verdict is cached per prompt, and a failed screen call is not cached
    monkeypatch.setattr(scav, "STATE", tmp_path)
    calls = []
    monkeypatch.setattr(scav, "claude_json", lambda *a: calls.append(a) or {"answerable": True})
    assert scav.answerable("q") and scav.answerable("q") and len(calls) == 1
    monkeypatch.setattr(scav, "claude_json", lambda *a: None)
    assert not scav.answerable("other") and "other" not in (tmp_path / "evals/answerable.json").read_text()


def test_hooks_and_mcp_declared_in_plugin_json_are_found_and_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    cache, claude, dest = tmp_path / "cache", tmp_path / "claude", tmp_path / "scavsrc"
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/h.js"}]}]}}
    p = make_plugin(cache, "p", {".claude-plugin/plugin.json": json.dumps({"name": "p", "hooks": "./hooks/custom.json",
                                                                         "mcpServers": {"srv": {"command": "${CLAUDE_PLUGIN_ROOT}/s"}}}),
                                 "hooks/custom.json": json.dumps(hook)})
    q = make_plugin(cache, "q", {".claude-plugin/plugin.json": json.dumps({"name": "q", "hooks": hook["hooks"]})})  # inline
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {
        "p@m": [{"installPath": str(p)}], "q@m": [{"installPath": str(q)}]}}))
    dest.mkdir()
    ids = {c["id"]: c for c in scav.inventory(claude)}
    assert {"hook:p:all", "mcp:p:srv", "hook:q:all"} <= ids.keys()
    assert ids["hook:p:all"]["path"] == str(p / "hooks/custom.json") and "SessionStart" in ids["hook:q:all"]["desc"]
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"keep": ["hook:p:all", "mcp:p:srv"]}))
    scav.apply(sel, claude=claude, dest=dest)
    cmd = json.loads((dest / "hooks/hooks.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cmd == f"export CLAUDE_PLUGIN_ROOT='{p}'; node {p}/h.js"
    assert json.loads((dest / ".mcp.json").read_text())["mcpServers"]["srv"]["command"] == f"{p}/s"
    # held hooks and servers are in origin, so find shows them in-scav and eval picks them by default
    origin = json.loads((tmp_path / "state/manifest.json").read_text())["origin"]
    assert origin == {"hooks.json:p": "hook:p:all", "mcp__plugin_scav_srv": "mcp:p:srv"}


def test_stats_history_only_pools_rows_from_the_current_method(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path)
    (tmp_path / "evals").mkdir()
    old, new = mkrow("a", "r1", [0.0]), {**mkrow("a", "r2", [0.0]), "v": scav.EVAL_VERSION}
    (tmp_path / "evals/history.jsonl").write_text(json.dumps({**old, "pairwise": True}) + "\n" + json.dumps(new) + "\n")
    assert [r["run"] for r in scav.eval_history()] == ["r2"]


def test_versus_placebo_swaps_the_baseline_and_splits_the_placebo_against_itself():
    comp = {"cases": [{"name": "cal-k1", "arms": {"with": [arm(1, True)]}}]}
    placebo = {"cases": [{"name": "cal-k1", "arms": {"with": [arm(0.5, False), arm(0.25, True), arm(0.75, True)]}}]}
    v = scav.versus_placebo(comp, placebo, 1)["cases"][0]["arms"]
    assert [r["score"] for r in v["with"]] == [1] and [r["score"] for r in v["without"]] == [0.25]  # uninvoked dropped
    own = scav.versus_placebo(placebo, placebo, 1, own=True)["cases"][0]["arms"]
    assert [r["score"] for r in own["with"]] == [0.5] and [r["score"] for r in own["without"]] == [0.25]
    assert scav.versus_placebo(comp, {"cases": []}, 1)["cases"][0]["arms"]["without"] == []  # no placebo, no pair


def test_github_entries_keep_real_skill_files_and_read_their_descriptions(monkeypatch):
    import base64 as b64
    calls = []

    def fake_gh(args):
        calls.append(args)
        if args[:2] == ["search", "code"]:
            return [{"repository": {"nameWithOwner": "o/r"}, "path": "skills/tf/SKILL.md"},
                    {"repository": {"nameWithOwner": "o/r"}, "path": "skills/terraform-skill.md"},
                    {"repository": {"nameWithOwner": "o/r"}, "path": "skills/tf/SKILL.md"}]
        if "/contents/" in args[1]:  # every file is readable, so only the filename filter can drop the decoy
            md = "---\nname: tf\ndescription: Review Terraform plans\n---\nbody"
            return {"content": b64.b64encode(md.encode()).decode()}
        return {"stargazers_count": 9, "pushed_at": "2026-10-01T00:00:00Z"}

    monkeypatch.setattr(scav, "gh_json", fake_gh)
    rows = scav.github_entries("review terraform plans", limit=5)
    assert [r["id"] for r in rows] == ["gh:o/r:skills/tf/SKILL.md"]
    assert rows[0]["desc"] == "Review Terraform plans" and rows[0]["status"] == "github ★9 pushed 2026-10-01"
    assert "filename:SKILL.md" in calls[0] and "terraform" in calls[0]


def test_fetched_repo_lists_skills_but_never_hooks_or_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path)
    clone = make_plugin(tmp_path / "github", "o__tools", {
        "SKILL.md": "---\nname: tools\ndescription: root skill\n---\n",
        "skills/lint/SKILL.md": "---\nname: lint\ndescription: lints\n---\n",
        "deep/nested/fmt/SKILL.md": "---\nname: fmt\ndescription: formats\n---\n",
        "hooks/hooks.json": json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]}}),
        ".mcp.json": json.dumps({"mcpServers": {"evil": {"command": "y"}}}),
    })
    ids = sorted(c["id"] for c in scav.github_components())
    assert ids == ["skill:gh-o-tools:fmt", "skill:gh-o-tools:lint", "skill:gh-o-tools:tools"]
    assert all(c["pid"] == "github:o/tools" and not c["enabled"] for c in scav.github_components())
    assert clone.exists()


def test_fetch_rejects_anything_but_owner_repo():
    import pytest
    for bad in ["o/r; rm -rf ~", "../etc", "https://evil.com/o/r", "o"]:
        with pytest.raises(SystemExit):
            scav.fetch(bad)


def test_placebo_baseline_reuses_earlier_runs_on_the_same_model(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path)
    cases = [{"prompt": "plan a trip", "source": "cal"}]
    k = scav.case_key("plan a trip")
    def stored(run, model, n):
        f = tmp_path / f"evals/{run}/placebo-0/result.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"suite": {"modelOverride": model}, "cases": [
            {"name": f"cal-{k}", "promptMarkdown": "plan a trip", "arms": {"with": [arm(0.5, True)] * n + [arm(0, False)]}}]}))
    stored("r1", "sonnet", 2)
    stored("r2", "haiku", 5)  # another model's answers are not reused
    asked = []
    fresh = lambda n: asked.append(n) or ({"cases": [{"name": f"cal-{k}", "arms": {"with": [arm(1, True)] * n}}]}, "")
    base, _ = scav.placebo_baseline(cases, 3, "sonnet", fresh)
    assert asked == [1] and len(base["cases"][0]["arms"]["with"]) == 3  # 2 reused + 1 new; the uninvoked run dropped
    assert base["cases"][0]["promptMarkdown"] == "plan a trip"
    asked.clear()
    scav.placebo_baseline(cases, 2, "sonnet", fresh)
    assert asked == []  # enough on disk: nothing runs


def test_inventory_counts_only_held_fetched_components_but_find_sees_all(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    claude = tmp_path / "claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {}}))
    make_plugin(tmp_path / "state/github", "o__big", {
        f"skills/{n}/SKILL.md": f"---\nname: {n}\ndescription: {n} widget helper\n---\n" for n in ("kept", "pinned", "other")})
    (tmp_path / "state/manifest.json").write_text(json.dumps({"origin": {"scav:kept": "skill:gh-o-big:kept"}}))
    scav.save_pins({"skill:gh-o-big:pinned"})

    held = sorted(c["id"] for c in scav.inventory(claude))
    assert held == ["skill:gh-o-big:kept", "skill:gh-o-big:pinned"]
    assert len(scav.inventory(claude, all_fetched=True)) == 3
    scav.find("widget helper", limit=10, claude=claude)
    assert "skill:gh-o-big:other" in capsys.readouterr().out


def test_synced_plugins_are_inventory_sources_on_unless_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    claude = tmp_path / "claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {}}))
    for name in ("used", "off"):
        make_plugin(claude / "plugins/synced/bucket", name, {
            ".claude-plugin/plugin.json": json.dumps({"name": name}),
            "skills/s/SKILL.md": f"---\nname: s\ndescription: {name} skill\n---\n"})
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"off@synced": False}}))
    rows = {c["id"]: c for c in scav.inventory(claude)}
    assert rows["skill:used:s"]["pid"] == "used@synced" and rows["skill:used:s"]["enabled"]
    assert not rows["skill:off:s"]["enabled"]


def mcp_and_hook_plugins(tmp_path):
    """An installed browser MCP plugin, a guard-hook plugin and a weak-overlap skill, plus a claude dir listing them."""
    cache, claude = tmp_path / "cache", tmp_path / "claude"
    pw = make_plugin(cache, "pw", {
        ".claude-plugin/plugin.json": json.dumps({"name": "pw", "description": "Drive a real browser for end-to-end tests"}),
        ".mcp.json": json.dumps({"mcpServers": {"browser": {"command": "npx", "args": ["@acme/browser-mcp@1.2"]}}}),
        "README.md": "# pw\n\nThe `browser` server takes screenshots and fills forms.\n\nUnrelated install notes.\n"})
    guard = make_plugin(cache, "guard", {
        ".claude-plugin/plugin.json": json.dumps({"name": "guard", "description": "Safety rails for git"}),
        "hooks/hooks.json": json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/no-force.sh", "statusMessage": "Checking push..."}]}]}}),
        "hooks/no-force.sh": "#!/bin/sh\n# Refuse git push --force to protected branches\nexit 0\n"})
    other = make_plugin(cache, "other", {"skills/mockups/SKILL.md": "---\nname: mockups\ndescription: Design mockups from "
                                         "screenshots, sketches, wireframes, moodboards, palettes and type scales\n---\n"})
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {
        "pw@m": [{"installPath": str(pw)}], "guard@m": [{"installPath": str(guard)}], "other@m": [{"installPath": str(other)}]}}))
    return claude, pw, guard


def test_find_ranks_mcp_servers_and_hooks_by_what_they_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    claude, pw, guard = mcp_and_hook_plugins(tmp_path)
    rows = {c["id"]: c for c in scav.inventory(claude)}
    mcp, hook = rows["mcp:pw:browser"]["desc"], rows["hook:guard:all"]["desc"]
    assert mcp.startswith("MCP server browser (runs npx @acme/browser-mcp@1.2): Drive a real browser")
    assert "takes screenshots and fills forms" in mcp and "install notes" not in mcp  # only README lines naming it
    assert "PreToolUse (Bash)" in hook and "Checking push..." in hook and "no-force.sh" in hook
    assert "Refuse git push --force" in hook and "Safety rails for git" in hook

    scav.find("browser screenshots forms", limit=2, claude=claude)
    assert [r.split("\t")[0] for r in capsys.readouterr().out.splitlines()] == ["mcp:pw:browser", "skill:other:mockups"]
    scav.find("refuse force push", limit=1, claude=claude)
    assert capsys.readouterr().out.startswith("hook:guard:all")


def test_mcp_calls_and_acting_hooks_count_as_uses_by_plugin_and_scav_names(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    claude, pw, guard = mcp_and_hook_plugins(tmp_path)
    att = lambda kind, event, **a: line(type="attachment", attachment={"type": kind, "hookEvent": event, **a})
    ingested = scav.ingested_command("${CLAUDE_PLUGIN_ROOT}/hooks/no-force.sh", str(guard))
    uses, prompts = Counter(), []
    for ln in [
        line(type="assistant", message={"content": [
            {"type": "tool_use", "name": "mcp__plugin_pw_browser__take_screenshot", "input": {}},
            {"type": "tool_use", "name": "mcp__plugin_scav_browser__click", "input": {}}]}),
        att("hook_success", "PreToolUse", command="Checking push...", stdout=""),  # ran, did nothing
        att("hook_success", "PreToolUse", command=ingested, stdout="note"),
        att("hook_blocking_error", "PreToolUse", blockingError={"blockingError": "no", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/no-force.sh"}),
        att("hook_blocking_error", "Stop", blockingError="a bare string names no command"),
    ]:
        scav.parse_claude_line(ln, uses, prompts)
    rows = {c["id"]: c for c in scav.inventory(claude)}
    origin = {"mcp__plugin_scav_browser": "mcp:pw:browser"}
    assert scav.uses_for(rows["mcp:pw:browser"], uses, origin, True) == 2
    assert scav.uses_for(rows["hook:guard:all"], uses, origin, True) == 2  # output + block; a quiet run is not a use


def test_hook_report_counts_outcomes_and_names_plugins_sharing_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    claude, pw, guard = mcp_and_hook_plugins(tmp_path)
    twin = make_plugin(tmp_path / "cache", "twin", {"hooks/hooks.json": (guard / "hooks/hooks.json").read_text()})
    rows = {c["id"]: c for c in scav.inventory(claude)}
    rows["hook:twin:all"] = scav.scan_plugin("twin", "twin@m", twin, False)[0]
    key = "hook:PreToolUse:Checking push..."
    tasks = {key: [{"hook": "ran", "session": "s1"}, {"hook": "blocked", "session": "s1"}, {"hook": "error", "session": "s2"}]}
    assert scav.hook_report(rows["hook:guard:all"], tasks, rows.values()) == {
        "ran": 1, "blocked": 1, "error": 1, "sessions": 2, "shared_with": ["hook:twin:all"]}


def test_github_lists_mcp_and_hook_plugins_as_listing_only_rows(monkeypatch):
    import base64 as b64
    files = {
        ("o/p", ".claude-plugin/plugin.json"): json.dumps({"name": "p", "description": "Browser tools",
                                                          "mcpServers": "./servers.json"}),
        ("o/p", "servers.json"): json.dumps({"mcpServers": {"web": {"command": "npx", "args": ["web-mcp"]}}}),
        ("o/p", "hooks/hooks.json"): json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "./check.sh"}]}]}}),
        ("o/q", ".mcp.json"): json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://x.dev/mcp"}}}),
    }
    def fake_gh(args):
        if args[:2] == ["search", "code"]:
            if "filename:plugin.json" in args:
                return [{"repository": {"nameWithOwner": "o/p"}, "path": ".claude-plugin/plugin.json"}]
            if "filename:.mcp.json" in args:
                return [{"repository": {"nameWithOwner": "o/q"}, "path": ".mcp.json"},
                        {"repository": {"nameWithOwner": "o/z"}, "path": ".mcp.json.archived"}]
            return []
        if "/contents/" in args[1]:
            repo, path = args[1][len("repos/"):].split("/contents/")
            if (repo, path) not in files:
                raise RuntimeError("HTTP 404")
            return {"content": b64.b64encode(files[(repo, path)].encode()).decode()}
        return {"stargazers_count": 3, "pushed_at": "2026-10-01T00:00:00Z"}
    monkeypatch.setattr(scav, "gh_json", fake_gh)
    rows = {r["id"]: r for r in scav.github_entries("browser tools", limit=5)}
    assert set(rows) == {"gh:o/p:servers.json#mcp", "gh:o/p:hooks/hooks.json#hook", "gh:o/q:.mcp.json#mcp"}
    assert rows["gh:o/p:servers.json#mcp"]["desc"].startswith("runs web: npx web-mcp. Browser tools")
    assert rows["gh:o/p:hooks/hooks.json#hook"]["desc"].startswith("runs ./check.sh. hooks on Stop")
    assert rows["gh:o/q:.mcp.json#mcp"]["desc"].startswith("runs remote: https://x.dev/mcp")
    assert all(r["status"] == "github ★3 pushed 2026-10-01, listing only" for r in rows.values())


def test_mcp_case_checks_a_real_tool_call_and_shows_the_judge_its_tool_results(tmp_path):
    scav.write_case(tmp_path / "c", "look it up", {"kind": "mcp"}, "browser")
    g = tmp_path / "c/graders"
    assert {p.stem for p in g.iterdir()} == {"fired", "up", "trace", *scav.RUBRIC}
    assert "target: trace" in (g / "fired.md").read_text() and '"name":"mcp__plugin_scav-eval_browser__' in (g / "fired.md").read_text()
    assert '"status":"connected"' in (g / "up.md").read_text() and "focus: trace" in (g / "trace.md").read_text()
    trace = "\n".join([json.dumps({"type": "system", "tools": ["mcp__plugin_scav-eval_browser__shot"]}),
                       json.dumps({"type": "user", "message": {"content": [
                           {"type": "tool_result", "content": [{"type": "text", "text": "title: Acme"}]}]}}), "not json"])
    run = {"score": 1, "costUsd": 0.1, "error": None, "graders": [
        {"name": "done", "evidence": "It is Acme."}, {"name": "fired", "passed": True}, {"name": "trace", "evidence": trace}]}
    seen = []
    res = {"cases": [{"name": "fired-k", "promptMarkdown": "q", "arms": {"with": [run], "without": [judged("No idea.")]}}]}
    scav.pairwise(res, "sonnet", ask=lambda r, a, b, j: seen.append(a) or "A")
    assert seen[0] == "It is Acme.\n\n<tool_results_it_saw>\ntitle: Acme\n</tool_results_it_saw>" and seen[1] == "No idea."


def test_eval_mcp_needs_run_servers_and_writes_no_row_when_the_server_never_connected(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scav, "STATE", tmp_path / "state")
    claude, pw, guard = mcp_and_hook_plugins(tmp_path)
    (claude / "projects/p").mkdir(parents=True)
    ask = "Take a screenshot of the example.com landing page and describe its header"
    (claude / "projects/p/s.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "timestamp": "2026-10-20T00:00:00Z", "sessionId": "s", "message": {"content": ask}},
        {"type": "assistant", "timestamp": "2026-10-20T00:00:01Z", "message": {"content": [
            {"type": "tool_use", "name": "mcp__plugin_pw_browser__take_screenshot", "input": {}}]}}]) + "\n")
    monkeypatch.setattr(scav, "answerable", lambda t, model="sonnet", tools="": "screenshots" in tools)
    monkeypatch.setattr(scav, "claude_json", lambda sys_, text, *a: {"better": "A" if "<answer_A>\na\n" in text else "B"})
    up = []
    def fake_run(cmd, **kw):
        if cmd[:3] == ["claude", "plugin", "eval"]:
            assert "--allow-real-servers" in cmd and "mcp__plugin_scav-eval_browser__*" in cmd
            cases = [{"name": p.name, "promptMarkdown": ask, "arms": {
                "with": [{"score": 1, "costUsd": 0.1, "error": None, "graders": [
                    {"name": "done", "evidence": "a"}, {"name": "fired", "passed": True}, {"name": "up", "passed": bool(up)}]}],
                "without": [judged("b")]}} for p in (Path(cmd[3]) / "evals").iterdir()]
            Path(cmd[cmd.index("--json") + 1]).write_text(json.dumps({"costUsd": 0.2, "cases": cases}))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(scav.subprocess, "run", fake_run)
    run = lambda **kw: scav.eval_components(["mcp:pw:browser", "hook:guard:all"], 1.0, days=3650, claude=claude, **kw) \
        or json.loads(capsys.readouterr().out)

    out = run()
    assert out["notes"]["mcp:pw:browser"].startswith("unevaluated: --run-servers starts") and "npx @acme/browser-mcp" in out["notes"]["mcp:pw:browser"]
    assert out["notes"]["hook:guard:all"].startswith("no verdict") and out["hooks"]["hook:guard:all"] == {"sessions": 0}
    out = run(run_servers=True)
    assert "never connected" in out["notes"]["mcp:pw:browser"] and not scav.eval_history()
    up.append(1)
    out = run(run_servers=True)
    row = scav.eval_history()[-1]
    assert (row["baseline"], row["judge"], row["fired"]) == ("no-server", "sonnet", "1/1")
    assert list(row["per_case"].values())[0]["pair"] == [1.0]
