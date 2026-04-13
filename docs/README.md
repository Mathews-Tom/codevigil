# codevigil documentation

Start here. The repository [README](../README.md) is the front-door pitch and quickstart; everything below is the deep material.

## By task

**I want to install codevigil.** → [installation.md](installation.md)

**I just installed it. What now?** → [getting-started.md](getting-started.md)

**I need every flag for `codevigil watch` / `report` / `export` / `config check`.** → [cli.md](cli.md)

**I want to write a `~/.config/codevigil/config.toml` and need the key reference.** → [configuration.md](configuration.md)

**I want to know what each metric measures and how to interpret WARN / CRITICAL signals.** → [collectors.md](collectors.md)

**I'm evaluating codevigil for security and want to know what the privacy guarantees actually are.** → [privacy.md](privacy.md)

**I want to understand the architecture and the plugin boundaries.** → [design.md](design.md)

**What changed between releases?** → [../CHANGELOG.md](../CHANGELOG.md)

## By doc taxonomy

The docs follow the [Diátaxis framework](https://diataxis.fr/) — four distinct kinds of documentation, each serving a different reader need.

| Kind        | Doc                                                                                    | What it does for you                                                        |
| ----------- | -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Tutorial    | [getting-started.md](getting-started.md)                                               | Walks you through your first run end to end. Read once.                     |
| How-to      | [README quickstart](../README.md#first-run), [installation.md](installation.md)        | Recipes for specific outcomes. Read when you need them.                     |
| Reference   | [cli.md](cli.md), [configuration.md](configuration.md), [collectors.md](collectors.md) | Lookup material. Skim, then return when you need a specific fact.           |
| Explanation | [privacy.md](privacy.md), [design.md](design.md)                                       | Why codevigil is the way it is. Read when you want to understand decisions. |

## Conventions across the docs

- **All paths assume `$HOME` is your real home directory.** Substitutions like `~/.claude/projects` are expanded by codevigil at runtime via `Path.expanduser()`.
- **All examples assume codevigil is on your `PATH`.** If `codevigil --version` does not work after install, see [installation.md#troubleshooting](installation.md#troubleshooting).
- **Code blocks marked `bash` are shell commands.** Everything else (`text`, `toml`, `json`, `markdown`, `python`) is content rather than command.
- **Every cross-reference inside the docs uses relative paths.** The docs render correctly on GitHub, in any markdown viewer, and in `less` with rendering.

## Contributing to the docs

The docs live in this directory and are written in plain markdown with no manual line wrapping — paragraphs are single soft-wrapped lines so the renderer handles width. Update the relevant doc when you change behaviour, and run the same gate the rest of the repo runs:

```bash
uv run pytest
uv run ruff check .
bash scripts/ci_privacy_grep.sh
```

Doc changes do not need new tests, but they do need to stay accurate against the current code. If a doc and the code disagree, the code is right and the doc is wrong — update the doc.
