# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Scav: profile Claude + Codex usage, then rebuild the Scav plugin from the components you use.

  scav.py profile [--days N]     writes ~/.scav/{inventory.tsv,profile.md,profile.json}
  scav.py apply SELECTION.json   ingests kept components into the Scav plugin source dir
  scav.py restore                removes ingested components, re-enables the absorbed plugins
  scav.py list PLUGIN            one plugin's components (for /scav add)
  scav.py add ID... [--disable P] pins ids, merges them into the selection, applies
  scav.py add --trial ID...      same, unpinned: the next /scav run drops them if unused
  scav.py find "NEED" [--catalogs] ranks installed components (and uninstalled marketplace plugins) against a need
  scav.py find "NEED" --github   also searches SKILL.md files on GitHub (read-only)
  scav.py fetch OWNER/REPO       shallow-clones a repo into ~/.scav/github; its skills become trial-addable
  scav.py unpin ID...            lets later /scav runs drop them again
  scav.py eval [--component ID]  scores components against vanilla Claude; history in ~/.scav/evals/
  scav.py eval --calibrate       runs the controls that must pass before prune verdicts count
"""
import argparse, base64, hashlib, json, math, os, random, re, shutil, statistics, subprocess, sys, time
from collections import Counter
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
CODEX = HOME / ".codex"
STATE = HOME / ".scav"
SELF = "scav"
RESERVED = {SELF, "find-missing-skill"}  # Scav's own command and skill (skills-src/); ingested look-alikes get a plugin prefix
KINDS = ("skill", "agent", "command")


# ---------- inventory ----------

def frontmatter(text):
    if not text.startswith("---"):
        return {}
    out, key = {}, None
    for line in text.split("\n")[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            out[key] = "" if val in (">", "|", ">-", "|-") else val.strip("\"'")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def installed_plugins(claude=CLAUDE):
    """[(plugin, plugin@marketplace, root, enabled)] for every installed plugin except Scav, plus the plugins synced
    from claude.ai (`<name>@synced`). Those load unless settings say false, and `claude plugin disable` turns them off
    locally (probed 2026-10-19), so they are ordinary sources Scav can absorb."""
    data = json.loads((claude / "plugins/installed_plugins.json").read_text())["plugins"]
    try:
        enabled = json.loads((claude / "settings.json").read_text()).get("enabledPlugins", {})
    except FileNotFoundError:
        enabled = {}
    out = []
    for pid, entries in data.items():
        name = pid.split("@")[0]
        root = Path(entries[0]["installPath"])
        if name != SELF and root.is_dir():
            out.append((name, pid, root, bool(enabled.get(pid))))
    for pj in sorted((claude / "plugins/synced").glob("*/*/.claude-plugin/plugin.json")):
        try:
            name = json.loads(pj.read_text())["name"]
        except (ValueError, KeyError):
            continue
        pid = f"{name}@synced"
        out.append((name, pid, pj.parent.parent, enabled.get(pid, True) is not False))
    return out


def plugin_config(root, key, default):
    """(mapping, file) for a plugin's hooks ({event: groups}) or MCP servers ({name: config}). plugin.json's `key` wins:
    a path relative to the plugin root, or an inline object. Otherwise the conventional file, if any."""
    manifest = root / ".claude-plugin/plugin.json"
    try:
        val, src = json.loads(manifest.read_text()).get(key), manifest
    except (FileNotFoundError, ValueError):
        val, src = None, manifest
    if isinstance(val, str):
        src = root / val
        val = json.loads(src.read_text()) if src.exists() else {}
    elif val is None:
        src = root / default
        val = json.loads(src.read_text()) if src.exists() else {}
    # a file wraps the mapping in {"hooks": ...} / {"mcpServers": ...}; an inline object may not
    return (val.get(key, val) if isinstance(val, dict) else {}), src


def scan_plugin(name, pid, root, enabled):
    comps = []
    def add(kind, cname, path, desc, tok=None):
        comps.append({"id": f"{kind}:{name}:{cname}", "kind": kind, "plugin": name, "pid": pid,
                      "name": cname, "path": str(path), "root": str(root), "enabled": enabled,
                      "desc": desc, "tok": max(1, (len(cname) + len(desc)) // 4) if tok is None else tok})
    for f in sorted(root.glob("skills/*/SKILL.md")):
        add("skill", f.parent.name, f.parent, frontmatter(f.read_text(errors="ignore")).get("description", ""))
    for kind in ("agent", "command"):
        for f in sorted(root.glob(f"{kind}s/*.md")):
            add(kind, f.stem, f, frontmatter(f.read_text(errors="ignore")).get("description", ""))
    events, src = plugin_config(root, "hooks", "hooks/hooks.json")
    if events:
        add("hook", "all", src, "lifecycle hooks: " + ", ".join(events), tok=0)
    servers, src = plugin_config(root, "mcpServers", ".mcp.json")
    for srv in servers:
        add("mcp", srv, src, "MCP server", tok=0)
    return comps


def inventory(claude=CLAUDE, all_fetched=False):
    """Installed components, plus fetched-repo ones. A fetched repo can hold a thousand skills, so by default only the
    ones Scav holds (kept or trial) or pins are counted; find, add, apply and list pass all_fetched to see the rest."""
    fetched = github_components()
    if not all_fetched:
        held = set(load_manifest().get("origin", {}).values()) | load_pins()
        fetched = [c for c in fetched if c["id"] in held]
    return [c for p in installed_plugins(claude) for c in scan_plugin(*p)] + fetched


GITHUB = "github"  # ~/.scav/github/<owner>__<repo>: shallow clones made by `fetch`
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def github_components():
    """Skills, agents and commands of fetched GitHub repos, as inventory rows. Hooks and MCP servers are left out:
    they run code as the user on every session, so an unreviewed repo never gets that far."""
    comps = []
    for d in sorted((STATE / GITHUB).glob("*__*")):
        owner, repo = d.name.split("__", 1)
        name = f"gh-{owner}-{repo}".lower()
        rows = [c for c in scan_plugin(name, f"github:{owner}/{repo}", d, False) if c["kind"] in KINDS]
        seen = {c["path"] for c in rows}
        for f in sorted(d.rglob("SKILL.md")):  # bare skill repos keep SKILL.md anywhere, not only skills/*/
            if ".git" in f.parts or str(f.parent) in seen:
                continue
            desc = frontmatter(f.read_text(errors="ignore")).get("description", "")
            cname = f.parent.name if f.parent != d else repo.lower()
            rows.append({"id": f"skill:{name}:{cname}", "kind": "skill", "plugin": name, "pid": f"github:{owner}/{repo}",
                         "name": cname, "path": str(f.parent), "root": str(d), "enabled": False, "desc": desc,
                         "tok": max(1, (len(cname) + len(desc)) // 4)})
        comps += rows
    return comps


def gh_json(args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(out.stderr.strip() or f"gh {args[0]} failed")
    return json.loads(out.stdout)


def github_entries(need, limit=8):
    """SKILL.md files on GitHub matching the need, as find rows. Read-only: nothing is cloned or installed.
    Each hit's own description is read from its SKILL.md frontmatter; stars and last push are shown as trust signals."""
    words = sorted(set(WORD.findall(need.lower())) - STOP)
    if not words:
        return []
    hits = gh_json(["search", "code", *words, "filename:SKILL.md", "--limit", str(limit * 3),
                    "--json", "repository,path"])
    rows, repos = [], {}
    for h in hits:
        repo, path = h["repository"]["nameWithOwner"], h["path"]
        # filename: matches loosely (terraform-skill.md); only a real SKILL.md is loadable after fetch
        if len(rows) == limit * 2 or Path(path).name != "SKILL.md" or any(r["id"] == f"gh:{repo}:{path}" for r in rows):
            continue
        try:
            raw = gh_json(["api", f"repos/{repo}/contents/{path}"])
            text = base64.b64decode(raw["content"]).decode(errors="ignore")
            if repo not in repos:
                meta = gh_json(["api", f"repos/{repo}"])
                repos[repo] = f"github ★{meta['stargazers_count']} pushed {meta['pushed_at'][:10]}"
        except (RuntimeError, KeyError, ValueError):
            continue
        fm = frontmatter(text)
        cname = fm.get("name") or Path(path).parent.name or repo.split("/")[1]
        rows.append({"id": f"gh:{repo}:{path}", "kind": "skill", "plugin": repo, "name": cname,
                     "desc": fm.get("description", ""), "tok": "-", "status": repos[repo]})
    return rows


def fetch(repo):
    """Shallow-clone a GitHub repo into ~/.scav/github so its components can be listed and trial-added."""
    if not REPO_RE.match(repo):
        sys.exit(f"not an owner/repo: {repo}")
    dest = STATE / GITHUB / repo.replace("/", "__")
    if dest.exists():
        cmd = ["git", "-C", str(dest), "pull", "--ff-only", "--depth", "1"]
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1", f"https://github.com/{repo}.git", str(dest)]
    if subprocess.run(cmd).returncode:
        sys.exit(f"fetch failed: {repo}")
    for c in github_components():
        if c["pid"] == f"github:{repo}":
            print(f"{c['id']}\t{c['tok']}\t{c['path']}\t{c['desc'][:200]}")


# ---------- usage ----------

CMD_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
CODEX_SKILL_RE = re.compile(r"/skills/([\w.-]+)/SKILL\.md")


def user_text(d):
    """What the user typed on a main-thread transcript line, else ''."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary") or d.get("isSidechain"):
        return ""
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, str) else " ".join(
        i.get("text", "") for i in (content or []) if isinstance(i, dict) and i.get("type") == "text")


def parse_claude_line(line, uses, prompts, since=""):
    """Count Skill/Agent/slash-command invocations and collect user-typed prompts from one transcript line."""
    if '"tool_use"' not in line and '"user"' not in line:
        return  # cheap prefilter; most lines are tool output
    try:
        d = json.loads(line)
    except ValueError:
        return
    if d.get("timestamp", "") < since:
        return  # resumed sessions keep old turns in a recently modified file
    msg = d.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if d.get("type") == "assistant" and isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            inp = item.get("input") or {}
            if item.get("name") == "Skill" and inp.get("skill"):
                uses[inp["skill"].lstrip("/")] += 1
            elif item.get("name") in ("Agent", "Task") and inp.get("subagent_type"):
                uses[inp["subagent_type"]] += 1
    elif text := user_text(d):
        m = CMD_RE.search(text)
        if m:
            uses[m.group(1)] += 1
        elif text and not text.lstrip().startswith(("<", "[Request interrupted")):
            prompts.append((d.get("timestamp", ""), "claude", d.get("sessionId", ""), text.strip()[:400]))


def usage(days, claude=CLAUDE, codex=CODEX):
    cutoff = time.time() - days * 86400
    uses, prompts = Counter(), []
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
    for f in (claude / "projects").glob("*/*.jsonl"):
        if f.stat().st_mtime < cutoff:
            continue
        with open(f, errors="ignore") as fh:
            for line in fh:
                parse_claude_line(line, uses, prompts, since)
    hist = codex / "history.jsonl"
    if hist.exists():
        for line in open(hist, errors="ignore"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ts", 0) >= cutoff and d.get("text"):
                prompts.append((time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(d["ts"])), "codex",
                                d.get("session_id", ""), d["text"].strip()[:400]))
    for f in (codex / "sessions").rglob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            continue
        uses.update("codex:" + n for n in codex_skill_reads(f))
    return uses, prompts


def codex_skill_reads(path):
    """Skills the model opened in one Codex session, once each.
    Only its own tool calls count: skill paths also sit in every turn's context listing."""
    names = set()
    with open(path, errors="ignore") as fh:
        for line in fh:
            if "SKILL.md" in line and ('"function_call"' in line or '"custom_tool_call"' in line):
                names.update(CODEX_SKILL_RE.findall(line))
    return names


def invoked_as(c, origin, bare_unique):
    """Every name c may have been called by: qualified, its Scav alias, or its bare name when no other component shares it."""
    keys = {f"{c['plugin']}:{c['name']}"} | {k for k, v in origin.items() if v == c["id"]}
    if bare_unique:
        keys.add(c["name"])
    return keys


def bare_names(comps, claude=CLAUDE):
    """How many things answer to each bare name. The user's own ~/.claude/skills and commands count too:
    `/execute-plan` there must not be credited to a plugin's same-named component."""
    own = [d.name for d in (claude / "skills").glob("*/") if (d / "SKILL.md").exists()]
    own += [f.stem for f in (claude / "commands").glob("*.md")]
    return Counter([c["name"] for c in comps] + own)


def uses_for(c, uses, origin, bare_unique):
    return sum(uses.get(k, 0) for k in invoked_as(c, origin, bare_unique))


def sample_prompts(prompts, limit=600, per_session=4):
    """Spread the sample over time and sessions: each session's first prompts, round-robin across ISO weeks."""
    sessions = {}
    for p in sorted(prompts):
        sessions.setdefault((p[1], p[2] or p[0]), []).append(p)
    weeks, seen = {}, set()
    for picks in sessions.values():
        taken = 0
        for ts, src, _, text in picks:
            if text[:120] in seen:
                continue
            seen.add(text[:120])
            if taken == per_session:
                continue  # still counted as unique, just not shown
            taken += 1
            week = time.strftime("%G-W%V", time.strptime(ts[:10], "%Y-%m-%d")) if ts[:10] else "unknown"
            weeks.setdefault(week, []).append(f"- [{ts[:10]} {src}] {' '.join(text.split())}")
    out, queues = [], [weeks[w] for w in sorted(weeks, reverse=True)]
    while len(out) < limit and any(queues):
        for q in queues:
            if q and len(out) < limit:
                out.append(q.pop(0))
    return sorted(out, reverse=True), len(seen)


def scale_tokens(comps):
    """Rescale the chars/4 estimates so each enabled plugin sums to the always-on cost `claude plugin details` reports."""
    for pid in {c["pid"] for c in comps}:
        try:
            out = subprocess.run(["claude", "plugin", "details", pid], capture_output=True, text=True, timeout=60).stdout
            real = int(re.search(r"Always-on:\s*~?([\d,]+)", out).group(1).replace(",", ""))
        except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
            continue
        mine = [c for c in comps if c["pid"] == pid and c["kind"] in KINDS]
        est = sum(c["tok"] for c in mine)
        for c in mine:
            c["tok"] = max(1, round(c["tok"] * real / est)) if est else c["tok"]


WORD = re.compile(r"[a-z][a-z0-9+#-]{3,}")
STOP = set("this that with from have when what your into then they them will would should could about there "
           "their which while where using used also just like make sure need want each other only more some "
           "than very been were does done here".split())


def keyword_scores(comps, prompts):
    df = Counter()
    for *_, text in prompts:
        df.update(set(WORD.findall(text.lower())) - STOP)
    for c in comps:
        toks = set(WORD.findall((c["name"].replace("-", " ") + " " + c["desc"]).lower())) - STOP
        c["kw"] = round(sum(math.log1p(df[t]) for t in toks) / math.sqrt(len(toks) or 1), 2)


def profile(days):
    STATE.mkdir(exist_ok=True)
    comps = inventory()
    origin = load_manifest().get("origin", {})
    uses, prompts = usage(days)
    keyword_scores(comps, prompts)
    scale_tokens(comps)
    bare = bare_names(comps)
    for c in comps:
        c["uses"] = uses_for(c, uses, origin, bare[c["name"]] == 1)
        # Codex reads its own ~/.codex/skills copy: a same-named signal, kept apart from real Claude invocations
        c["cx"] = uses.get("codex:" + c["name"], 0) if c["kind"] == "skill" else 0
    comps.sort(key=lambda c: (-c["uses"], -c["cx"], -c["kw"]))
    ev = eval_column(eval_history(), load_pins())
    with open(STATE / "inventory.tsv", "w") as fh:
        fh.write("id\tuses\tcx\tkw\tev\ttok\tenabled\tdesc\n")
        for c in comps:
            fh.write(f"{c['id']}\t{c['uses']}\t{c['cx']}\t{c['kw']}\t{ev.get(c['id'], '-')}\t{c['tok']}\t"
                     f"{int(c['enabled'])}\t{c['desc'][:160]}\n")
    # the LLM screen reads this sample, not the raw transcripts
    sample, unique = sample_prompts(prompts)
    (STATE / "profile.md").write_text(
        f"# User prompts, last {days} days ({len(sample)} of {unique} unique, spread across weeks and sessions)\n\n"
        + "\n".join(sample) + "\n")
    names = {c["name"] for c in comps}
    summary = {"days": days, "components": len(comps), "unique_prompts": unique,
               "always_on_tokens_enabled": sum(c["tok"] for c in comps if c["enabled"] and c["kind"] in KINDS),
               "top_uses": dict(uses.most_common(40)),
               # shortcut: uses cover the whole --days window, not just the time since the trial add
               "trials": {c["id"]: c["uses"] for c in comps if c["id"] in load_manifest().get("trial", [])},
               "codex_skills_without_plugin": {k: v for k, v in uses.most_common()
                                               if k.startswith("codex:") and k[6:] not in names and v >= 3}}
    (STATE / "profile.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


# ---------- apply ----------

def load_manifest():
    p = STATE / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def scav_source(claude=CLAUDE):
    """The Scav marketplace's source dir: installs copy from here, so ingest writes here, not into the cache."""
    try:
        return Path(json.loads((claude / "plugins/known_marketplaces.json").read_text())[SELF]["installLocation"])
    except (FileNotFoundError, KeyError):
        return Path(__file__).resolve().parent.parent


def clear_generated(dest, manifest):
    for rel in manifest.get("generated", []):
        p = dest / rel
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()


def rewrite(text, root, renames):
    text = text.replace("${CLAUDE_PLUGIN_ROOT}", root).replace("$CLAUDE_PLUGIN_ROOT", root)
    for old, new in renames.items():
        text = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", new, text)
    return text


REL_PATH = re.compile(r"(?:\.\./)+[\w.-][\w./-]*")


def relink(text, src_dir, root, vendor=None, md_dir=None):
    """Fix ../ references that leave the copied component (e.g. a plugin-root references/ dir).
    With `vendor` (a skill dir), the target is copied into vendor/_ext/ and linked relatively, so it survives source
    updates. Without it (single-file agents/commands), the link becomes absolute.
    shortcut: absolute links break when the source version dir is cleaned up; re-run /scav after plugin updates.
    Paths that don't resolve to a real file are prose examples and stay as written."""
    def fix(m):
        target = (src_dir / m.group(0)).resolve()
        if not target.is_file():
            return m.group(0)
        if vendor is None:
            return str(target)
        try:
            local = vendor / "_ext" / target.relative_to(Path(root).resolve())
        except ValueError:
            return str(target)  # outside the source plugin: nothing sensible to vendor
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, local)
        return os.path.relpath(local, md_dir)
    return REL_PATH.sub(fix, text)


def copy_component(c, dest, n, renames):
    """Copy one skill/agent/command into plugin dir `dest` as `n`, with paths fixed up. Returns its relative path."""
    root, src = c["root"], Path(c["path"])
    if c["kind"] == "skill":
        rel = f"skills/{n}"
        shutil.copytree(src, dest / rel, symlinks=True, ignore=shutil.ignore_patterns("node_modules", ".git"))
        mds = list((dest / rel).rglob("*.md"))
    else:
        rel = f"{c['kind']}s/{n}.md"
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)
        mds = [dest / rel]
    for md in mds:
        src_dir = (src / md.relative_to(dest / rel)).parent if c["kind"] == "skill" else src.parent
        vendor = dest / rel if c["kind"] == "skill" else None
        text = relink(rewrite(md.read_text(errors="ignore"), root, renames), src_dir, root, vendor, md.parent)
        if n != c["name"] and md.name in ("SKILL.md", f"{n}.md"):
            text = re.sub(r"^name:.*$", f"name: {n}", text, count=1, flags=re.M)
        md.write_text(text)
    return rel


def apply(selection_path, claude=CLAUDE, dest=None):
    sel = json.loads(Path(selection_path).read_text())
    comps = {c["id"]: c for c in inventory(claude, all_fetched=True)}
    pins = load_pins()
    stale_pins = pins - comps.keys()  # e.g. the source plugin was uninstalled; warn, don't block
    keep = set(sel.get("keep", [])) | (pins & comps.keys())
    missing = keep - comps.keys()
    if missing:
        sys.exit(f"unknown component ids: {sorted(missing)[:10]}")
    dest = Path(dest) if dest else scav_source(claude)
    STATE.mkdir(exist_ok=True)
    prior = load_manifest()
    clear_generated(dest, prior)
    # trials stay trials while kept unpinned; a selection that leaves one out drops it like any unpinned component
    trial = ((set(prior.get("trial", [])) | set(sel.get("trial", []))) & keep) - pins

    chosen = [comps[i] for i in sorted(keep)]
    counts = Counter((c["kind"], c["name"]) for c in chosen if c["kind"] in KINDS)
    newname = {c["id"]: f"{c['plugin']}-{c['name']}" if counts[(c["kind"], c["name"])] > 1 or c["name"] in RESERVED
               else c["name"] for c in chosen if c["kind"] in KINDS}
    renames = {f"{comps[i]['plugin']}:{comps[i]['name']}": f"{SELF}:{n}" for i, n in newname.items()}
    generated, origin = [], {}
    warnings = [f"pinned but not installed: {i}" for i in sorted(stale_pins)]

    for c in chosen:
        if c["kind"] not in KINDS:
            continue
        n = newname[c["id"]]
        generated.append(copy_component(c, dest, n, renames))
        origin[f"{SELF}:{n}"] = c["id"]

    hooks = {}
    for c in chosen:
        if c["kind"] == "hook":
            for event, groups in plugin_config(Path(c["root"]), "hooks", "hooks/hooks.json")[0].items():
                for g in groups:
                    for h in g.get("hooks", []):
                        if h.get("type") == "command":
                            # hook scripts that read $CLAUDE_PLUGIN_ROOT must keep seeing their source plugin
                            h["command"] = f"export CLAUDE_PLUGIN_ROOT='{c['root']}'; " + rewrite(h["command"], c["root"], {})
                    hooks.setdefault(event, []).append(g)
    if hooks:
        (dest / "hooks").mkdir(exist_ok=True)
        (dest / "hooks/hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2))
        generated.append("hooks/hooks.json")

    servers = {}
    for c in chosen:
        if c["kind"] == "mcp":
            cfg = plugin_config(Path(c["root"]), "mcpServers", ".mcp.json")[0][c["name"]]
            cfg = json.loads(rewrite(json.dumps(cfg), c["root"], {}))
            cfg.setdefault("env", {})["CLAUDE_PLUGIN_ROOT"] = c["root"]
            servers[c["name"] if c["name"] not in servers else f"{c['plugin']}-{c['name']}"] = cfg
    if servers:
        (dest / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2))
        generated.append(".mcp.json")

    bump_version(dest)
    settings = claude / "settings.json"
    if settings.exists() and not prior:  # back up once per apply..restore cycle
        shutil.copy2(settings, STATE / "settings.pre-scav.json")
    (STATE / "manifest.json").write_text(json.dumps(
        {"applied_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "dest": str(dest), "generated": generated,
         "origin": origin, "disable": sel.get("disable", []), "trial": sorted(trial)}, indent=2))
    print(json.dumps({"ingested": len(origin), "hooks_from": [c["plugin"] for c in chosen if c["kind"] == "hook"],
                      "mcp": list(servers), "trial": sorted(trial),
                      "scav_always_on_tokens": sum(comps[i]["tok"] for i in keep if comps[i]["kind"] in KINDS),
                      "warnings": warnings}, indent=2))


def bump_version(dest):
    # convention: Scav's patch number counts applies, so `claude plugin update` sees a new version
    for rel, get in ((".claude-plugin/plugin.json", lambda d: d),
                     (".claude-plugin/marketplace.json", lambda d: d["plugins"][0])):
        p = dest / rel
        if p.exists():
            d = json.loads(p.read_text())
            major, minor, patch = get(d).get("version", "0.1.0").split(".")
            get(d)["version"] = f"{major}.{minor}.{int(patch) + 1}"
            p.write_text(json.dumps(d, indent=2) + "\n")


def restore():
    """Remove ingested components and re-enable the plugins Scav absorbed.
    Re-enables through the CLI rather than copying settings.pre-scav.json back, which would undo later settings edits."""
    m = load_manifest()
    if not m:
        sys.exit("nothing to restore: no ~/.scav/manifest.json")
    clear_generated(Path(m["dest"]), m)
    bump_version(Path(m["dest"]))
    for pid in m.get("disable", []):
        subprocess.run(["claude", "plugin", "enable", pid], check=False)
    (STATE / "manifest.json").unlink()
    print(f"restored: ingested components removed, re-enabled {m.get('disable', [])}; "
          f"full settings backup at {STATE / 'settings.pre-scav.json'}")


PINS = "pins.json"


def load_pins():
    p = STATE / PINS
    return set(json.loads(p.read_text())) if p.exists() else set()


def save_pins(pins):
    STATE.mkdir(exist_ok=True)
    (STATE / PINS).write_text(json.dumps(sorted(pins), indent=2))


def list_plugin(plugin, claude=CLAUDE):
    """One plugin's components, for matching a plain-language request. `plugin` is name or name@marketplace."""
    name = plugin.split("@")[0]
    rows = [c for c in inventory(claude, all_fetched=True) if c["plugin"] == name]
    if not rows:
        sys.exit(f"{plugin} is not installed")
    for c in rows:
        print(f"{c['id']}\t{c['tok']}\t{c['path']}\t{c['desc'][:200]}")


def add(ids, disable, claude=CLAUDE, dest=None, trial=False):
    """Pin ids so every later apply keeps them, merge them into the current selection, and apply.
    With `trial`, nothing is pinned: the ids are marked as trials, and the next /scav run drops them if unused."""
    known = {c["id"] for c in inventory(claude, all_fetched=True)}
    unknown = [i for i in ids if i not in known]
    if unknown:
        sys.exit(f"unknown component ids: {unknown}")
    if not trial:
        save_pins(load_pins() | set(ids))
    STATE.mkdir(exist_ok=True)
    path = STATE / "selection.json"
    sel = json.loads(path.read_text()) if path.exists() else {"keep": [], "disable": []}
    sel["keep"] = sorted(set(sel.get("keep", [])) | set(ids))
    sel["disable"] = sorted(set(sel.get("disable", [])) | set(disable))
    if trial:
        sel["trial"] = sorted(set(sel.get("trial", [])) | set(ids))
    path.write_text(json.dumps(sel, indent=2))
    apply(path, claude=claude, dest=dest)
    for alias, i in load_manifest()["origin"].items():
        if i in ids:
            print(f"{i} loads as {alias}")


def unpin(ids):
    save_pins(load_pins() - set(ids))
    print(f"unpinned {ids}; they stay in Scav until the next /scav run drops them")


def catalog_entries(claude=CLAUDE):
    """Plugins offered by marketplaces already on disk but not installed, as find rows."""
    try:
        known = json.loads((claude / "plugins/known_marketplaces.json").read_text())
        installed = json.loads((claude / "plugins/installed_plugins.json").read_text())["plugins"]
    except (FileNotFoundError, ValueError, KeyError):
        return []
    rows = []
    for mkt, info in known.items():
        try:
            plugins = json.loads((Path(info["installLocation"]) / ".claude-plugin/marketplace.json").read_text())["plugins"]
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            continue  # local marketplaces may have no catalog file
        for p in plugins:
            pid = f"{p.get('name')}@{mkt}"
            if pid not in installed and p.get("name"):
                rows.append({"id": f"plugin:{pid}", "kind": "plugin", "plugin": p["name"], "name": p["name"],
                             "desc": p.get("description", ""), "tok": "-", "status": "not installed"})
    return rows


def find(need, limit=5, catalogs=False, github=False, claude=CLAUDE):
    """Rank local components (disabled plugins included) against a need, by the same keyword overlap profile uses.
    shortcut: WORD skips words under 4 letters (pdf, sql, git); widen it if short needs come up empty."""
    origin = set(load_manifest().get("origin", {}).values())
    comps = inventory(claude, all_fetched=True)
    for c in comps:
        c["status"] = ("in-scav" if c["id"] in origin else "on" if c["enabled"] else
                       "fetched" if c["pid"].startswith("github:") else "off")
    comps += catalog_entries(claude) if catalogs else []
    if github:
        try:
            comps += github_entries(need, limit)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print(f"github search skipped: {e}", file=sys.stderr)
    stems = lambda text: {t[:6] for t in set(WORD.findall(text.lower())) - STOP}  # accessible ~ accessibility
    want = stems(need)
    toks = [stems(c["name"].replace("-", " ") + " " + c["desc"]) for c in comps]
    df = Counter(t for ts in toks for t in ts & want)
    # like keyword_scores, but a need word counts by its rarity across components: "review" alone says little
    for c, ts in zip(comps, toks):
        c["kw"] = sum(math.log(1 + len(comps) / df[t]) for t in ts & want) / math.sqrt(len(ts) or 1)
    hits = sorted((c for c in comps if c["kw"] > 0), key=lambda c: -c["kw"])[:limit]
    if not hits:
        print(f"no component matches: {need}")
    for c in hits:
        print(f"{c['id']}\t{c['plugin']}\t{c['status']}\t{c['tok']}\t{c['desc'][:200]}")


# ---------- eval ----------
# The baseline is the placebo control, not plain Claude: loading and invoking any skill makes answers longer and fuller
# (placebo +0.30 over vanilla, 2026-10-18), so a component is credited only for what its content adds beyond that.
# Each component is wrapped alone in a temporary plugin, so `claude plugin eval --ablation with-without` scores it
# against a no-plugin arm. Both arms run with a fresh config dir: no user plugins or skills, i.e. vanilla Claude.
# Verdicts rest on distinct prompts (scores averaged per prompt over every run first), and prune verdicts only count
# once calibration controls on the same model and judge show the instrument separates a known win from a known loss.

EVAL_PLUGIN = "scav-eval"
EVAL_VERSION = 4  # history rows from another method (vs vanilla, blind judge, rubric, unscreened) don't pool
MARGIN = 0.1  # an effect smaller than this is not a win worth its tokens
MIN_PROMPTS = 4  # distinct prompts a component must have fired on before any verdict
T95 = [6.314, 2.920, 2.353, 2.132, 2.015, 1.943, 1.895, 1.860, 1.833, 1.812]  # one-sided 95% t, df 1..10
CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Prompts that lean on local state (paths, "this repo", commit hashes, file names) can't be judged in the empty eval
# sandbox: both arms would flounder and tie, which would read as "no better than vanilla".
CTX_RE = re.compile(r"(?:^|[\s`(\[])~?/[\w.-]+/|\b(?:this|my|the|our|that) (?:repo|diff|branch|commit|pr|changes?|code|"
                    r"file|project|worktree|plan|spec)s?\b|\b[0-9a-f]{7,40}\b|\b[\w-]+\.(?:py|tsx?|js|md|rs|json|ya?ml|toml)\b",
                    re.I)
# The change a task is about: an explicit range, or "review commit X". A bare hash is often a base or a checkout
# ("at commit X"), whose own diff is the wrong context.
RANGE_RE = re.compile(r"\b([0-9a-f]{7,40})\.{2,3}([0-9a-f]{7,40})\b")
REVIEW_COMMIT_RE = re.compile(r"\breview(?:ing)?\s+(?:the\s+)?commit\s+([0-9a-f]{7,40})\b", re.I)
ABS_PATH_RE = re.compile(r"(?:~|/Users|/home|/private)/[\w./@-]+")
FIXTURE_CAP = 40000  # chars; a bigger diff would crowd out the task
# Same line in both arms and names no component: it stands in for the user's CLAUDE.md, which asks for skill use.
# Without it the sandboxed model almost never reaches for a skill (0 of 13 runs fired, 2026-10-15).
NUDGE = "Before answering, check your available skills and agents and use any that fit the task."
# The LLM judge sees only the response, never the prompt (probed 2.1.296), so each grader restates the request.
RUBRIC = {  # judged against the user's request, never against the component's own instructions
    "done": "PASS if the response actually does what the request asks (answers it, produces the artifact, or makes "
            "concrete progress) instead of only outlining a plan or asking questions the request already answers.",
    "specific": "PASS if the content is specific to this request (concrete names, numbers, steps or text from it) "
                "rather than generic advice that would fit any similar request.",
    "grounded": "PASS if the response invents nothing: no made-up file contents, results, sources or facts it could "
                "not know from the request, and it flags uncertainty where the answer depends on it.",
    "concise": "PASS if there is no padding: no restating the request, no boilerplate preamble or closing offer, and "
               "length proportional to the task.",
}
JUDGE_REQUEST_CAP = 6000
# Calibration controls: run like components on real answerable prompts, each against the placebo. The gate needs the
# saboteur to lose to the placebo and the placebo to tie itself (no false effect). The partial saboteur (sabotages
# only questions) is reported, not required: at n=10 with one run, effects that size are below what the instrument
# resolves (partial − placebo −0.47 [−1.01, +0.06], 2026-10-18), so moderate effects stay inconclusive.
CONTROLS = {
    "placebo": "Answer the request as you normally would.",
    "saboteur": "Reply with one short generic sentence. Never use specifics from the request: no names, numbers, "
                "steps or examples.",
    "partial": "If the request contains a question mark (?), reply with one short generic sentence that uses no "
               "specifics from the request. Otherwise answer the request as you normally would.",
}


def fired_tasks(days, claude=CLAUDE):
    """{invoked name: [task, ...]}: what each Skill/Agent/slash-command invocation worked on.
    A task is {text, standalone, slash, cwd}. An agent works on its brief, a slash command on its arguments, a skill
    on its args or the user's last prompt. That prompt stands alone only if it opened the session."""
    cutoff = time.time() - days * 86400
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
    out = {}
    def add(name, text, standalone, slash, cwd):
        out.setdefault(name, []).append({"text": text, "standalone": standalone, "slash": slash, "cwd": cwd})
    for f in (claude / "projects").glob("*/*.jsonl"):
        if f.stat().st_mtime < cutoff:
            continue
        last, opener = "", None
        with open(f, errors="ignore") as fh:
            for line in fh:
                if '"tool_use"' not in line and '"user"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("timestamp", "") < since:
                    continue
                cwd = d.get("cwd", "")
                if text := user_text(d):
                    if m := CMD_RE.search(text):
                        a = CMD_ARGS_RE.search(text)
                        add(m.group(1).lstrip("/"), a.group(1).strip() if a else "", True, True, cwd)
                    elif not text.lstrip().startswith(("<", "[Request interrupted")):
                        last = text.strip()
                        opener = last if opener is None else opener
                elif d.get("type") == "assistant" and isinstance(d.get("message"), dict):
                    for item in d["message"].get("content") or []:
                        if not isinstance(item, dict) or item.get("type") != "tool_use":
                            continue
                        inp = item.get("input") or {}
                        if item.get("name") == "Skill" and inp.get("skill"):
                            name, own = inp["skill"].lstrip("/"), inp.get("args")
                        elif item.get("name") in ("Agent", "Task") and inp.get("subagent_type"):
                            name, own = inp["subagent_type"], inp.get("prompt")
                        else:
                            continue
                        add(name, *((own, True) if own else (last, last == opener)), False, cwd)
    return out


def git(*args):
    p = subprocess.run(["git", *args], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def fixture(text, cwd=""):
    """The immutable context a repo-bound task points at, as text to inline: the diff of the change it reviews (an
    explicit commit range, or "review commit X"), from a repo it names or ran in. Files are never inlined at their current state: they may already hold the answer.
    None when nothing resolves or the diff is too big."""
    repos = []
    for raw in ABS_PATH_RE.findall(text) + [cwd]:
        p = Path(os.path.expanduser(raw.rstrip(".,:;)`'\""))) if raw else None
        while p and p != p.parent and not p.exists():  # a removed worktree resolves to its parent repo
            p = p.parent
        if p:
            top = git("-C", str(p), "rev-parse", "--show-toplevel")
            if top and top.strip() not in repos:
                repos.append(top.strip())
    parts = []
    for a, b in dict.fromkeys(RANGE_RE.findall(text) + [(h, "") for h in REVIEW_COMMIT_RE.findall(text)]):
        for repo in repos:
            if all(git("-C", repo, "cat-file", "-e", f"{h}^{{commit}}") is not None for h in (a, b) if h):
                args = ["diff", f"{a}..{b}"] if b else ["show", "--format=commit %H%n%n%B", a]
                parts.append(f"$ git {' '.join(args)}\n" + git("-C", repo, *args))
                break
    body = "\n".join(parts)
    return body if parts and len(body) <= FIXTURE_CAP else None


def case_key(prompt):
    return hashlib.sha1(prompt.encode()).hexdigest()[:10]


def build_cases(c, keys, tasks, prompts, limit=6, screen=None):
    """Eval prompts for c: tasks it actually fired on first (self-contained, or repo-bound with the named commits
    inlined), then real session-opening prompts whose words overlap its description.
    Returns (cases, number of fired tasks skipped as context-dependent)."""
    cases, seen, skipped, screen = [], set(), 0, screen or answerable
    def take(text, source, standalone=True, cwd=""):
        nonlocal skipped
        text = text.strip()
        if len(text) < 40 or text[:120] in seen or len(cases) >= limit:
            return
        seen.add(text[:120])
        if not standalone:
            skipped += source == "fired"
        elif not CTX_RE.search(text) and screen(text):
            cases.append({"prompt": text, "source": source})
        elif source == "fired" and (fx := fixture(text, cwd)):
            cases.append({"prompt": f"{text}\n\nThe repository itself is not available here. These are the commits "
                                    f"this task names, captured for you:\n\n```\n{fx}```\n", "source": "fixture"})
        else:
            skipped += source == "fired"
    for k in sorted(keys):
        for t in tasks.get(k, []):
            take(t["text"], "fired", t["standalone"], t["cwd"])
    want = set(WORD.findall((c["name"].replace("-", " ") + " " + c["desc"]).lower())) - STOP
    for hits, text in sorted(((len(want & set(WORD.findall(t.lower()))), t) for t in openers(prompts)), reverse=True):
        if hits < 2:
            break
        take(text, "kw")
    return cases, skipped


def openers(prompts):
    """Each session's first prompt: it stands alone, later ones lean on the conversation so far."""
    first = {}
    for p in sorted(prompts):
        first.setdefault((p[1], p[2] or p[0]), p[3])
    return list(first.values())


def calibration_prompts(prompts, n, screen=None):
    """A fixed sample of answerable session openers, the same every time so calibration runs pool. The sample is
    kept in ~/.scav/evals/calibration_prompts.json: new sessions change the opener pool, which would reshuffle it."""
    path = STATE / "evals/calibration_prompts.json"
    picks = json.loads(path.read_text()) if path.exists() else []
    if len(picks) < n:
        pool = sorted(t for t in openers(prompts) if 80 <= len(t) <= 3000 and not CTX_RE.search(t))
        random.Random(0).shuffle(pool)
        for t in pool:  # screen lazily: each check is a model call
            if len(picks) == n:
                break
            if t not in picks and (screen or answerable)(t):
                picks.append(t)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(picks, indent=2))
    return picks[:n]


def write_case(path, prompt, c, n):
    """One case in `claude plugin eval` format: a rubric scored in both arms plus a with-only did-it-fire check."""
    tool, field = ("Agent", "subagent_type") if c["kind"] == "agent" else ("Skill", "skill")
    (path / "graders").mkdir(parents=True, exist_ok=True)
    (path / "prompt.md").write_text(
        f"---\nmax_turns: 8\nallowed_tools: [Read, Glob, Grep, Skill, Agent]\nappend_system_prompt: '{NUDGE}'\n---\n\n"
        + prompt + "\n")
    (path / "graders/fired.md").write_text(
        f"---\ntype: tool_used\ntool: {tool}\ninput_match: '\"{field}\"\\s*:\\s*\"(?:[\\w-]+:)?{n}\"'\n"
        "arm: with-only\n---\n")
    request = prompt if len(prompt) <= JUDGE_REQUEST_CAP else prompt[:JUDGE_REQUEST_CAP] + "\n[...truncated]"
    for name, criteria in RUBRIC.items():
        (path / f"graders/{name}.md").write_text(
            f"---\ntype: llm\n---\n\nThe user's request was:\n<request>\n{request}\n</request>\n\n{criteria}\n")


def invoked(run):
    """Did the skill or agent under test fire in this run? Read by grader name: `withOnly` is only set when the eval
    has a no-plugin arm."""
    return any(g.get("name") == "fired" and g.get("passed") for g in run.get("graders", []))


def versus_placebo(res, placebo, runs, own=False):
    """res with each case's comparison arm taken from the placebo's runs on the same prompt, not plain Claude.
    Only placebo runs that invoked the placebo count. own=True judges the placebo against itself: its first `runs`
    runs against the rest."""
    by_key = {case["name"].split("-", 1)[1]: case for case in placebo.get("cases", [])}
    cases = []
    for case in res.get("cases", []):
        base = by_key.get(case["name"].split("-", 1)[1], {}).get("arms", {}).get("with", [])
        mine = case.get("arms", {}).get("with", [])
        if own:
            mine, base = mine[:runs], mine[runs:]
        cases.append({**case, "arms": {"with": mine, "without": [r for r in base if invoked(r)][:runs]}})
    return {**res, "cases": cases}


def placebo_baseline(cases, need, model, fresh):
    """(result, note): `need` invoked placebo runs per prompt. Placebo answers don't depend on the component, so runs
    from earlier evals on the same model are reused, and fresh(n) runs the placebo only for what is missing."""
    keys = {case_key(x["prompt"]) for x in cases}
    got, asked = {}, {}
    def take(res):
        for case in res.get("cases", []):
            k = case["name"].split("-", 1)[1]
            if k in keys:
                asked[k] = case.get("promptMarkdown") or asked.get(k, "")
                got.setdefault(k, []).extend(r for r in case.get("arms", {}).get("with", [])
                                             if not r.get("error") and invoked(r))
    for f in sorted((STATE / "evals").glob("*/placebo-*/result.json")):
        res = json.loads(f.read_text())
        if (res.get("suite", {}).get("modelOverride") or "default") == (model or "default"):
            take(res)
    note, missing = "", need - min(len(got.get(k, [])) for k in keys)
    if missing > 0:
        res, note = fresh(missing)
        if res is not None:
            take(res)
    if not got:
        return None, note
    return {"cases": [{"name": f"placebo-{k}", "promptMarkdown": asked[k], "arms": {"with": got[k]}}
                      for k in sorted(got)]}, note


def summarize(res, c, run, group, model, judge, explicit):
    """One history row: raw scores per prompt, so later runs pool per prompt instead of counting runs as evidence."""
    per_case, cw, cwo, fired_runs, with_runs = {}, [], [], 0, 0
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None
    for case in res.get("cases", []):
        withs = [r for r in case.get("arms", {}).get("with", []) if not r.get("error")]
        withouts = [r for r in case.get("arms", {}).get("without", []) if not r.get("error")]
        fired = sum(map(invoked, withs))
        fired_runs, with_runs = fired_runs + fired, with_runs + len(withs)
        cw += [r["costUsd"] for r in withs]
        cwo += [r["costUsd"] for r in withouts]
        src, key = case["name"].split("-", 1)
        per_case[key] = {"src": src, "fired": fired, "with": [r["score"] for r in withs],
                         "without": [r["score"] for r in withouts]}
    src = Path(c["path"]) / "SKILL.md" if c["kind"] == "skill" else Path(c["path"])
    return {"id": c["id"], "run": run, "group": group, "model": model or "default", "judge": judge or "haiku",
            "explicit": explicit, "per_case": per_case, "fired": f"{fired_runs}/{with_runs}",
            "cost_with": mean(cw), "cost_without": mean(cwo), "tok": c["tok"],
            "invoke_tok": len(src.read_text(errors="ignore")) // 4 if src.exists() else 0,
            "partial": bool(res.get("partial"))}


PAIR_SYSTEM = ("You compare two answers to one user request. The better answer does what was asked, is specific to "
               "the request, invents nothing it could not know, and has no padding. Length alone is not quality.")
PAIR_SCHEMA = json.dumps({"type": "object", "properties": {"better": {"enum": ["A", "B", "tie"]}},
                          "required": ["better"]})


def claude_json(system, text, schema, model):
    """Structured output from one `claude -p` call with no tools and no user settings, or None."""
    p = subprocess.run(["claude", "-p", "--model", model, "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                        "--no-session-persistence", "--system-prompt", system, "--json-schema", schema,
                        "--output-format", "json"], input=text, capture_output=True, text=True)
    try:
        return json.loads(p.stdout)["structured_output"]
    except (ValueError, KeyError, TypeError):
        return None


def ask_judge(request, a, b, judge):
    """'A', 'B' or 'tie'. An unreadable verdict counts as no preference rather than failing the run."""
    text = (f"<request>\n{request}\n</request>\n\n<answer_A>\n{a}\n</answer_A>\n\n<answer_B>\n{b}\n</answer_B>\n\n"
            "Which answer better serves the request?")
    return (claude_json(PAIR_SYSTEM, text, PAIR_SCHEMA, judge) or {}).get("better", "tie")


ANSWERABLE_SYSTEM = ("You screen user requests for an offline test. The assistant answering will have no files, no "
                     "repository, no shell, no servers, no memory and no earlier conversation: only the request text. "
                     "A request is answerable only if a complete, useful written answer needs nothing else: not an "
                     "action on a system (deploy, copy, restart, fix), not existing code, documents, logs or project "
                     "state it refers to, and not the current session or earlier discussion. When in doubt, it is not.")
ANSWERABLE_SCHEMA = json.dumps({"type": "object", "properties": {"answerable": {"type": "boolean"}},
                                "required": ["answerable"]})


def answerable(prompt, model="sonnet"):
    """Can a good answer be written from the request text alone? Cached per prompt in ~/.scav/evals/.
    Regex catches paths and "this repo"; this catches "our current work", "on my server", "the log below" with none."""
    path = STATE / "evals/answerable.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    key = case_key(prompt)
    if key not in cache:
        out = claude_json(ANSWERABLE_SYSTEM, f"<request>\n{prompt}\n</request>\n\nCan a good answer be written from "
                          "this text alone, without access to anything it refers to?", ANSWERABLE_SCHEMA, model)
        if out is None:
            return False  # unscreened is unusable; not cached, so a later run retries
        cache[key] = bool(out.get("answerable"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    return cache[key]


def pairwise(res, judge, ask=ask_judge):
    """{prompt key: [score per paired run]}: run i with the component against run i without, judged side by side
    twice with the order swapped, so a position-biased judge cancels out. +1 with wins both, -1 loses both, 0 split
    or tie. Only runs where the component fired are judged."""
    out = {}
    answer = lambda r: next((g.get("evidence", "") for g in r.get("graders", []) if g.get("evidence")), "")
    for case in res.get("cases", []):
        withs = [r for r in case.get("arms", {}).get("with", []) if not r.get("error")]
        withouts = [r for r in case.get("arms", {}).get("without", []) if not r.get("error")]
        request = case.get("promptMarkdown", "")
        for w, wo in zip(withs, withouts):
            if not invoked(w):
                continue
            first = {"A": 1, "B": -1}.get(ask(request, answer(w), answer(wo), judge), 0)
            second = {"A": -1, "B": 1}.get(ask(request, answer(wo), answer(w), judge), 0)
            out.setdefault(case["name"].split("-", 1)[1], []).append((first + second) / 2)
    return out


def bounds(xs):
    """One-sided 95% (lower, upper) bounds on the mean of xs, or (None, None) below two values."""
    n = len(xs)
    if n < 2:
        return None, None
    m = sum(xs) / n
    h = (T95[n - 2] if n - 1 <= len(T95) else 1.73) * statistics.stdev(xs) / math.sqrt(n)
    return m - h, m + h


def pooled(rows):
    """{prompt key: mean side-by-side score} over all rows, for prompts where the component fired and was judged.
    A prompt without a side-by-side score (e.g. its without arm was cut by the budget) is not evidence: the rubric
    difference is on another scale."""
    acc = {}
    for r in rows:
        for k, pc in r["per_case"].items():
            a = acc.setdefault(k, {"pair": [], "fired": 0})
            a["pair"] += pc.get("pair", [])
            a["fired"] += pc["fired"]
    return {k: sum(a["pair"]) / len(a["pair"]) for k, a in acc.items() if a["fired"] and a["pair"]}


def effect(rows):
    """Effect over the placebo across distinct prompts: {n, delta, lo, hi}."""
    deltas = list(pooled(rows).values())
    lo, hi = bounds(deltas)
    return {"n": len(deltas), "delta": sum(deltas) / len(deltas) if deltas else None, "lo": lo, "hi": hi}


def calibration(rows, model, judge, min_prompts=MIN_PROMPTS):
    """(ok, why): did the controls behave on this model and judge? Prune verdicts count only when ok."""
    eff = {k: effect([r for r in rows if r["id"] == f"control:{k}" and (r["model"], r["judge"]) == (model, judge)])
           for k in CONTROLS}
    shown = lambda k, e: f"{k} {e['delta']:+.2f} [{e['lo']:+.2f}, {e['hi']:+.2f}] n={e['n']}" if e["lo"] is not None \
        else f"{k} n={e['n']}"
    power = "" if eff["partial"]["lo"] is None else f"; power limit: {shown('partial', eff['partial'])}"
    if any(eff[k]["n"] < max(min_prompts, 2) for k in ("placebo", "saboteur")):
        return False, f"controls not run on {model}/{judge}: " + ", ".join(f"{k} n={e['n']}" for k, e in eff.items())
    ok = eff["saboteur"]["hi"] < 0 and eff["placebo"]["lo"] <= 0 <= eff["placebo"]["hi"]
    return ok, f"{shown('saboteur', eff['saboteur'])}, {shown('placebo', eff['placebo'])}" + power


def verdicts(rows, pins, min_prompts=MIN_PROMPTS, margin=MARGIN):
    """{id: {verdict, why, delta, n}}. Each id is judged on its latest model and judge, over distinct prompts:
    keep (beats the placebo), prune (confidently no win worth MARGIN, or an as-good cheaper look-alike), inconclusive,
    insufficient, idle (never fires on its own past tasks; report only). A prune becomes report for pinned ids and
    uncalibrated when the controls have not passed on that model and judge."""
    by_id, out, setup = {}, {}, {}
    for r in sorted(rows, key=lambda r: r["run"]):
        by_id.setdefault(r["id"], []).append(r)
    for i, rs in by_id.items():
        setup[i] = (rs[-1]["model"], rs[-1]["judge"])
        same = by_id[i] = [r for r in rs if (r["model"], r["judge"]) == setup[i]]
        e = effect(same)
        res = {"delta": e["delta"], "n": e["n"]}
        if e["n"] < min_prompts:
            own = [pc for r in same for pc in r["per_case"].values() if pc["src"] != "kw"]
            if not same[-1]["explicit"] and sum(len(pc["with"]) for pc in own) >= 4 and not any(pc["fired"] for pc in own):
                out[i] = {**res, "verdict": "idle", "why": "never fired on its own past tasks"}
            else:
                out[i] = {**res, "verdict": "insufficient", "why": f"fired on {e['n']}/{min_prompts} distinct prompts"}
        elif e["lo"] > 0:
            out[i] = {**res, "verdict": "keep", "why": f"beats the placebo, lower bound {e['lo']:+.2f}"}
        elif e["hi"] < margin:
            out[i] = {**res, "verdict": "prune", "why": f"no win over the placebo, upper bound {e['hi']:+.2f}"}
        else:
            out[i] = {**res, "verdict": "inconclusive", "why": f"bounds [{e['lo']:+.2f}, {e['hi']:+.2f}]"}
    members = {}
    for i, rs in by_id.items():
        if not i.startswith("control:"):
            members.setdefault(rs[-1]["group"], []).append(i)
    cost = lambda i: by_id[i][-1]["tok"] + by_id[i][-1]["invoke_tok"]
    for ids in members.values():
        for m in ids:
            for k in sorted((k for k in ids if cost(k) < cost(m)), key=cost):
                pm, pk = pooled(by_id[m]), pooled(by_id[k])
                diffs = [pm[x] - pk[x] for x in pm.keys() & pk.keys()]
                if len(diffs) >= min_prompts and bounds(diffs)[1] < margin and out[m]["verdict"] != "prune":
                    out[m] = {**out[m], "verdict": "prune", "why": f"look-alike {k} scores as well and costs fewer tokens"}
                    break
    for i, v in out.items():
        if v["verdict"] == "prune" and not i.startswith("control:"):
            ok, why = calibration(rows, *setup[i], min_prompts)
            if i in pins:
                out[i] = {**v, "verdict": "report", "why": v["why"] + " (pinned)"}
            elif not ok:
                out[i] = {**v, "verdict": "uncalibrated", "why": v["why"] + f"; calibration failed: {why}"}
    return out


def eval_history():
    """Rows from the current method: side-by-side judge on screened prompts. Earlier rows used the absolute rubric
    (a different scale), a judge that never saw the request, or prompts that needed unseen context."""
    p = STATE / "evals/history.jsonl"
    rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []
    return [r for r in rows if r.get("v") == EVAL_VERSION]


def eval_column(rows, pins):
    """{id: 'verdict±delta/nN'} for the inventory's `ev` column."""
    return {i: v["verdict"] + (f"{v['delta']:+.2f}/n{v['n']}" if v["delta"] is not None else "")
            for i, v in verdicts(rows, pins).items() if not i.startswith("control:")}


def control_component(name, root):
    """A calibration control as a skill on disk, shaped like an inventory entry."""
    d = root / f"control-{name}"
    d.mkdir(parents=True, exist_ok=True)
    desc = "Use for any request."
    (d / "SKILL.md").write_text(f"---\nname: answer-{name}\ndescription: {desc}\n---\n\n{CONTROLS[name]}\n")
    return {"id": f"control:{name}", "kind": "skill", "plugin": "control", "name": f"answer-{name}", "path": str(d),
            "root": str(d), "desc": desc, "tok": len(desc) // 4}


def eval_components(ids, budget, model=None, judge=None, runs=1, limit=6, days=365, dry=False, calibrate=False,
                    claude=CLAUDE):
    comps = {c["id"]: c for c in inventory(claude)}
    origin = load_manifest().get("origin", {})
    run = time.strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = STATE / "evals" / run
    tasks, (_, prompts) = fired_tasks(days, claude), usage(days, claude)
    if calibrate:
        ids = ids or [f"control:{k}" for k in CONTROLS]  # a subset, e.g. to finish one control
        comps.update({f"control:{k}": control_component(k, out_dir / "controls") for k in CONTROLS})
        fixed = [{"prompt": t, "source": "cal"} for t in calibration_prompts(prompts, limit)]
    ids = ids or sorted(set(origin.values()))  # default: everything Scav holds
    unknown = [i for i in ids if i not in comps]
    if unknown:
        sys.exit(f"unknown component ids: {unknown}")
    bare = bare_names(comps.values(), claude)
    groups = {}  # look-alikes (same kind and name) share one case set so their scores compare
    for i in ids:
        groups.setdefault(f"{comps[i]['kind']}:{comps[i]['name']}", []).append(comps[i])
    placebo = control_component("placebo", out_dir / "controls")
    placebo_runs = runs * (2 if "control:placebo" in ids else 1)  # judged against itself needs twice the runs
    rows, notes, spent, baselines = [], {}, 0.0, {}

    def run_alone(c, cases, work, n):
        """`claude plugin eval` with c as the only plugin and no baseline arm: (result or None, note)."""
        nonlocal spent
        (work / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (work / ".claude-plugin/plugin.json").write_text(json.dumps({"name": EVAL_PLUGIN, "version": "0.0.0"}))
        copy_component(c, work, c["name"], {})
        for case in cases:
            write_case(work / f"evals/{case['source']}-{case_key(case['prompt'])}", case["prompt"], c, c["name"])
        if dry or spent >= budget:
            return None, "" if dry else "; not run: budget spent"
        raw = work / "result.json"
        cmd = ["claude", "plugin", "eval", str(work), "--ablation", "none", "--json", str(raw), "--no-publish",
               "--trust-plugin", "--threshold", "0", "--runs", str(n), "--max-cost-usd", f"{budget - spent:.2f}"]
        cmd += ["--model", model] if model else []
        cmd += ["--judge-model", judge] if judge else []
        p = subprocess.run(cmd, capture_output=True, text=True)
        if not raw.exists():
            return None, f"; eval failed: {(p.stderr or p.stdout).strip()[-300:]}"
        res = json.loads(raw.read_text())
        spent += res.get("costUsd", 0)
        return res, ""

    for group, members in groups.items():
        keys = set().union(*(invoked_as(c, origin, bare[c["name"]] == 1) for c in members))
        cases, skipped = (fixed, 0) if calibrate else build_cases(members[0], keys, tasks, prompts, limit)
        explicit = any(t["slash"] for k in keys for t in tasks.get(k, []))
        for c in members:
            if c["kind"] not in KINDS:
                notes[c["id"]] = "unevaluated: hooks and MCP servers act across a session, not on one answer"
                continue
            if not cases:
                notes[c["id"]] = f"unevaluated: no usable cases ({skipped} fired tasks need local context)"
                continue
            src = Counter(x["source"] for x in cases)
            notes[c["id"]] = f"{len(cases)} cases {dict(src)}, {skipped} fired tasks skipped as context-dependent"
            ckey = tuple(sorted(case_key(x["prompt"]) for x in cases))
            if ckey not in baselines:  # look-alikes and controls on the same prompts share one placebo baseline
                baselines[ckey] = placebo_baseline(cases, placebo_runs, model, lambda n: run_alone(
                    placebo, cases, out_dir / f"placebo-{len(baselines)}", n))
            base, note = baselines[ckey]
            res, note2 = (base, "") if c["id"] == "control:placebo" else \
                run_alone(c, cases, out_dir / c["id"].replace(":", "__"), runs)
            if res is None or base is None:
                notes[c["id"]] += note2 or note or ("" if dry else "; placebo did not run")
                continue
            res = versus_placebo(res, base, runs, own=c["id"] == "control:placebo")
            rows.append(summarize(res, c, run, group, model, judge, explicit))
            for k, scores in pairwise(res, judge or "haiku").items():
                rows[-1]["per_case"][k]["pair"] = scores
            rows[-1]["v"] = EVAL_VERSION
            with open(STATE / "evals/history.jsonl", "a") as fh:
                fh.write(json.dumps(rows[-1]) + "\n")
    hist = eval_history()
    v = verdicts(hist, load_pins())
    ok, why = calibration(hist, model or "default", judge or "haiku")
    print(json.dumps({"run": run, "dir": str(out_dir), "spent_usd": round(spent, 4),
                      "calibration": {"ok": ok, "detail": why}, "notes": notes,
                      "fired": {r["id"]: r["fired"] for r in rows},
                      "verdicts": {i: v[i] for i in ids if i in v}}, indent=2))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profile").add_argument("--days", type=int, default=90)
    sub.add_parser("apply").add_argument("selection")
    sub.add_parser("restore")
    sub.add_parser("list").add_argument("plugin")
    a = sub.add_parser("add")
    a.add_argument("ids", nargs="+")
    a.add_argument("--disable", action="append", default=[], help="plugin@marketplace to disable after ingesting")
    a.add_argument("--trial", action="store_true", help="don't pin; the next /scav run drops them if unused")
    f = sub.add_parser("find", help="rank installed components (disabled plugins too) against a need")
    f.add_argument("need")
    f.add_argument("--limit", type=int, default=5)
    f.add_argument("--catalogs", action="store_true", help="also search known marketplaces' uninstalled plugins")
    f.add_argument("--github", action="store_true", help="also search SKILL.md files on GitHub (read-only, via gh)")
    sub.add_parser("fetch", help="shallow-clone a GitHub repo so its skills can be trial-added").add_argument("repo")
    sub.add_parser("unpin").add_argument("ids", nargs="+")
    e = sub.add_parser("eval", help="score components against vanilla Claude via `claude plugin eval`")
    e.add_argument("--component", action="append", default=[], help="id to evaluate (repeat; same kind+name = look-alikes)")
    e.add_argument("--max-cost-usd", type=float, default=5.0, help="total budget across all components")
    e.add_argument("--model")
    e.add_argument("--judge-model")
    e.add_argument("--runs", type=int, default=1, help="runs per case per arm (later runs pool per prompt)")
    e.add_argument("--cases", type=int, default=6, help="max cases per component")
    e.add_argument("--days", type=int, default=365)
    e.add_argument("--dry-run", action="store_true", help="write the cases, run nothing")
    e.add_argument("--calibrate", action="store_true", help="run the placebo/saboteur/partial controls instead (each vs the placebo)")
    args = ap.parse_args()
    {"profile": lambda: profile(args.days), "apply": lambda: apply(args.selection), "restore": restore,
     "list": lambda: list_plugin(args.plugin), "add": lambda: add(args.ids, args.disable, trial=args.trial),
     "find": lambda: find(args.need, args.limit, args.catalogs, args.github), "fetch": lambda: fetch(args.repo),
     "unpin": lambda: unpin(args.ids),
     "eval": lambda: eval_components(args.component, args.max_cost_usd, args.model, args.judge_model, args.runs,
                                     args.cases, args.days, args.dry_run, args.calibrate)}[args.cmd]()


if __name__ == "__main__":
    main()
