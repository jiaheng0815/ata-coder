/**
 * Git Workflow — replaces git_workflow.py.
 *
 * Git status, diff, commit, and branch management.
 * Uses child_process.spawn (not exec) for safe argument handling.
 */

import { spawn } from "node:child_process";

// ── Types ───────────────────────────────────────────────────────────────────

export interface GitStatus {
  branch: string;
  staged: number;
  modified: number;
  untracked: number;
  ahead: number;
  behind: number;
  clean: boolean;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function runGit(args: string[], cwd: string, timeoutMs = 30_000): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const child = spawn("git", args, { cwd, stdio: ["ignore", "pipe", "pipe"] });
    const stdoutChunks: Buffer[] = [];
    const stderrChunks: Buffer[] = [];

    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      resolve({ code: -1, stdout: "", stderr: "timeout" });
    }, timeoutMs);

    child.stdout?.on("data", (chunk: Buffer) => stdoutChunks.push(chunk));
    child.stderr?.on("data", (chunk: Buffer) => stderrChunks.push(chunk));

    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({
        code: code ?? -1,
        stdout: Buffer.concat(stdoutChunks).toString("utf-8").trim(),
        stderr: Buffer.concat(stderrChunks).toString("utf-8").trim(),
      });
    });

    child.on("error", () => {
      clearTimeout(timer);
      resolve({ code: -1, stdout: "", stderr: "git not found" });
    });
  });
}

// ── Git Workflow ────────────────────────────────────────────────────────────

export class GitWorkflow {
  readonly #cwd: string;

  constructor(cwd: string) { this.#cwd = cwd; }

  // ── Status ────────────────────────────────────────────────────────────────

  async getStatus(): Promise<GitStatus> {
    const status: GitStatus = {
      branch: "(detached)",
      staged: 0,
      modified: 0,
      untracked: 0,
      ahead: 0,
      behind: 0,
      clean: true,
    };

    // Branch
    const { stdout: branch } = await runGit(["branch", "--show-current"], this.#cwd);
    if (branch) status.branch = branch.split("\n")[0];

    // Short status — CRITICAL: do NOT strip lines.
    // git status --short uses positional encoding:
    //   col 0 = index (staged), col 1 = working tree (unstaged)
    //   "?? path" → untracked
    //   "M  path" → staged, " M path" → unstaged
    const { stdout: short } = await runGit(["status", "--short"], this.#cwd);
    if (short) {
      for (const line of short.split("\n")) {
        const l = line.replace(/\r$/, ""); // strip CR only
        if (!l.trim()) continue;

        if (l.length >= 2 && l[0] === "?" && l[1] === "?") {
          status.untracked++;
        } else {
          if (l.length > 0 && "MADRC".includes(l[0])) status.staged++;
          if (l.length > 1 && "MD".includes(l[1])) status.modified++;
        }
      }
      status.clean = (status.staged === 0 && status.modified === 0 && status.untracked === 0);
    }

    // Ahead/behind
    const hasUpstream = await runGit(["rev-parse", "--abbrev-ref", "@{u}"], this.#cwd, 10_000);
    if (hasUpstream.code === 0) {
      const { stdout: ahead } = await runGit(["rev-list", "--count", "@{u}..HEAD"], this.#cwd, 10_000);
      status.ahead = parseInt(ahead || "0", 10) || 0;
      const { stdout: behind } = await runGit(["rev-list", "--count", "HEAD..@{u}"], this.#cwd, 10_000);
      status.behind = parseInt(behind || "0", 10) || 0;
    }

    return status;
  }

  // ── Diff ──────────────────────────────────────────────────────────────────

  async diff(staged = false): Promise<string> {
    const args = ["diff"];
    if (staged) args.push("--staged");
    const { stdout } = await runGit(args, this.#cwd);
    return stdout;
  }

  // ── Commit ────────────────────────────────────────────────────────────────

  async commit(message: string): Promise<boolean> {
    const { code } = await runGit(["commit", "-m", message], this.#cwd);
    return code === 0;
  }

  async commitAll(message: string): Promise<boolean> {
    const { code } = await runGit(["commit", "-a", "-m", message], this.#cwd);
    return code === 0;
  }

  // ── Branch ────────────────────────────────────────────────────────────────

  async createBranch(name: string): Promise<boolean> {
    const { code } = await runGit(["checkout", "-b", name], this.#cwd);
    return code === 0;
  }

  async switchBranch(name: string): Promise<boolean> {
    const { code } = await runGit(["checkout", name], this.#cwd);
    return code === 0;
  }

  // ── Log ───────────────────────────────────────────────────────────────────

  async log(count = 10): Promise<string> {
    const { stdout } = await runGit(
      ["log", `-${count}`, "--oneline", "--decorate"],
      this.#cwd,
    );
    return stdout;
  }

  // ── Git repo check ────────────────────────────────────────────────────────

  static async isGitRepo(cwd: string): Promise<boolean> {
    const { code } = await runGit(["rev-parse", "--git-dir"], cwd, 5_000);
    return code === 0;
  }
}
