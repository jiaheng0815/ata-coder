/**
 * Safety Guard — replaces safety_guard.py + fool_proof.py.
 *
 * Pattern-based risk analysis for tool execution.
 * Blocks CRITICAL operations, warns on DANGER ones.
 */

// ── Risk Levels ─────────────────────────────────────────────────────────────

// Use plain `enum` (not `const enum`) — Node.js 24 native TS does NOT compile
// const enums away; they must exist as runtime objects for value access.
export enum RiskLevel {
  SAFE = "SAFE",
  CAUTION = "CAUTION",
  DANGER = "DANGER",
  CRITICAL = "CRITICAL",
}

export interface SafetyCheck {
  allowed: boolean;
  risk: RiskLevel;
  reason: string;
  warnings: string[];
}

// ── Protected paths (never writable) ────────────────────────────────────────

const PROTECTED_PATHS = [
  "/etc/passwd", "/etc/shadow", "/etc/sudoers", "/etc/hosts",
  "/boot", "/sys", "/proc", "/dev",
  "C:\\Windows", "C:\\Windows\\System32",
  "~/.ssh", "~/.gnupg",
  ".git/config", ".git/hooks",
];

// ── Destructive shell patterns ──────────────────────────────────────────────

const DESTRUCTIVE_PATTERNS: Array<{ pattern: RegExp; risk: RiskLevel; label: string }> = [
  // CRITICAL — irreversible destruction
  { pattern: /\brm\s+-rf\s+\/\b/, risk: RiskLevel.CRITICAL, label: "rm -rf /" },
  { pattern: /\brm\s+-rf\s+\/\*/, risk: RiskLevel.CRITICAL, label: "rm -rf /*" },
  { pattern: /\bdd\s+if=.*of=\/dev\//, risk: RiskLevel.CRITICAL, label: "dd to /dev" },
  { pattern: /\bmkfs\./, risk: RiskLevel.CRITICAL, label: "mkfs format" },
  { pattern: /:\s*\(\)\s*\{.*\}\s*;/, risk: RiskLevel.CRITICAL, label: "fork bomb" },
  // DANGER — destructive but recoverable
  { pattern: /\brm\s+-rf\s+(?!\/)/, risk: RiskLevel.DANGER, label: "rm -rf (non-root)" },
  { pattern: /\bdel\s+\/f\s+\/s\s+\/q\s+[A-Z]:\\/, risk: RiskLevel.DANGER, label: "del /f/s/q drive root" },
  { pattern: /\bchmod\s+-R\s+777/, risk: RiskLevel.DANGER, label: "chmod -R 777" },
  { pattern: /\bgci\s+-rec/, risk: RiskLevel.CAUTION, label: "recursive PowerShell delete" },
  // CAUTION — potentially dangerous
  { pattern: /\bgit\s+push\s+--force/, risk: RiskLevel.CAUTION, label: "git push --force" },
  { pattern: /\bdocker\s+rm\s+-f/, risk: RiskLevel.CAUTION, label: "docker rm -f" },
  { pattern: /\bkubectl\s+delete/, risk: RiskLevel.CAUTION, label: "kubectl delete" },
  { pattern: /\bpip\s+uninstall/, risk: RiskLevel.CAUTION, label: "pip uninstall" },
  { pattern: /\bnpm\s+uninstall/, risk: RiskLevel.CAUTION, label: "npm uninstall" },
];

// ── SQL injection patterns ──────────────────────────────────────────────────

const SQL_DESTRUCTIVE: Array<{ pattern: RegExp; label: string }> = [
  { pattern: /\bDROP\s+(TABLE|DATABASE)\s+\w+/i, label: "DROP TABLE/DATABASE" },
  { pattern: /\bDELETE\s+FROM\s+[\w.]+\s*;?\s*$/im, label: "DELETE FROM (no WHERE)" },
  { pattern: /\bTRUNCATE\s+(TABLE\s+)?\w+/i, label: "TRUNCATE TABLE" },
  { pattern: /\bALTER\s+TABLE\s+\w+\s+DROP\b/i, label: "ALTER TABLE DROP" },
];

// ── File path traversal detection ───────────────────────────────────────────

function hasPathTraversal(filePath: string): boolean {
  const normalized = filePath.replace(/\\/g, "/");
  return normalized.includes("../") || normalized.includes("..\\");
}

function isProtected(filePath: string): boolean {
  const resolved = filePath.replace(/\\/g, "/").toLowerCase();
  const home = (process.env.HOME ?? process.env.USERPROFILE ?? "").toLowerCase();
  return PROTECTED_PATHS.some((p) => {
    const pp = p.replace("~", home).replace(/\\/g, "/").toLowerCase();
    return resolved === pp || resolved.startsWith(pp + "/");
  });
}

// ── Main Safety Check ───────────────────────────────────────────────────────

export function check(toolName: string, args: Record<string, unknown>): SafetyCheck {
  const warnings: string[] = [];

  switch (toolName) {
    case "run_shell": {
      const cmd = String(args.command ?? args.cmd ?? "").trim();
      if (!cmd) {
        return { allowed: true, risk: RiskLevel.SAFE, reason: "", warnings: [] };
      }

      const cmdLower = cmd.toLowerCase();

      // Check destructive patterns
      for (const { pattern, risk, label } of DESTRUCTIVE_PATTERNS) {
        if (pattern.test(cmdLower)) {
          if (risk === RiskLevel.CRITICAL) {
            return {
              allowed: false,
              risk: RiskLevel.CRITICAL,
              reason: `Blocked: ${label} — irreversible operation detected in: ${cmd.slice(0, 80)}`,
              warnings,
            };
          }
          warnings.push(`${risk}: ${label}`);
        }
      }

      // SQL injection in shell
      for (const { pattern, label } of SQL_DESTRUCTIVE) {
        if (pattern.test(cmd)) {
          return {
            allowed: false,
            risk: RiskLevel.CRITICAL,
            reason: `Blocked: ${label} detected in command`,
            warnings,
          };
        }
      }

      // PowerShell web download + execute chain
      if (/\bIEX\b/i.test(cmd) && /\bInvoke-WebRequest\b/i.test(cmd)) {
        return {
          allowed: false,
          risk: RiskLevel.CRITICAL,
          reason: "Blocked: IEX + Invoke-WebRequest download-execute chain",
          warnings,
        };
      }

      const risk = warnings.length > 0 ? RiskLevel.DANGER : RiskLevel.SAFE;
      return { allowed: true, risk, reason: "", warnings };
    }

    case "write_file":
    case "edit_file": {
      const filePath = String(args.file_path ?? "");
      if (hasPathTraversal(filePath)) {
        return {
          allowed: false,
          risk: RiskLevel.CRITICAL,
          reason: `Blocked: path traversal detected → ${filePath}`,
          warnings,
        };
      }
      if (isProtected(filePath)) {
        return {
          allowed: false,
          risk: RiskLevel.CRITICAL,
          reason: `Blocked: protected path → ${filePath}`,
          warnings,
        };
      }
      return { allowed: true, risk: RiskLevel.SAFE, reason: "", warnings: [] };
    }

    default:
      return { allowed: true, risk: RiskLevel.SAFE, reason: "", warnings: [] };
  }
}
