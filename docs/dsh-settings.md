# Configuring the DSH bundle: the settings section and what wins

**There is no settings card for Tool Guardian in the DSH web UI.** That is how DSH works, not a missing screenshot. The Plugins -> *Plugin configuration* tab draws a card only for a namespace that ships its own browser bundle. The rest render nothing (DSH `packages/client/ui-settings-plugins`: "A served namespace no card claims renders nothing"). Tool Guardian registers the `tool-guardian` settings namespace but ships no browser half. You edit it in DSH's settings file instead, and DSH applies the change live.

- **File:** `<DSH_HOME>/settings.yaml`. `DSH_HOME` defaults to `~/.dsh` (Windows: `%USERPROFILE%\.dsh`).
- **Section:** the top-level key `tool-guardian:`.
- **Takes effect:** as soon as you save. DSH hot-reloads the file. Tool Guardian restarts its bridge only when a resolved value actually changed.
- **A mistake while DSH runs is safe:** an edit that does not parse is ignored with a warning, and the last good settings stay in force. A file that is already broken when DSH *starts* stops DSH from loading, so fix it before you restart. (`dsh-settings-file`, mounted by `@deepseek-ai/dsh-base`.)

## The section, as a card

```
+-- tool-guardian ------------------------------ ~/.dsh/settings.yaml ------+
|                                                                           |
|  WHICH SERVERS                                                            |
|  mcpServers    {}         inline mcpServers block; non-empty = used       |
|                           instead of any file   (no env override)         |
|  configPath    ''         an mcpServers JSON file  <- TOOL_GUARDIAN_CONFIG|
|                           '' = ./mcp.json, ./.mcp.json in the plugin      |
|                           folder, then ~/.tool-guardian/mcp.json          |
|  python        ''         '' = .venv beside the plugin, then python /     |
|                           python3 on PATH          <- TOOL_GUARDIAN_PYTHON|
|  eagerStart    true       start the servers when DSH loads the plugin     |
|  startTimeoutMs 90000     per server   <- TOOL_GUARDIAN_START_TIMEOUT (s)  |
|  callTimeoutMs 120000     per call     <- TOOL_GUARDIAN_CALL_TIMEOUT (s)   |
|                                                                           |
|  GROUPS                                                                   |
|  groups        {}         { name: ["server", "server.tool", "srv.pre*"] }  |
|                           {} = one group per server                       |
|  activeGroups  []         groups loaded as direct tools at start          |
|  allowRuntimeActivation   true   activate_group + restore_groups exist    |
|                                                                           |
|  RESULTS                                                                  |
|  ladder.enabled   true    shape every tool's result <- TOOL_GUARDIAN_LADDER|
|  ladder.allTools  true    all DSH tools, not only the router's            |
|  ladder.skipTools [read, read_image, skill, todo_write]                   |
|  ladder.options   {}      tg_ladder thresholds                            |
|  spillDir      ''         full originals  <- TOOL_GUARDIAN_SPILL_DIR       |
|  spillKeep     500        archived results kept                           |
|                                                                           |
|  BYPASS WATCH                                                             |
|  bypass.mode   nudge      off | log | nudge | deny                        |
|  bypass.shellTools [bash, pwsh, run_code]                                 |
|  bypass.rules  []         [{pattern, server, tool}]                       |
|  bypass.minNameLength 8                                                   |
|                                                                           |
|  DSH'S OWN TOOLS                                                          |
|  builtinGroups.enabled false  <- TOOL_GUARDIAN_BUILTIN_GROUPS=0 forces off|
|  builtinGroups.hidden  []     groups hidden until activate_group          |
|                                                                           |
|  OTHER                                                                    |
|  skillsDirs    []         SKILL.md roots        <- TOOL_GUARDIAN_SKILLS    |
|  logPath       ''         router log file        <- TOOL_GUARDIAN_LOG      |
+---------------------------------------------------------------------------+
   <- ENV_VAR : that variable, when set, beats every value in this card
```

The same thing as YAML. This is a working example: two servers, one custom group, and deny mode:

```yaml
# ~/.dsh/settings.yaml
tool-guardian:
  mcpServers:
    files:
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "C:/data"]
    git:
      command: uvx
      args: ["mcp-server-git"]
  groups:
    code: ["git", "files.read_*"]
  bypass:
    mode: deny
```

Most people never need this file. `npm run setup` writes `~/.tool-guardian/mcp.json`, and the bridge finds it with `configPath: ''`. Use the settings file when you want DSH's copy to differ from the one other MCP clients use.

## What wins: the precedence order, proven from the code

Each value is looked up in this order, and the first one that is set wins:

1. **the environment variable**, where one exists (the `<-` column above);
2. **`settings.yaml` -> `tool-guardian:`** (the user layer);
3. **the `tool-guardian` row's `config`** in a profile's `cordis.patch.yml` (the bundle's own row, or a later profile layer that overrides it by id);
4. **the schema default** (the card above).

For two things there are more layers, because the Python router has its own config file:

| Setting | Order, first wins | Code that makes it so |
|---|---|---|
| **Which MCP servers run** | (1) `mcpServers` from settings / patch row, **if non-empty** -> (2) `TOOL_GUARDIAN_CONFIG` -> (3) `configPath` -> (4) `./mcp.json`, `./.mcp.json` (in the plugin folder), `~/.tool-guardian/mcp.json` | `PythonBridge.startRouter()`: `inline ? { mcpServers } : { config: configPath }`. `resolveOptions()`: `configPath: env.TOOL_GUARDIAN_CONFIG?.trim() \|\| section.configPath`. `tool_guardian._config_search_order()` |
| **`groups`, `ladder.options`, `spillDir`** | (1) the DSH value, **if non-empty** -> (2) the `toolGuardian` block of the mcp.json that was found -> (3) built-in default | `tool_guardian.Router.__init__`: `self.options = load_options(config)`, then `update({k: v ... if v not in (None, "", {}, [])})` with what the bridge passed |
| `python`, `configPath`, `logPath`, `spillDir` | env -> settings -> patch row -> default | `resolveOptions()`: `env.X?.trim() \|\| section.x \|\| default` |
| timeouts | env (seconds) -> settings -> patch row -> default | `resolveOptions()` `seconds(...)` |
| `skillsDirs`, `logPath`, `spillDir`, timeouts (again, for the Python child) | an env var you exported is never overwritten | `PythonBridge.ensureStarted()`: `env.TOOL_GUARDIAN_* ??= ...` |
| `ladder.enabled`, `builtinGroups.enabled` | env set to `0`/`false`/`off`/`no` forces **off**; env cannot force on | `resolveOptions()` |
| everything else | settings -> patch row -> default (no env override) | `resolveOptions()` |

Three details that explain most "my change did nothing" reports:

- **`mcpServers` beats `TOOL_GUARDIAN_CONFIG`.** Once any `mcpServers` entry exists in settings or the patch row, the config file (and the env var that points at it) is not read for servers at all.
- **settings.yaml merges into the patch row; it does not replace it.** DSH merges plain objects key by key and replaces arrays and scalars (`settings/src/index.ts` `mergeLayers`). So you can *add* a server under `mcpServers` in settings.yaml, but a server that the patch row lists cannot be removed there. Remove it from the row. The patch row itself is different: a later profile layer replaces the whole `config` value, so restate every key you keep.
- **`spillKeep` in `mcp.json` has no effect under DSH.** The DSH default (500) is never empty, so it always wins.

Outside DSH (the plain MCP server) there is no settings layer. The order is `--config PATH` -> `$TOOL_GUARDIAN_CONFIG` -> `./mcp.json` -> `./.mcp.json` -> `~/.tool-guardian/mcp.json`, and the `toolGuardian` block of that file sets groups and ladder options.

## Checking what is actually in force

- `/toolguardian` in a DSH session: the servers that started, their tool counts, the token saving, the active groups and the update line.
- `dsh --profile <name> --dump-config`: the composed profile. The `tool-guardian` row shows the patch-row layer, not settings.yaml.
- `python tg_setup.py doctor` in the plugin folder: which config file the router finds, whether each server's command is on PATH, and unset `${VARS}`.
