# opencode-usage

Track your [OpenCode Go](https://opencode.ai/docs/go/) subscription usage directly from [Pi](https://github.com/earendil-works/pi-mono). Local token/cost accumulation + API-ready for when the live usage endpoint ships.

## Features

- **`/usage` command** — displays your locally-accumulated token counts and estimated cost, plus live Go plan windows and Zen balance when the API endpoints are available
- **Automatic tracking** — hooks every assistant response to log tokens (input, output, cache read) and cost
- **Persistent state** — usage accumulates across Pi sessions via `~/.pi/agent/extensions/opencode-usage/state.json`
- **LLM tool** — the model can call `check_opencode_usage` to look up current usage stats
- **API-ready** — hits the proposed `GET /api/v1/usage/plan` (Go) and `GET /zen/v1/balance` (Zen) endpoints; gracefully reports "not yet available" since they're not live yet

## Install

```bash
# From GitHub (once published):
pi install git:github.com/carter2099/pi-opencode-usage

# Local install:
pi install ~/.pi/agent/extensions/opencode-usage
```

Or just drop the files into `~/.pi/agent/extensions/opencode-usage/`.

## API Key

The extension reads your OpenCode API key from:
1. `OPENCODE_API_KEY` environment variable
2. `~/.pi/agent/auth.json` (the `opencode-go` provider key)

If neither is set, local tracking still works but live API queries are skipped.

## Usage

```
/usage          — show full usage report in a TUI widget
```

The agent can also call `check_opencode_usage` when you ask about your OpenCode usage.

## Go Plan Limits

| Window   | Limit |
|----------|-------|
| 5 hours  | $12   |
| Weekly   | $30   |
| Monthly  | $60   |

## API Status

The live usage endpoints are not yet shipped by OpenCode. Track progress:

- Go plan usage: [anomalyco/opencode#16017](https://github.com/anomalyco/opencode/issues/16017)
- Zen balance: [anomalyco/opencode#10448](https://github.com/anomalyco/opencode/issues/10448)

When those endpoints go live, `/usage` will pick them up automatically — no extension update needed.

## License

MIT
