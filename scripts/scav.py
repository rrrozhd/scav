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
  scav.py find "NEED" --github   also searches GitHub for SKILL.md files and MCP/hook plugins (read-only, listing only)
  scav.py fetch OWNER/REPO       shallow-clones a repo into ~/.scav/github; its skills become trial-addable
  scav.py unpin ID...            lets later /scav runs drop them again
  scav.py eval [--component ID]  scores components against a placebo skill; history in ~/.scav/evals/
  scav.py eval --run-servers     also scores MCP servers (starts them, as you) against no server; hooks get usage only
  scav.py eval --calibrate       runs the controls that must pass before prune verdicts count
"""
import argparse, base64, fcntl, hashlib, json, math, os, random, re, shutil, statistics, subprocess, sys, time
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
        try:
            root = Path(entries[0]["installPath"])
            usable = root.is_dir()
        except (IndexError, KeyError, TypeError, OSError):
            continue  # a broken registry entry: skip that plugin, not every command
        if name != SELF and usable:
            out.append((name, pid, root, bool(enabled.get(pid))))
    for pj in sorted((claude / "plugins/synced").glob("*/*/.claude-plugin/plugin.json")):
        try:
            name = json.loads(pj.read_text())["name"]
        except (ValueError, KeyError, TypeError, OSError, RecursionError):
            continue
        if not safe_name(name) or name.startswith("."):
            continue  # Claude Code refuses such names; here they would become paths
        pid = f"{name}@synced"
        out.append((name, pid, pj.parent.parent, enabled.get(pid, True) is not False))
    return out


def read_local(path):
    return path.read_text(errors="ignore") if path.is_file() else None


def plugin_config(root, key, default, read=read_local):
    """(mapping, file) for a plugin's hooks ({event: groups}) or MCP servers ({name: config}). Like Claude Code, the
    default file loads first, then what plugin.json declares under `key` merges in: a path relative to the plugin
    root, an inline object, or an array of either (a later MCP name replaces an earlier one; hook groups add up).
    `file` is the last source that contributed. `read(path)` returns a file's text or None; github_entries passes
    one that reads from GitHub."""
    def parse(text):
        try:
            return json.loads(text or "{}")
        except (ValueError, RecursionError):
            return {}
    manifest = root / ".claude-plugin/plugin.json"
    declared = parse(read(manifest))
    declared = declared.get(key) if isinstance(declared, dict) else None
    sources = [root / default]
    for d in declared if isinstance(declared, list) else [] if declared is None else [declared]:
        if isinstance(d, str) and ".." not in Path(d).parts:  # Claude Code refuses paths that leave the plugin
            if os.path.normpath(root / d) not in [os.path.normpath(s) for s in sources if isinstance(s, Path)]:
                sources.append(root / d)
        elif isinstance(d, dict):
            sources.append(d)
    merged, src = {}, root / default
    for s in sources:
        # a file wraps the mapping in {"hooks": ...} / {"mcpServers": ...}; an inline object doesn't
        val = s if isinstance(s, dict) else parse(read(s))
        m = (val.get(key, val) if isinstance(s, Path) else val) if isinstance(val, dict) else {}
        if not isinstance(m, dict) or not m:
            continue
        src = manifest if isinstance(s, dict) else s
        for k, v in m.items():
            if key != "hooks":
                merged[k] = v
            elif isinstance(v, list):
                merged.setdefault(k, []).extend(v)
    # drop malformed entries here, so one broken plugin can't crash inventory or apply for all the others
    if key == "hooks":
        ok = lambda h: isinstance(h, dict) and isinstance(h.get("command", ""), str) and \
            (h.get("type") != "command" or h.get("command"))
        events = {e: [{**g, "hooks": [h for h in g["hooks"] if ok(h)]} for g in gs
                      if isinstance(g, dict) and isinstance(g.get("hooks"), list) and isinstance(g.get("matcher", ""), str)]
                  for e, gs in merged.items()}
        events = {e: [g for g in gs if g["hooks"]] for e, gs in events.items()}
        return {e: gs for e, gs in events.items() if gs}, src
    return {k: v for k, v in merged.items() if isinstance(v, dict)}, src


DESC_CAP = 400  # find divides overlap by sqrt(word count), so a long built description would rank itself down
SCRIPT_RE = re.compile(r"[\w.${}/-]+\.(?:sh|py|js|mjs|cjs|ts|rb|cmd)\b")


def plugin_about(root):
    """(plugin.json description, README lines) for building hook and MCP descriptions."""
    try:
        about = json.loads(read_local(root / ".claude-plugin/plugin.json") or "{}").get("description", "")
    except (ValueError, AttributeError, RecursionError):
        about = ""
    readme = [re.sub(r"[#*`>|_\[\]]|\(http\S*\)", "", ln).strip() for ln in (read_local(root / "README.md") or "").splitlines()]
    return about, [ln for ln in readme if ln]


def server_run(cfg):
    """What an MCP server config starts or calls: its command line or URL, as written."""
    args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
    return str(cfg.get("url") or " ".join(map(str, [cfg.get("command") or "", *args])).strip())


def mcp_desc(name, cfg, about, readme):
    """What an MCP server does, from disk only (nothing is started): the command or URL it runs, its plugin's
    description, and the README lines naming it.
    shortcut: tool names are not listed, since that needs the server running; add an opt-in launch if find misses."""
    mentions = [ln[:160] for ln in readme if name.lower() in ln.lower()][:3]
    run = server_run(cfg).replace("${CLAUDE_PLUGIN_ROOT}/", "")
    return f"MCP server {name} (runs {run or '-'}): {' '.join([about, *mentions]).strip()}"[:DESC_CAP]


def script_comment(path):
    """The first two comment lines of a hook script, without the shebang."""
    text = read_local(path) or ""
    lines = [re.sub(r"^\s*(?:#+|//+|/?\*+)\s?", "", ln).strip() for ln in text.splitlines()[:20]
             if re.match(r"\s*(?:#(?!!)|//|/?\*)", ln)]
    return " ".join([ln for ln in lines if ln][:2])


def hook_desc(events, about, root, src=None, read=read_local):
    """What a plugin's hooks do, from disk only: events and matchers, each hook's status message, the scripts it runs
    and their header comments, the hook file's own description, and the plugin's description."""
    try:
        own = json.loads(read(src) or "{}").get("description", "") if src and src.suffix == ".json" else ""
    except ValueError:
        own = ""
    on, said, scripts = [], [], []
    for event, groups in events.items():
        matchers = sorted({g.get("matcher") for g in groups if isinstance(g, dict) and g.get("matcher")})
        on.append(event + (f" ({', '.join(matchers)})" if matchers else ""))
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                said += [h[k] for k in ("statusMessage", "description") if h.get(k)]
                for s in SCRIPT_RE.findall(h.get("command", "")):
                    s = s.replace("${CLAUDE_PLUGIN_ROOT}/", "").replace("$CLAUDE_PLUGIN_ROOT/", "")
                    if Path(s).name not in scripts:
                        scripts.append(Path(s).name)
                        said.append(script_comment(root / s) if read is read_local else "")
    text = " ".join(dict.fromkeys(x for x in [own, *said] if x))
    return f"hooks on {', '.join(on)}: {text} Scripts: {', '.join(scripts) or '-'}. {about}"[:DESC_CAP]


def safe_name(n):
    """Can a component name from a manifest or frontmatter become part of a path in apply and eval?"""
    return isinstance(n, str) and bool(n) and "/" not in n and "\\" not in n and n not in (".", "..")


CLAUDE_FM_RE = re.compile(r"^---\s*\n([\s\S]*?)---\s*\n?")  # Claude Code 2.1.304's own frontmatter regex
NAME_LINE_RE = re.compile(r"""^(?:"name"|'name'|name):[ \t]+(.*)$""", re.M)


def raw_frontmatter(text):
    """The frontmatter block exactly as Claude Code cuts it: its closing --- need not start a line."""
    m = CLAUDE_FM_RE.match(text.lstrip("\ufeff"))
    return m.group(1) if m else ""


def skill_file(d):
    """d's SKILL.md in any letter case (a case-insensitive filesystem loads skill.md too), or None."""
    try:
        return next((f for f in sorted(d.iterdir()) if f.name.lower() == "skill.md" and f.is_file()), None)
    except OSError:
        return None


def skill_name(f, fallback):
    """A skill's invocation name: its frontmatter `name`, under any key quoting (Claude Code uses it in place of the
    folder name), else the folder name."""
    text = f.read_text(errors="ignore")
    n = frontmatter(text).get("name")  # bare key, including folded block scalars
    if n is None and (m := NAME_LINE_RE.search(raw_frontmatter(text))):  # a quoted "name" key
        v = m.group(1).split(" #")[0].strip()
        try:
            n = json.loads(v) if v.startswith('"') else v.strip("'")  # double quotes may hold escapes
        except ValueError:
            n = None
    return n if safe_name(n) else fallback


def scan_plugin(name, pid, root, enabled):
    comps = []
    def add(kind, cname, path, desc, tok=None):
        comps.append({"id": f"{kind}:{name}:{cname}", "kind": kind, "plugin": name, "pid": pid,
                      "name": cname, "path": str(path), "root": str(root), "enabled": enabled,
                      "desc": desc, "tok": max(1, (len(cname) + len(desc)) // 4) if tok is None else tok})
    try:
        decl = json.loads(read_local(root / ".claude-plugin/plugin.json") or "{}")
    except (ValueError, RecursionError):
        decl = {}
    decl = decl if isinstance(decl, dict) else {}
    def paths(key):  # a declared path or array of paths; Claude Code refuses ones that leave the plugin
        v = decl.get(key)
        return [root / x for x in (v if isinstance(v, list) else [v]) if isinstance(x, str) and ".." not in Path(x).parts]
    seen = set()
    def add_md(kind, cname, f):
        if not safe_name(cname):
            return  # e.g. a command map key like ../../x: it would become a path in apply and eval
        if f.is_file() and (kind, cname) not in seen:
            seen.add((kind, cname))
            desc = frontmatter(f.read_text(errors="ignore")).get("description", "")
            add(kind, cname, f.parent if kind == "skill" else f, desc)
    # skills: declared dirs add to skills/; each holds <name>/SKILL.md folders or is one folder with SKILL.md
    if decl.get("skills") is None and not (root / "skills").is_dir() and (f := skill_file(root)):
        add_md("skill", skill_name(f, name), f)  # a bare plugin that is one skill
    for d in [root / "skills", *paths("skills")]:
        subs = [skill_file(x) for x in sorted(d.iterdir()) if x.is_dir()] if d.is_dir() else []
        for f in [own] if (own := skill_file(d)) else [x for x in subs if x]:
            add_md("skill", skill_name(f, f.parent.name if f.parent != root else name), f)
    # agents and commands: declared paths replace the default dir; commands may also be a name -> {source} map
    for kind in ("agent", "command"):
        declared = decl.get(f"{kind}s")
        if kind == "command" and isinstance(declared, dict):  # inline `content` commands have no file to copy
            files = [(k, root / v["source"]) for k, v in declared.items()
                     if isinstance(v, dict) and isinstance(v.get("source"), str) and ".." not in Path(v["source"]).parts]
        else:
            where = paths(f"{kind}s") if declared is not None else [root / f"{kind}s"]
            files = [(f.stem, f) for w in where for f in ([w] if w.suffix == ".md" else sorted(w.glob("*.md")))]
        for cname, f in files:
            add_md(kind, cname, f)
    events, hsrc = plugin_config(root, "hooks", "hooks/hooks.json")
    servers, msrc = plugin_config(root, "mcpServers", ".mcp.json")
    about, readme = plugin_about(root) if events or servers else ("", [])
    if events:
        add("hook", "all", hsrc, hook_desc(events, about, root, hsrc), tok=0)
    for srv, cfg in servers.items():
        if not safe_name(srv):
            continue
        add("mcp", srv, msrc, mcp_desc(srv, cfg if isinstance(cfg, dict) else {}, about, readme), tok=0)
    return comps


def inventory(claude=CLAUDE, all_fetched=False):
    """Installed components, plus fetched-repo ones. A fetched repo can hold a thousand skills, so by default only the
    ones Scav holds (kept or trial) or pins are counted; find, add, apply and list pass all_fetched to see the rest."""
    fetched = github_components()
    if not all_fetched:
        held = set(load_manifest().get("origin", {}).values()) | load_pins()
        fetched = [c for c in fetched if c["id"] in held]
    comps = []
    for p in installed_plugins(claude):
        try:
            comps += scan_plugin(*p)
        except (AttributeError, TypeError, KeyError, ValueError, OSError, RecursionError) as e:  # one broken plugin can't hide the rest
            print(f"skipped {p[1]}: unreadable config ({e!r})", file=sys.stderr)
    return comps + fetched


GITHUB = "github"  # ~/.scav/github/<owner>__<repo>: shallow clones made by `fetch`
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def github_components():
    """Skills, agents and commands of fetched GitHub repos, as inventory rows. Hooks and MCP servers are left out:
    they run code as the user on every session, so an unreviewed repo never gets that far. Frontmatter hooks, which
    Claude Code registers when a skill or agent is invoked, are stripped when one is copied (safe_frontmatter)."""
    comps = []
    for d in sorted((STATE / GITHUB).glob("*__*")):
        owner, repo = d.name.split("__", 1)
        name = f"gh-{owner}-{repo}".lower()
        try:
            rows = [c for c in scan_plugin(name, f"github:{owner}/{repo}", d, False) if c["kind"] in KINDS]
            seen = {c["path"] for c in rows}
            # bare skill repos keep SKILL.md anywhere, not only skills/*/, and in any letter case
            for f in sorted(x for x in d.rglob("*") if x.name.lower() == "skill.md" and x.is_file()):
                if ".git" in f.parts or str(f.parent) in seen:
                    continue
                desc = frontmatter(f.read_text(errors="ignore")).get("description", "")
                cname = skill_name(f, f.parent.name if f.parent != d else repo.lower())
                rows.append({"id": f"skill:{name}:{cname}", "kind": "skill", "plugin": name,
                             "pid": f"github:{owner}/{repo}", "name": cname, "path": str(f.parent), "root": str(d), "enabled": False, "desc": desc,
                             "tok": max(1, (len(cname) + len(desc)) // 4)})
            # only plain frontmatter is copied (plain_frontmatter): anything Scav can't read exactly is left out
            rows = [c for c in rows if plain_frontmatter(read_local(
                skill_file(Path(c["path"])) if c["kind"] == "skill" else Path(c["path"])) or "", c["kind"])]
        except (AttributeError, TypeError, KeyError, ValueError, OSError, RecursionError) as e:
            print(f"skipped fetched {owner}/{repo}: unreadable ({e!r})", file=sys.stderr)
            continue
        comps += rows
    return comps


SAFE_KEYS = {"skill": ("description", "argument-hint"), "command": ("description", "argument-hint"),
             "agent": ("description", "model")}
TOOL_RE = re.compile(r"[A-Za-z_][\w-]*(?:\([^()\n]*\))?")


def agent_tools(fm):
    """An agent's `tools` as a list of tool names, [] if it declares none, None if they can't be read exactly."""
    v = fm.get("tools", "").strip()
    if not v:
        return []
    items = v[2:].split(" - ") if v.startswith("- ") else v.strip("[]").split(",")  # block list or flow/comma list
    items = [x.strip().strip("\"'") for x in items]
    return items if all(TOOL_RE.fullmatch(x) for x in items) else None


TOP_KEY_RE = re.compile(r"[A-Za-z][\w-]*:(?:[ \t].*)?")


def plain_frontmatter(text, kind):
    """May a fetched component be copied? Only if its frontmatter is plain YAML that Scav reads exactly as Claude Code
    does: bare top-level keys, no backslash (escapes can spell any key), no duplicate key, and a closing --- on its own
    line (Claude Code cuts at the first --- anywhere). It must also lose no restriction when rebuilt: no
    disallowed-tools, and an agent's tools must parse exactly, since an agent without `tools` gets them all.
    Anything else is left out rather than second-guessed."""
    head = text.lstrip("\ufeff")
    m = CLAUDE_FM_RE.match(head)
    if not m:
        return not head.startswith("---")  # no frontmatter at all is plain; an unclosed block is not
    raw = m.group(1)
    closes_on_own_line = re.match(r"---[ \t]*(?:\r?\n|$)", head[m.start(1) + len(raw):])
    if "\\" in raw or (raw and not raw.endswith("\n")) or not closes_on_own_line:
        return False
    keys = []
    for ln in raw.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#") or ln[0] in " \t":
            continue  # blank, comment, or nested under a top-level key
        if not TOP_KEY_RE.fullmatch(ln):
            return False  # quoted, tagged, anchored, flow or explicit (?) key
        keys.append(ln.split(":", 1)[0].lower())
    if len(keys) != len(set(keys)) or {"disallowed-tools", "disallowedtools"} & set(keys):
        return False
    return kind != "agent" or agent_tools(frontmatter(head)) is not None


def inert(text):
    """Text with Claude Code's shell directives (!`cmd` and ```! blocks) broken the way Claude Code breaks them."""
    return text.replace("`!", "` !").replace("!`", "! `")


def yaml_str(v):
    """A YAML double-quoted scalar with no run of dashes: Claude Code ends frontmatter at the first --- anywhere."""
    return re.sub(r"-{3,}", lambda m: "\\u002d" * len(m.group()), json.dumps(v, ensure_ascii=False))


def safe_frontmatter(text, kind, name):
    """A fetched component's frontmatter rebuilt from what Scav parsed itself: its name, description and a few inert
    keys (an agent also keeps its exact tool list). Everything else it declared, hooks or allowed-tools in any YAML
    spelling, is dropped rather than detected. The body after the original frontmatter is kept as it is."""
    head = text.lstrip("\ufeff")
    lines = head.split("\n")
    body = head
    if lines[0].strip() == "---":
        end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
        body = "\n".join(lines[end + 1:]) if end else head  # unclosed: keep it all, it now sits below ours
    fm = frontmatter(head)
    out = [] if kind == "command" else [f"name: {yaml_str(name)}"]
    out += [f"{k}: {yaml_str(fm[k])}" for k in SAFE_KEYS[kind] if fm.get(k)]
    if kind == "agent" and agent_tools(fm):
        out.append("tools: " + yaml_str(", ".join(agent_tools(fm))))
    return "---\n" + "\n".join(out) + "\n---\n" + inert(body)


def gh_json(args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(out.stderr.strip() or f"gh {args[0]} failed")
    return json.loads(out.stdout)


def gh_text(repo, path):
    """A file's text from GitHub, or None if it is missing or unreadable."""
    try:
        return base64.b64decode(gh_json(["api", f"repos/{repo}/contents/{path}"])["content"]).decode(errors="ignore")
    except (RuntimeError, KeyError, ValueError, TypeError):
        return None


def github_entries(need, limit=8):
    """SKILL.md files, and plugins or .mcp.json files offering MCP servers or hooks, on GitHub matching the need, as find
    rows. Read-only: nothing is cloned or installed. Descriptions come from each hit's own files; stars and last push
    are shown as trust signals. MCP and hook rows are listings only: their desc starts with the exact command or URL
    that would run, for the user to review, and `fetch` never ingests them."""
    words = sorted(set(WORD.findall(need.lower())) - STOP)
    if not words:
        return []
    rows, repos = [], {}
    def search(*query, n):
        try:  # each search on its own: code search allows about 10 a minute, so one may be refused
            return gh_json(["search", "code", *words, *query, "--limit", str(n), "--json", "repository,path"])
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as e:
            print(f"github search {' '.join(query)} skipped: {e}", file=sys.stderr)
            return []
    def status(repo):
        if repo not in repos:
            try:
                meta = gh_json(["api", f"repos/{repo}"])
                repos[repo] = f"github ★{meta['stargazers_count']} pushed {meta['pushed_at'][:10]}"
            except (RuntimeError, KeyError, ValueError):
                repos[repo] = None
        return repos[repo]
    def row(repo, path, kind, cname, desc):
        rid = f"gh:{repo}:{path}" + ("" if kind == "skill" else f"#{kind}")  # one plugin.json can declare both
        if status(repo) and not any(r["id"] == rid for r in rows):
            rows.append({"id": rid, "kind": kind, "plugin": repo, "name": cname, "desc": desc,
                         "tok": "-", "status": status(repo) + ("" if kind == "skill" else ", listing only")})
    for h in search("filename:SKILL.md", n=limit * 3):
        repo, path = h["repository"]["nameWithOwner"], h["path"]
        # filename: matches loosely (terraform-skill.md); only a real SKILL.md is loadable after fetch
        if len(rows) == limit * 2 or Path(path).name != "SKILL.md" or (text := gh_text(repo, path)) is None:
            continue
        fm = frontmatter(text)
        row(repo, path, "skill", fm.get("name") or Path(path).parent.name or repo.split("/")[1], fm.get("description", ""))
    hits = search("filename:plugin.json", "path:.claude-plugin", n=limit) + search("filename:.mcp.json", n=limit)
    texts = {}
    for h in hits:
        repo, path = h["repository"]["nameWithOwner"], Path(h["path"])
        if path.name not in ("plugin.json", ".mcp.json"):
            continue  # filename: also matches .mcp.json.archived
        root = path.parent.parent if path.name == "plugin.json" else path.parent
        read = lambda p: texts[(repo, p)] if (repo, p) in texts else texts.setdefault((repo, p), gh_text(repo, str(p)))
        try:  # remote files are untrusted: one malformed hit is skipped, not the whole search
            about = json.loads(read(path) or "{}").get("description", "") if path.name == "plugin.json" else ""
            servers, src = plugin_config(root, "mcpServers", path.name if path.name == ".mcp.json" else ".mcp.json", read)
            if servers:
                runs = "; ".join(f"{k}: {server_run(v)}" for k, v in servers.items())
                row(repo, str(src), "mcp", ",".join(servers), f"runs {runs}. {about}"[:DESC_CAP])
            events, src = plugin_config(root, "hooks", "hooks/hooks.json", read) if path.name == "plugin.json" else ({}, path)
            if events:
                cmds = {h["command"] for gs in events.values() for g in gs for h in g["hooks"] if h.get("command")}
                row(repo, str(src), "hook", "all", f"runs {'; '.join(sorted(cmds))}. " +
                    hook_desc(events, about, root, src, read))
        except (ValueError, AttributeError, TypeError, KeyError, RecursionError) as e:
            print(f"github hit {repo}/{path} skipped: {e!r}", file=sys.stderr)
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


def mcp_prefix(name):
    """`mcp__plugin_<plugin>_<server>` for a plugin MCP tool's name, else None."""
    return "__".join(name.split("__", 2)[:2]) if name.startswith("mcp__plugin_") and name.count("__") >= 2 else None


HOOK_OUTCOMES = {"hook_success": "ran", "hook_blocking_error": "blocked", "hook_non_blocking_error": "error"}


def hook_record(d):
    """(key, outcome) for a transcript line recording a hook run, else None. The key is `hook:<event>:<command>` with
    the command as configured, which is how invoked_as names a plugin's hooks. Outcome: ran, output (it printed
    something, e.g. added context), blocked, or error."""
    a = d.get("attachment") if d.get("type") == "attachment" else None
    if not isinstance(a, dict) or a.get("type") not in HOOK_OUTCOMES:
        return None
    err = a.get("blockingError")
    cmd = a.get("command") or (err.get("command") if isinstance(err, dict) else None)
    if not cmd:
        return None
    outcome = HOOK_OUTCOMES[a["type"]]
    return f"hook:{a.get('hookEvent')}:{cmd}", "output" if outcome == "ran" and str(a.get("stdout", "")).strip() else outcome


def parse_claude_line(line, uses, prompts, since=""):
    """Count Skill/Agent/slash-command invocations, plugin MCP tool calls and hooks that blocked or printed something,
    and collect user-typed prompts, from one transcript line."""
    if '"tool_use"' not in line and '"user"' not in line and '"hook_' not in line:
        return  # cheap prefilter; most lines are tool output
    try:
        d = json.loads(line)
    except ValueError:
        return
    if d.get("timestamp", "") < since:
        return  # resumed sessions keep old turns in a recently modified file
    msg = d.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if rec := hook_record(d):
        # shortcut: a hook that only ran says nothing about reliance; one that blocked or printed acted
        uses[rec[0]] += rec[1] in ("blocked", "output")
    elif d.get("type") == "assistant" and isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            inp = item.get("input") or {}
            if item.get("name") == "Skill" and inp.get("skill"):
                uses[inp["skill"].lstrip("/")] += 1
            elif item.get("name") in ("Agent", "Task") and inp.get("subagent_type"):
                uses[inp["subagent_type"]] += 1
            elif prefix := mcp_prefix(str(item.get("name", ""))):
                uses[prefix] += 1
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
    """Every name c may have been called by: qualified, its Scav alias, or its bare name when no other component shares it.
    An MCP server is called by its tools' prefix, a plugin's hooks by their (event, command) as configured or as
    ingested into Scav. Two plugins shipping the same hook command share its keys."""
    aliases = {k.split("#")[0] for k, v in origin.items() if v == c["id"]}
    if c["kind"] == "mcp":
        return {f"mcp__plugin_{c['plugin']}_{c['name']}"} | aliases
    if c["kind"] == "hook":
        return hook_keys(c)
    keys = {f"{c['plugin']}:{c['name']}"} | aliases
    if bare_unique:
        keys.add(c["name"])
    return keys


def hook_keys(c):
    keys = set()
    for event, groups in plugin_config(Path(c["root"]), "hooks", "hooks/hooks.json")[0].items():
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                if h.get("type") == "command" and h.get("command"):
                    ingested = ingested_command(h["command"], c["root"], isinstance(h.get("args"), list))
                    keys |= {f"hook:{event}:{h['command']}", f"hook:{event}:{ingested}"}
                    if h.get("statusMessage"):  # transcripts log a hook's status message in place of its command
                        keys.add(f"hook:{event}:{h['statusMessage']}")
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
               "top_uses": dict([u for u in uses.most_common() if not u[0].startswith("hook:")][:40]),
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


def write_atomic(path, text):
    """Write via a temp file and os.replace, so a failed write never leaves a truncated file behind."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def save_manifest(m):
    write_atomic(STATE / "manifest.json", json.dumps(m, indent=2))


def scav_source(claude=CLAUDE):
    """The Scav marketplace's source dir: installs copy from here, so ingest writes here, not into the cache."""
    try:
        return Path(json.loads((claude / "plugins/known_marketplaces.json").read_text())[SELF]["installLocation"])
    except (FileNotFoundError, KeyError):
        return Path(__file__).resolve().parent.parent


def clear_generated(dest, manifest):
    for rel in manifest.get("generated", []):
        p = dest / rel
        if not p.parent.resolve().is_relative_to(Path(dest).resolve()):
            continue  # a path that escapes Scav's dir is never Scav's to delete
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
        if any(p.is_symlink() for p in [local, *local.parents] if p.is_relative_to(vendor)):
            return str(target)  # a link copied with the skill: copying through it would write outside Scav
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, local)
        return os.path.relpath(local, md_dir)
    return REL_PATH.sub(fix, text)


def component_rel(kind, n):
    return f"skills/{n}" if kind == "skill" else f"{kind}s/{n}.md"


def copy_component(c, dest, n, renames):
    """Copy one skill/agent/command into plugin dir `dest` as `n`, with paths fixed up. Returns its relative path."""
    root, src = c["root"], Path(c["path"])
    rel = component_rel(c["kind"], n)
    if c["kind"] == "skill":
        if (dest / rel).is_symlink():
            (dest / rel).unlink()
        shutil.rmtree(dest / rel, ignore_errors=True)  # left over by an interrupted apply, so not in any manifest
        shutil.copytree(src, dest / rel, symlinks=True, ignore=shutil.ignore_patterns("node_modules", ".git"))
        mds = [f for f in (dest / rel).rglob("*") if f.suffix.lower() == ".md"]  # SKILL.MD loads on macOS too
    else:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        if (dest / rel).is_symlink():
            (dest / rel).unlink()  # copy2 would write through it
        shutil.copy2(src, dest / rel)
        mds = [dest / rel]
    for md in mds:
        if md.is_symlink():  # copytree keeps links; write a copy, never through the link
            body = md.read_text(errors="ignore")
            md.unlink()
            md.write_text(body)
        if not md.resolve().is_relative_to((dest / rel).resolve()):
            continue  # inside a linked dir: the file belongs to its source
        src_dir = (src / md.relative_to(dest / rel)).parent if c["kind"] == "skill" else src.parent
        vendor = dest / rel if c["kind"] == "skill" else None
        text = relink(rewrite(md.read_text(errors="ignore"), root, renames), src_dir, root, vendor, md.parent)
        main = md.parent == dest / rel and md.name.lower() == "skill.md" if c["kind"] == "skill" else md == dest / rel
        if c.get("pid", "").startswith("github:"):  # unreviewed repo: keep only fields Scav parsed, no shell lines
            text = safe_frontmatter(text, c["kind"], n) if main else inert(text)
            if main and c["kind"] == "skill" and md.name != "SKILL.md":
                md.unlink()  # skill.md / SKILL.MD: written back under the one spelling every OS loads
                md = md.with_name("SKILL.md")
        elif n != c["name"] and main:
            text = re.sub(r"^name:.*$", f"name: {n}", text, count=1, flags=re.M)
        md.write_text(text)
    if c.get("pid", "").startswith("github:") and c["kind"] == "skill":  # relink vendored more files into _ext/
        for f in (dest / rel / "_ext").rglob("*"):
            if f.suffix.lower() == ".md" and f.is_file() and not f.is_symlink():
                f.write_text(inert(f.read_text(errors="ignore")))
    return rel


HELD = []  # non-empty while this process holds ~/.scav/lock


def exclusive(fn):
    """Run fn holding ~/.scav/lock: two applies, restores or pin edits interleaving could lose pins or leave output
    no manifest tracks. Reentrant within the process, since add calls apply."""
    def run(*args, **kwargs):
        if HELD:
            return fn(*args, **kwargs)
        STATE.mkdir(exist_ok=True)
        with open(STATE / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            HELD.append(lock)
            try:
                return fn(*args, **kwargs)
            finally:
                HELD.pop()
    return run


@exclusive
def apply(selection_path, claude=CLAUDE, dest=None):
    sel = json.loads(Path(selection_path).read_text())
    comps = {c["id"]: c for c in inventory(claude, all_fetched=True)}
    pins = load_pins()
    stale_pins = pins - comps.keys()  # e.g. the source plugin was uninstalled; warn, don't block
    keep = set(sel.get("keep", [])) | (pins & comps.keys())
    prior = load_manifest()
    # held before (or dropped as gone before), source since removed
    gone = (keep - comps.keys()) & (set(prior.get("origin", {}).values()) | set(prior.get("gone", [])))
    keep -= gone
    missing = keep - comps.keys()
    if missing:
        sys.exit(f"unknown component ids: {sorted(missing)[:10]}")
    dest = Path(dest) if dest else scav_source(claude)
    STATE.mkdir(exist_ok=True)
    # trials stay trials while kept unpinned; a selection that leaves one out drops it like any unpinned component
    trial = ((set(prior.get("trial", [])) | set(sel.get("trial", []))) & keep) - pins

    chosen = [comps[i] for i in sorted(keep)]
    counts = Counter((c["kind"], c["name"].lower()) for c in chosen if c["kind"] in KINDS)
    prefixed = lambda c: counts[(c["kind"], c["name"].lower())] > 1 or c["name"].lower() in RESERVED
    newname = {c["id"]: f"{c['plugin']}-{c['name']}" if prefixed(c) else c["name"] for c in chosen if c["kind"] in KINDS}
    renames = {f"{comps[i]['plugin']}:{comps[i]['name']}": f"{SELF}:{n}" for i, n in newname.items()}
    clash = [k for k, v in Counter((comps[i]["kind"], n.lower()) for i, n in newname.items()).items() if v > 1]
    unsafe = [n for n in newname.values() if "/" in n or "\\" in n or n in (".", "..")]
    if unsafe:  # names come from plugin manifests; one like ../../x must not become a path outside Scav
        sys.exit(f"unsafe component names: {unsafe}")
    origin = {}
    for i, n in newname.items():  # a skill and an agent may share a name; each keeps its own origin key
        origin[f"{SELF}:{n}" if f"{SELF}:{n}" not in origin else f"{SELF}:{n}#{comps[i]['kind']}"] = i
    warnings = [f"pinned but not installed: {i}" for i in sorted(stale_pins)]
    warnings += [f"dropped, source no longer installed: {i}" for i in sorted(gone - stale_pins)]

    # everything that can fail on a source's config happens before the previous apply is cleared
    hooks = {}
    for c in chosen:
        if c["kind"] == "hook":
            for event, groups in plugin_config(Path(c["root"]), "hooks", "hooks/hooks.json")[0].items():
                for g in groups:
                    for h in g.get("hooks", []):
                        if h.get("type") == "command":
                            exec_form = isinstance(h.get("args"), list)
                            h["command"] = ingested_command(h["command"], c["root"], exec_form)
                            if exec_form:
                                h["args"] = [rewrite(a, c["root"], {}) if isinstance(a, str) else a for a in h["args"]]
                    hooks.setdefault(event, []).append(g)
            origin[f"hooks.json:{c['plugin']}"] = c["id"]
    servers = {}
    for c in chosen:
        if c["kind"] == "mcp":
            n = c["name"] if c["name"] not in servers else f"{c['plugin']}-{c['name']}"
            if n in servers:  # e.g. a:x and b:x, where b-x is also a server's own name
                clash.append(("mcp", n))
                continue
            servers[n] = server_config(c)
            origin[f"mcp__plugin_{SELF}_{n}"] = c["id"]  # the prefix its tools are called by, as invoked_as reads it
    if clash:  # e.g. a:x and b:x become a-x, which c:a-x already is; copying would overwrite one with another
        sys.exit(f"ingested names clash: {clash}; leave one of them out")

    generated = [component_rel(comps[i]["kind"], n) for i, n in newname.items()]
    generated += (["hooks/hooks.json"] if hooks else []) + ([".mcp.json"] if servers else [])
    settings = claude / "settings.json"
    try:  # sources the user had turned off before Scav: restore must leave them off
        already_off = {k for k, v in json.loads(settings.read_text()).get("enabledPlugins", {}).items() if v is False}
    except (OSError, ValueError, AttributeError):
        already_off = set()
    if settings.exists() and not prior:  # back up once per apply..restore cycle
        shutil.copy2(settings, STATE / "settings.pre-scav.json")
    linked = [r for r in ("skills", "agents", "commands", "hooks", "hooks/hooks.json", ".mcp.json")
              if (dest / r).is_symlink()]
    if linked:  # apply deletes and writes under these; through a link that would reach outside Scav
        sys.exit(f"Scav output paths are symlinks: {linked}; replace them with plain dirs/files")
    clear_generated(dest, prior)
    save_manifest(
        {"applied_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "dest": str(dest), "generated": generated,
         "origin": origin, "trial": sorted(trial), "gone": sorted(gone),
         # every source disabled since the last restore, so restore re-enables all of them, not just the latest
         "disable": sorted(set(prior.get("disable", [])) | (set(sel.get("disable", [])) - already_off))})
    for i, n in newname.items():
        copy_component(comps[i], dest, n, renames)
    if hooks:
        (dest / "hooks").mkdir(exist_ok=True)
        (dest / "hooks/hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2))
    if servers:
        (dest / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2))
    bump_version(dest)
    print(json.dumps({"ingested": len(origin), "hooks_from": [c["plugin"] for c in chosen if c["kind"] == "hook"],
                      "mcp": list(servers), "trial": sorted(trial),
                      "scav_always_on_tokens": sum(comps[i]["tok"] for i in keep if comps[i]["kind"] in KINDS),
                      "warnings": warnings}, indent=2))


def ingested_command(cmd, root, exec_form=False):
    """A hook command as Scav declares it. Shell form: scripts that read $CLAUDE_PLUGIN_ROOT must keep seeing their
    source plugin, so it is exported first. Exec form (with `args`) runs no shell, so only the path is filled in.
    shortcut: an exec-form script reading $CLAUDE_PLUGIN_ROOT from its environment sees Scav's root."""
    return rewrite(cmd, root, {}) if exec_form else f"export CLAUDE_PLUGIN_ROOT='{root}'; " + rewrite(cmd, root, {})


def server_config(c):
    """MCP server c's config with its plugin root filled in, as Scav (or an eval plugin) declares it."""
    cfg = plugin_config(Path(c["root"]), "mcpServers", ".mcp.json")[0][c["name"]]
    root = json.dumps(c["root"])[1:-1]  # JSON-escaped, so a root with a quote or backslash stays one string
    cfg = json.loads(json.dumps(cfg).replace("${CLAUDE_PLUGIN_ROOT}", root).replace("$CLAUDE_PLUGIN_ROOT", root))
    cfg["env"] = {**(cfg.get("env") if isinstance(cfg.get("env"), dict) else {}), "CLAUDE_PLUGIN_ROOT": c["root"]}
    return cfg


def bump_version(dest):
    # convention: Scav's patch number counts applies, so `claude plugin update` sees a new version
    for rel, get in ((".claude-plugin/plugin.json", lambda d: d),
                     (".claude-plugin/marketplace.json", lambda d: d["plugins"][0])):
        p = dest / rel
        if p.exists():
            d = json.loads(p.read_text())
            major, minor, patch = get(d).get("version", "0.1.0").split(".")
            get(d)["version"] = f"{major}.{minor}.{int(patch) + 1}"
            write_atomic(p, json.dumps(d, indent=2) + "\n")


@exclusive
def restore():
    """Remove ingested components and re-enable the plugins Scav absorbed.
    Re-enables through the CLI rather than copying settings.pre-scav.json back, which would undo later settings edits."""
    m = load_manifest()
    if not m:
        sys.exit("nothing to restore: no ~/.scav/manifest.json")
    clear_generated(Path(m["dest"]), m)
    bump_version(Path(m["dest"]))
    failed = [pid for pid in m.get("disable", []) if subprocess.run(["claude", "plugin", "enable", pid]).returncode]
    if failed:  # keep the manifest so a retry still knows what is disabled
        save_manifest({**m, "generated": [], "origin": {}, "disable": failed})
        sys.exit(f"ingested components removed, but could not re-enable {failed}; run restore again")
    (STATE / "manifest.json").unlink()
    print(f"restored: ingested components removed, re-enabled {m.get('disable', [])}; "
          f"full settings backup at {STATE / 'settings.pre-scav.json'}")


PINS = "pins.json"


def load_pins():
    p = STATE / PINS
    return set(json.loads(p.read_text())) if p.exists() else set()


def save_pins(pins):
    STATE.mkdir(exist_ok=True)
    write_atomic(STATE / PINS, json.dumps(sorted(pins), indent=2))


def list_plugin(plugin, claude=CLAUDE):
    """One plugin's components, for matching a plain-language request. `plugin` is name or name@marketplace."""
    name = plugin.split("@")[0]
    rows = [c for c in inventory(claude, all_fetched=True) if c["plugin"] == name]
    if not rows:
        sys.exit(f"{plugin} is not installed")
    for c in rows:
        print(f"{c['id']}\t{c['tok']}\t{c['path']}\t{c['desc'][:200]}")


@exclusive
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
    write_atomic(path, json.dumps(sel, indent=2))
    apply(path, claude=claude, dest=dest)
    for alias, i in load_manifest()["origin"].items():
        if i in ids:
            print(f"{i} loads as {alias.split('#')[0]}")


@exclusive
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
EVAL_VERSION = 5  # history rows from another method (vs vanilla, blind judge, rubric, unscreened, pre-MCP) don't pool
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
    """{invoked name: [task, ...]}: what each Skill/Agent/slash-command invocation or plugin MCP tool call worked on,
    and what each hook run did. A task is {text, standalone, slash, cwd}. An agent works on its brief, a slash command
    on its arguments, a skill on its args or the user's last prompt, an MCP tool on that prompt. That prompt stands
    alone only if it opened the session. A hook run (key from hook_record) is {hook: outcome, session}."""
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
                if '"tool_use"' not in line and '"user"' not in line and '"hook_' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("timestamp", "") < since:
                    continue
                cwd = d.get("cwd", "")
                if rec := hook_record(d):
                    out.setdefault(rec[0], []).append({"hook": rec[1], "session": d.get("sessionId", f.stem)})
                elif text := user_text(d):
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
                        elif name := mcp_prefix(str(item.get("name", ""))):
                            own = None
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
                if (diff := git("-C", repo, *args)) is not None:
                    parts.append(f"$ git {' '.join(args)}\n" + diff)
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
    """One case in `claude plugin eval` format: a rubric scored in both arms plus a with-only did-it-fire check.
    An MCP server fired if the trace has a call to one of its tools, and was up if the run's init line says it
    connected. Its tools are granted by --allow-tools, not allowed_tools (probed 2.1.304)."""
    tool, field = ("Agent", "subagent_type") if c["kind"] == "agent" else ("Skill", "skill")
    (path / "graders").mkdir(parents=True, exist_ok=True)
    (path / "prompt.md").write_text(
        f"---\nmax_turns: 8\nallowed_tools: [Read, Glob, Grep, Skill, Agent]\nappend_system_prompt: '{NUDGE}'\n---\n\n"
        + prompt + "\n")
    if c["kind"] == "mcp":
        # tool_used needs an exact tool name; the trace's init line lists tool names bare, a call has "name":
        (path / "graders/fired.md").write_text(
            f"---\ntype: regex\ntarget: trace\npattern: '\"name\":\"mcp__plugin_{EVAL_PLUGIN}_{re.escape(n)}__'\n"
            "arm: with-only\n---\n")
        (path / "graders/up.md").write_text(
            f"---\ntype: regex\ntarget: trace\npattern: '\"name\":\"plugin:{EVAL_PLUGIN}:{re.escape(n)}\",\"status\":"
            "\"connected\"'\narm: with-only\n---\n")
        # its evidence is the run's trace: pairwise shows the judge the tool results, which otherwise read as invented
        (path / "graders/trace.md").write_text(
            "---\ntype: llm\nfocus: trace\narm: with-only\n---\n\nPASS if the trace shows at least one tool call.\n")
    else:
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
    src = skill_file(Path(c["path"])) if c["kind"] == "skill" else Path(c["path"]) if c["kind"] in KINDS else None
    return {"id": c["id"], "run": run, "group": group, "model": model or "default", "judge": judge or "haiku",
            "explicit": explicit, "per_case": per_case, "fired": f"{fired_runs}/{with_runs}",
            "cost_with": mean(cw), "cost_without": mean(cwo), "tok": c["tok"],
            "invoke_tok": len(src.read_text(errors="ignore")) // 4 if src and src.exists() else 0,
            "partial": bool(res.get("partial")), "baseline": "no-server" if c["kind"] == "mcp" else "placebo"}


PAIR_SYSTEM = ("You compare two answers to one user request. The better answer does what was asked, is specific to "
               "the request, invents nothing it could not know, and has no padding. Length alone is not quality. "
               "An answer may come with the tool results it saw; facts taken from those are not invented.")
TOOL_RESULTS_CAP = 4000
PAIR_SCHEMA = json.dumps({"type": "object", "properties": {"better": {"enum": ["A", "B", "tie"]}},
                          "required": ["better"]})


SPEND = {"usd": 0.0, "cap": math.inf}  # every model call this process makes; eval caps it at --max-cost-usd


def claude_json(system, text, schema, model):
    """Structured output from one `claude -p` call with no tools and no user settings, or None (also once SPEND is
    over its cap: callers already treat None as a failed call)."""
    if SPEND["usd"] >= SPEND["cap"]:
        return None
    p = subprocess.run(["claude", "-p", "--model", model, "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                        "--no-session-persistence", "--system-prompt", system, "--json-schema", schema,
                        "--output-format", "json"], input=text, capture_output=True, text=True)
    try:
        out = json.loads(p.stdout)
        SPEND["usd"] += out.get("total_cost_usd") or 0
        return out["structured_output"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def ask_judge(request, a, b, judge):
    """'A', 'B' or 'tie', or None when the call failed or its verdict is unreadable."""
    text = (f"<request>\n{request}\n</request>\n\n<answer_A>\n{a}\n</answer_A>\n\n<answer_B>\n{b}\n</answer_B>\n\n"
            "Which answer better serves the request?")
    return (claude_json(PAIR_SYSTEM, text, PAIR_SCHEMA, judge) or {}).get("better")


ANSWERABLE_SYSTEM = ("You screen user requests for an offline test. The assistant answering will have no files, no "
                     "repository, no shell, no servers, no memory and no earlier conversation: only the request text. "
                     "A request is answerable only if a complete, useful written answer needs nothing else: not an "
                     "action on a system (deploy, copy, restart, fix), not existing code, documents, logs or project "
                     "state it refers to, and not the current session or earlier discussion. When in doubt, it is not.")
ANSWERABLE_SCHEMA = json.dumps({"type": "object", "properties": {"answerable": {"type": "boolean"}},
                                "required": ["answerable"]})


def answerable(prompt, model="sonnet", tools=""):
    """Can a good answer be written from the request text alone? Cached per prompt in ~/.scav/evals/.
    Regex catches paths and "this repo"; this catches "our current work", "on my server", "the log below" with none.
    With `tools` (an MCP server's description), the answer may also use that server, and nothing else local."""
    path = STATE / "evals/answerable.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    key = case_key(tools + prompt)
    if key not in cache:
        system = ANSWERABLE_SYSTEM + (f" Exception: the assistant can call the tools of this one MCP server, and a "
                                      f"request that needs only those tools is answerable: {tools}" if tools else "")
        out = claude_json(system, f"<request>\n{prompt}\n</request>\n\nCan a good answer be written from "
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
    def answer(r):
        text = next((g.get("evidence", "") for g in r.get("graders", []) if g.get("evidence") and g["name"] != "trace"), "")
        seen = tool_results(r)
        return text + (f"\n\n<tool_results_it_saw>\n{seen[:TOOL_RESULTS_CAP]}\n</tool_results_it_saw>" if seen else "")
    for case in res.get("cases", []):
        withs = [r for r in case.get("arms", {}).get("with", []) if not r.get("error")]
        withouts = [r for r in case.get("arms", {}).get("without", []) if not r.get("error")]
        request = case.get("promptMarkdown", "")
        for w, wo in zip(withs, withouts):
            if not invoked(w):
                continue
            first, second = ask(request, answer(w), answer(wo), judge), ask(request, answer(wo), answer(w), judge)
            if first is None or second is None:
                continue  # a failed judge call (auth, rate limit) must not count as a tie toward prune
            first, second = {"A": 1, "B": -1}.get(first, 0), {"A": -1, "B": 1}.get(second, 0)
            out.setdefault(case["name"].split("-", 1)[1], []).append((first + second) / 2)
    return out


def tool_results(run):
    """The text of the tool results in a run's trace, read from its `trace` grader's evidence (MCP cases only)."""
    out = []
    for ln in next((g.get("evidence") or "" for g in run.get("graders", []) if g.get("name") == "trace"), "").splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        content = (d.get("message") or {}).get("content") if d.get("type") == "user" else None
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                c = item.get("content")
                out.append(c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("text")))
    return "\n".join(x for x in out if x)


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
            for k in sorted((k for k in ids if cost(k) < cost(m) and setup[k] == setup[m]), key=cost):
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


def hook_report(c, tasks, comps):
    """What hook component c did in past sessions: runs by outcome (ran, output, blocked, error) and sessions. Other
    plugins shipping the same command are listed under shared_with: their runs can't be told apart from c's."""
    keys = hook_keys(c)
    done = [t for k in keys for t in tasks.get(k, [])]
    out = {**Counter(t["hook"] for t in done), "sessions": len({t["session"] for t in done})}
    shared = sorted(o["id"] for o in comps if o["kind"] == "hook" and o["id"] != c["id"] and hook_keys(o) & keys)
    return {**out, "shared_with": shared} if shared else out


def eval_components(ids, budget, model=None, judge=None, runs=1, limit=6, days=365, dry=False, calibrate=False,
                    run_servers=False, claude=CLAUDE):
    SPEND.update(usd=0.0, cap=budget)  # before anything screens a prompt
    comps = {c["id"]: c for c in inventory(claude)}
    origin = load_manifest().get("origin", {})
    run = time.strftime("%Y-%m-%dT%H-%M-%S") + f"-{os.getpid()}"  # two evals in one second get their own dirs
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
    rows, notes, baselines, hooks = [], {}, {}, {}

    def run_alone(c, cases, work, n):
        """`claude plugin eval` with c as the only plugin: (result or None, note). A skill or agent runs with no
        baseline arm (the placebo is its baseline); an MCP server runs against the no-plugin arm of the same eval,
        with its real server started and its tools granted."""
        (work / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (work / ".claude-plugin/plugin.json").write_text(json.dumps({"name": EVAL_PLUGIN, "version": "0.0.0"}))
        if c["kind"] == "mcp":
            (work / ".mcp.json").write_text(json.dumps({"mcpServers": {c["name"]: server_config(c)}}, indent=2))
        else:
            copy_component(c, work, c["name"], {})
        for case in cases:
            write_case(work / f"evals/{case['source']}-{case_key(case['prompt'])}", case["prompt"], c, c["name"])
        if dry or SPEND["usd"] >= budget:
            return None, "" if dry else "; not run: budget spent"
        raw = work / "result.json"
        raw.unlink(missing_ok=True)  # a stale result must never pass for this run's
        cmd = ["claude", "plugin", "eval", str(work), "--json", str(raw), "--no-publish",
               "--trust-plugin", "--threshold", "0", "--runs", str(n),
               "--max-cost-usd", f"{budget - SPEND['usd']:.2f}"]
        if c["kind"] == "mcp":
            print(f"starting MCP server {c['name']} as you: {server_run(server_config(c))}", file=sys.stderr)
            cmd += ["--ablation", "with-without", "--allow-real-servers",
                    "--allow-tools", f"mcp__plugin_{EVAL_PLUGIN}_{c['name']}__*"]
        else:
            cmd += ["--ablation", "none"]
        cmd += ["--model", model] if model else []
        cmd += ["--judge-model", judge] if judge else []
        p = subprocess.run(cmd, capture_output=True, text=True)
        if not raw.exists():
            return None, f"; eval failed: {(p.stderr or p.stdout).strip()[-300:]}"
        res = json.loads(raw.read_text())
        SPEND["usd"] += res.get("costUsd", 0)
        return res, ""

    for group, members in groups.items():
        keys = set().union(*(invoked_as(c, origin, bare[c["name"]] == 1) for c in members))
        if members[0]["kind"] == "hook":
            for c in members:
                notes[c["id"]] = ("no verdict: a hook acts across whole sessions, so there is no single answer to "
                                  "judge; `hooks` shows what it did in past sessions")
                hooks[c["id"]] = hook_report(c, tasks, comps.values())
            continue
        if members[0]["kind"] == "mcp" and not run_servers:
            for c in members:
                notes[c["id"]] = (f"unevaluated: --run-servers starts `{server_run(server_config(c))}` as you and lets "
                                  "the model call all its tools, including ones that write or send, on your past prompts")
            continue
        screen = (lambda t: answerable(t, tools=members[0]["desc"])) if members[0]["kind"] == "mcp" else None
        cases, skipped = (fixed, 0) if calibrate else build_cases(members[0], keys, tasks, prompts, limit, screen)
        explicit = any(t.get("slash") for k in keys for t in tasks.get(k, []))
        for c in members:
            if not cases:
                notes[c["id"]] = f"unevaluated: no usable cases ({skipped} fired tasks need local context)"
                continue
            src = Counter(x["source"] for x in cases)
            notes[c["id"]] = f"{len(cases)} cases {dict(src)}, {skipped} fired tasks skipped as context-dependent"
            if c["kind"] == "mcp":  # no placebo server exists, so its baseline is the same eval's no-server arm
                res, note = run_alone(c, cases, out_dir / c["id"].replace(":", "__"), runs)
                if res is None:
                    notes[c["id"]] += note
                    continue
                if not any(g.get("name") == "up" and g.get("passed") for case in res.get("cases", [])
                           for r in case.get("arms", {}).get("with", []) for g in r.get("graders", [])):
                    # an absent server would read as "never fired"; record nothing rather than a false idle
                    notes[c["id"]] += "; unevaluated: the server never connected in the eval sandbox (it may need " \
                                      "OAuth, a network, or local state)"
                    continue
            else:
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
            # a haiku judge took tool-sourced facts for invention even when shown the tool results (probed 2026-10-20)
            jm = judge or ("sonnet" if c["kind"] == "mcp" else None)
            rows.append(summarize(res, c, run, group, model, jm, explicit))
            for k, scores in pairwise(res, jm or "haiku").items():
                rows[-1]["per_case"][k]["pair"] = scores
            rows[-1]["v"] = EVAL_VERSION
            with open(STATE / "evals/history.jsonl", "a") as fh:
                fh.write(json.dumps(rows[-1]) + "\n")
    hist = eval_history()
    v = verdicts(hist, load_pins())
    ok, why = calibration(hist, model or "default", judge or "haiku")
    print(json.dumps({"run": run, "dir": str(out_dir), "spent_usd": round(SPEND["usd"], 4),
                      "calibration": {"ok": ok, "detail": why}, "notes": notes,
                      "fired": {r["id"]: r["fired"] for r in rows},
                      "verdicts": {i: v[i] for i in ids if i in v}, "hooks": hooks}, indent=2))


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
    f.add_argument("--github", action="store_true", help="also search GitHub for SKILL.md files and MCP/hook plugins (read-only, via gh)")
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
    e.add_argument("--dry-run", action="store_true", help="write the cases, run no evals (uncached prompts are still screened by a model call)")
    e.add_argument("--calibrate", action="store_true", help="run the placebo/saboteur/partial controls instead (each vs the placebo)")
    e.add_argument("--run-servers", action="store_true", help="evaluate MCP servers: starts each one's real server, as you")
    args = ap.parse_args()
    {"profile": lambda: profile(args.days), "apply": lambda: apply(args.selection), "restore": restore,
     "list": lambda: list_plugin(args.plugin), "add": lambda: add(args.ids, args.disable, trial=args.trial),
     "find": lambda: find(args.need, args.limit, args.catalogs, args.github), "fetch": lambda: fetch(args.repo),
     "unpin": lambda: unpin(args.ids),
     "eval": lambda: eval_components(args.component, args.max_cost_usd, args.model, args.judge_model, args.runs,
                                     args.cases, args.days, args.dry_run, args.calibrate, args.run_servers)}[args.cmd]()


if __name__ == "__main__":
    main()
