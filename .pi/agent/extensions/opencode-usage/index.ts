/**
 * opencode-usage — Track OpenCode Go subscription usage from within Pi.
 *
 * Features:
 *   /usage command — show locally-accumulated usage + live API data (when available)
 *   Automatic tracking — hooks assistant message_end to accumulate tokens + cost
 *   API-ready — hits the proposed endpoints; gracefully reports "not yet live"
 *
 * Go limits: 5hr=$12 | weekly=$30 | monthly=$60
 * API tracking: https://github.com/anomalyco/opencode/issues/16017 (Go) and #10448 (Zen)
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as fs from "node:fs";
import * as path from "node:path";

// ── State file (persisted across sessions) ──────────────────────────────
const STATE_FILE = path.join(
  process.env.HOME || "~",
  ".pi/agent/extensions/opencode-usage/state.json",
);

interface UsageState {
  totalInputTokens: number;
  totalOutputTokens: number;
  totalCacheReadTokens: number;
  totalCost: number;
  sessionCount: number;
  firstTrackedAt: string;
  lastTrackedAt: string;
}

function loadState(): UsageState {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, "utf-8"));
  } catch {
    return {
      totalInputTokens: 0,
      totalOutputTokens: 0,
      totalCacheReadTokens: 0,
      totalCost: 0,
      sessionCount: 0,
      firstTrackedAt: new Date().toISOString(),
      lastTrackedAt: new Date().toISOString(),
    };
  }
}

function saveState(s: UsageState): void {
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2));
}

// ── Helpers ─────────────────────────────────────────────────────────────
function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

function fmtDuration(seconds: number): string {
  if (seconds <= 0) return "now";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtDollars(n: number): string {
  return `$${n.toFixed(4)}`;
}

// ── API key resolution ──────────────────────────────────────────────────
let cachedApiKey: string | undefined;

function getApiKey(): string | undefined {
  if (cachedApiKey !== undefined) return cachedApiKey;

  // Check env var
  if (process.env.OPENCODE_API_KEY) {
    cachedApiKey = process.env.OPENCODE_API_KEY;
    return cachedApiKey;
  }

  // Try reading from pi auth.json
  try {
    const authPath = path.join(
      process.env.HOME || "~",
      ".pi/agent/auth.json",
    );
    const auth = JSON.parse(fs.readFileSync(authPath, "utf-8"));
    if (auth["opencode-go"]?.key) {
      cachedApiKey = auth["opencode-go"].key;
      return cachedApiKey;
    }
  } catch {
    // no auth file
  }

  cachedApiKey = "";
  return undefined;
}

// ── Extension ───────────────────────────────────────────────────────────
export default function (pi: ExtensionAPI) {
  let state = loadState();

  // Reload state on each session start (in case another session wrote it)
  pi.on("session_start", () => {
    state = loadState();
  });

  // ── Track usage on every assistant message ──────────────────────────
  pi.on("message_end", async (event, _ctx) => {
    if (event.message.role !== "assistant") return;

    const usage = event.message.usage;
    if (!usage) return;

    state.totalInputTokens += usage.input || 0;
    state.totalOutputTokens += usage.output || 0;
    state.totalCacheReadTokens += usage.cacheRead || 0;
    state.totalCost += usage.cost?.total || 0;
    state.lastTrackedAt = new Date().toISOString();

    saveState(state);
  });

  // Count sessions
  pi.on("session_start", () => {
    state.sessionCount++;
    saveState(state);
  });

  // ── /usage command ──────────────────────────────────────────────────
  pi.registerCommand("usage", {
    description: "Show OpenCode Go subscription usage",
    handler: async (_args, ctx) => {
      const s = loadState();

      const lines = [
        "📊  OpenCode Go Usage",
        "═══════════════════════",
        "",
        "── Local tracking ──",
        `  Sessions:       ${s.sessionCount}`,
        `  Input tokens:   ${fmtTokens(s.totalInputTokens)}`,
        `  Output tokens:  ${fmtTokens(s.totalOutputTokens)}`,
        `  Cache read:     ${fmtTokens(s.totalCacheReadTokens)}`,
        `  Est. cost:      ${fmtDollars(s.totalCost)}`,
        `  First tracked:  ${s.firstTrackedAt.slice(0, 10)}`,
        `  Last activity:  ${s.lastTrackedAt.slice(0, 10)}`,
        "",
      ];

      // ── Live API queries ──────────────────────────────────────────
      const apiKey = getApiKey();

      if (apiKey) {
        // Go usage endpoint
        try {
          const res = await fetch(
            "https://opencode.ai/api/v1/usage/plan",
            {
              headers: { Authorization: `Bearer ${apiKey}` },
              signal: AbortSignal.timeout(5000),
            },
          );

          if (res.ok) {
            const data = (await res.json()) as {
              plan: string;
              windows: Record<
                string,
                { usage_percent: number; resets_in_seconds: number }
              >;
            };
            lines.push("── Live Go plan (API) ──");
            if (data.windows) {
              for (const [window, info] of Object.entries(data.windows)) {
                lines.push(
                  `  ${window}: ${info.usage_percent}%  (resets in ${fmtDuration(info.resets_in_seconds)})`,
                );
              }
            }
          } else {
            lines.push(
              "── Live Go API: not yet available (404) ──",
            );
            lines.push(
              "  Track: https://github.com/anomalyco/opencode/issues/16017",
            );
          }
        } catch {
          lines.push("── Live Go API: not reachable ──");
        }

        // Zen balance endpoint
        try {
          const res = await fetch(
            "https://opencode.ai/zen/v1/balance",
            {
              headers: { Authorization: `Bearer ${apiKey}` },
              signal: AbortSignal.timeout(5000),
            },
          );

          if (res.ok) {
            const data = (await res.json()) as {
              balance: number;
              currency: string;
              auto_reload?: {
                enabled: boolean;
                threshold: number;
                amount: number;
              };
            };
            lines.push("");
            lines.push("── Zen balance ──");
            lines.push(`  Balance:    ${fmtDollars(data.balance)} ${data.currency}`);
            if (data.auto_reload?.enabled) {
              lines.push(
                `  Auto-reload: on (threshold ${fmtDollars(data.auto_reload.threshold)}, amount ${fmtDollars(data.auto_reload.amount)})`,
              );
            }
          } else {
            lines.push("");
            lines.push(
              "── Zen balance API: not yet available (404) ──",
            );
            lines.push(
              "  Track: https://github.com/anomalyco/opencode/issues/10448",
            );
          }
        } catch {
          // Zen endpoint not reachable
        }
      } else {
        lines.push(
          "  Note: set OPENCODE_API_KEY env var for live API queries",
        );
      }

      lines.push("");
      lines.push("Go limits: 5hr = $12  |  weekly = $30  |  monthly = $60");
      lines.push("Check dashboard: https://opencode.ai/console");

      // Display in TUI
      if (ctx.hasUI) {
        ctx.ui.setWidget("opencode-usage", lines);
        ctx.ui.notify(
          `Usage: ${fmtDollars(s.totalCost)} tracked | ${fmtTokens(s.totalInputTokens)} in / ${fmtTokens(s.totalOutputTokens)} out`,
          "info",
        );
      } else {
        // Print mode: write to stdout
        for (const line of lines) {
          console.log(line);
        }
      }
    },
  });

  // ── Tool for LLM to call ────────────────────────────────────────────
  pi.registerTool({
    name: "check_opencode_usage",
    label: "Check OpenCode Usage",
    description:
      "Check current OpenCode Go subscription usage statistics (locally tracked tokens/cost). Use when the user asks about their OpenCode usage or wants to know if they're approaching limits.",
    promptSnippet:
      "check_opencode_usage: show locally-tracked OpenCode Go token counts and estimated cost",
    parameters: Type.Object({}),
    async execute() {
      const s = loadState();
      const apiKey = getApiKey();

      let apiInfo = "";
      if (apiKey) {
        try {
          const res = await fetch(
            "https://opencode.ai/api/v1/usage/plan",
            {
              headers: { Authorization: `Bearer ${apiKey}` },
              signal: AbortSignal.timeout(5000),
            },
          );
          if (res.ok) {
            const data = (await res.json()) as {
              windows: Record<
                string,
                { usage_percent: number; resets_in_seconds: number }
              >;
            };
            const parts: string[] = [];
            for (const [window, info] of Object.entries(data.windows)) {
              parts.push(
                `${window}: ${info.usage_percent}% (resets in ${fmtDuration(info.resets_in_seconds)})`,
              );
            }
            apiInfo = "Live API: " + parts.join(" | ");
          }
        } catch {
          // API not available
        }
      }

      const text = [
        `Locally tracked: ${fmtTokens(s.totalInputTokens)} input / ${fmtTokens(s.totalOutputTokens)} output tokens`,
        `Estimated cost: ${fmtDollars(s.totalCost)}`,
        `Go limits: 5hr=$12, weekly=$30, monthly=$60`,
        apiInfo || "Live API endpoint not yet available (issue #16017)",
        `Dashboard: https://opencode.ai/console`,
      ]
        .filter(Boolean)
        .join("\n");

      return {
        content: [{ type: "text", text }],
        details: { state: s },
      };
    },
  });
}
