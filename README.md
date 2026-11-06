# Claude Code skills from Upshot Consultancy

A Claude Code plugin marketplace. Each plugin holds one or more skills and installs on its own.

## Install

In Claude Code, add the marketplace once:

```
/plugin marketplace add upshotconsultancy/claude-skills
```

Then install the plugins you want:

```
/plugin install <plugin>@upshot
```

To update later, run `/plugin marketplace update upshot`.

You can also install a skill by hand. Copy its folder, `plugins/<plugin>/skills/<skill>/`, into
`~/.claude/skills/` to use it in every project, or into `.claude/skills/` in one repo.

## Plugins

| Plugin | What it does |
|---|---|
| [code-mode](plugins/code-mode/) | Claude writes a script that calls your MCP tools, CLIs and APIs and reads only the answer, not every result. Cuts tokens on tasks with many calls or large results. |

## Adding a plugin

1. Create `plugins/<name>/` with:
   - `.claude-plugin/plugin.json`: name, description, version, author, license
   - `skills/<skill>/SKILL.md`: frontmatter `name` and `description`, plus any scripts the skill uses
   - `README.md`: what it does, requirements, limits
   - `tests/`, if it ships code
2. In `SKILL.md`, refer to bundled files as `${CLAUDE_SKILL_DIR}/<file>`, so the skill works both
   as a plugin and when copied by hand.
3. Add the plugin to `.claude-plugin/marketplace.json` and to the table above.
4. Check it:

   ```bash
   claude plugin validate .
   claude plugin validate plugins/<name>
   ```

5. Raise `version` in both `plugin.json` and `marketplace.json` whenever a plugin changes, so
   installed copies update.

## Licence

MIT, unless a plugin says otherwise. See [LICENSE](LICENSE).
