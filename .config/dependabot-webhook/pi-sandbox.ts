/**
 * Pi sandbox extension for dependabot webhook agent.
 *
 * Replicates the security posture of the opencode.json sandbox:
 * - Default-deny all bash commands
 * - Allowlist: git, bundle, rake, rubocop, brakeman, bundler-audit, gh, rbenv, echo, cat
 * - Block writes to protected paths (config/master.key, config/credentials, .env)
 *
 * Loaded via: pi -p -e ~/.config/dependabot-webhook/pi-sandbox.ts ...
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { isToolCallEventType } from "@earendil-works/pi-coding-agent";

// ── Glob → regex ────────────────────────────────────────────────────────────

function globToRegex(pattern: string): RegExp {
  // Escape all regex special chars, then convert glob * to .*
  const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp("^" + escaped.replace(/\\\*/g, ".*") + "$");
}

// ── Bash allowlist ───────────────────────────────────────────────────────────

const ALLOWED_PATTERNS: string[] = [
  // Basic utilities
  "echo *",
  "cat *",
  "cd *",

  // Ruby environment
  "rbenv versions",
  "ruby -v",
  "bundle -v",
  "bundle install",
  "bundle update *",
  "bundle exec *",

  // Git — all safe operations, push only to main or version tags
  "git clone *",
  "git checkout *",
  "git pull*",
  "git fetch *",
  "git add *",
  "git commit *",
  "git push origin main",
  "git push origin v*",
  "git tag *",
  "git log *",
  "git diff *",
  "git show *",
  "git status",
  "git config *",
  "git checkout -- *",

  // Rails / Ruby tooling
  "bin/rake",
  "bin/brakeman*",
  "bin/bundler-audit*",
  "bin/importmap audit",
  "bin/rubocop*",
  "bin/rails db:test:prepare test",
  "bin/rails db:test:prepare test:system",

  // RBENV_VERSION-prefixed variants
  "RBENV_VERSION=* bundle install",
  "RBENV_VERSION=* bundle update *",
  "RBENV_VERSION=* bundle exec *",
  "RBENV_VERSION=* bin/rake",
  "RBENV_VERSION=* bin/brakeman*",
  "RBENV_VERSION=* bin/bundler-audit*",
  "RBENV_VERSION=* bin/importmap audit",
  "RBENV_VERSION=* bin/rubocop*",
  "RBENV_VERSION=* bin/rails db:test:prepare test",
  "RBENV_VERSION=* bin/rails db:test:prepare test:system",

  // GitHub CLI — readonly + PR management
  "gh pr list *",
  "gh pr diff *",
  "gh pr close *",
  "gh pr view *",
  "gh pr checks *",
  "gh run list *",
  "gh run view *",
  "gh run watch *",
  "gh api *",
];

// Explicit deny patterns (caught by default-deny, but listed for audit clarity)
const EXPLICIT_DENY_PATTERNS: string[] = [
  "sudo *",
  "docker *",
  "systemctl *",
  "curl *",
  "wget *",
  "rm *",
  "*release.sh*",
  "*up.sh*",
];

// ── Protected write paths ────────────────────────────────────────────────────

const PROTECTED_PATHS = [
  "config/master.key",
  "config/credentials",
  ".env",
];

// ── Compiled regexes ─────────────────────────────────────────────────────────

const allowedRegexes = ALLOWED_PATTERNS.map(globToRegex);

function isAllowed(command: string): boolean {
  const trimmed = command.trim();
  for (const re of allowedRegexes) {
    if (re.test(trimmed)) return true;
  }
  return false;
}

function isProtectedPath(filePath: string): boolean {
  const normalized = filePath.replace(/^\.\//, "").replace(/^\/.*?\//, "");
  return PROTECTED_PATHS.some((p) => normalized === p || normalized.endsWith("/" + p));
}

// v2 — fixed globToRegex (* was not being escaped)

export default function (pi: ExtensionAPI) {
  // Block unapproved bash commands
  pi.on("tool_call", async (event, ctx) => {
    // Standard tool: bash
    if (isToolCallEventType("bash", event)) {
      const cmd = event.input.command?.trim() ?? "";
      if (!isAllowed(cmd)) {
        return {
          block: true,
          reason: `Blocked by dependabot sandbox: command not in allowlist. Allowed: git, bundle, bin/rake, bin/rubocop, bin/brakeman, bin/bundler-audit, gh (read-only + PR close). For safety, sudo, docker, systemctl, curl, wget, rm, release.sh, and up.sh are explicitly denied.`,
        };
      }
    }

    // Standard tool: write — block protected paths
    if (isToolCallEventType("write", event)) {
      if (isProtectedPath(event.input.path)) {
        return {
          block: true,
          reason: `Blocked by dependabot sandbox: writing to ${event.input.path} is not allowed. Protected files: config/master.key, config/credentials, .env.`,
        };
      }
    }

    // Standard tool: edit — block protected paths
    if (isToolCallEventType("edit", event)) {
      if (isProtectedPath(event.input.path)) {
        return {
          block: true,
          reason: `Blocked by dependabot sandbox: editing ${event.input.path} is not allowed. Protected files: config/master.key, config/credentials, .env.`,
        };
      }
    }
  });
}
