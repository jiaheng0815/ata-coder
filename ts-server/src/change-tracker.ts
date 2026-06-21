/**
 * Change Tracker — replaces change_tracker.py.
 *
 * Tracks file write/edit operations for undo/redo.
 * Dry-run safe: never records phantom changes.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync, copyFileSync, unlinkSync } from "node:fs";
import { join, dirname } from "node:path";
import { ATA_HOME } from "./config.ts";

// ── Types ───────────────────────────────────────────────────────────────────

export const enum ChangeType {
  WRITE = "WRITE",
  EDIT = "EDIT",
}

export interface FileChange {
  id: number;
  filePath: string;
  changeType: ChangeType;
  oldContent: string | null;
  newContent: string;
  timestamp: string;
  /** Whether this was recorded in dry-run mode (must skip revert) */
  dryRun: boolean;
}

// ── Change Tracker ──────────────────────────────────────────────────────────

export class ChangeTracker implements Disposable {
  readonly #changes: FileChange[] = [];
  readonly #backupDir: string;
  readonly #backups = new Map<string, string>(); // filePath → backupPath
  #nextId = 1;
  #lastActive = -1;
  #dryRun: boolean;

  constructor(dryRun = false) {
    this.#dryRun = dryRun;
    this.#backupDir = join(ATA_HOME, "changes");
    if (!existsSync(this.#backupDir)) {
      mkdirSync(this.#backupDir, { recursive: true });
    }
  }

  [Symbol.dispose](): void {
    this.clear();
  }

  get dryRun(): boolean { return this.#dryRun; }
  set dryRun(val: boolean) { this.#dryRun = val; }

  // ── Capture ──────────────────────────────────────────────────────────────

  captureWrite(filePath: string, content: string): FileChange | null {
    const existsBefore = existsSync(filePath);
    const oldContent = existsBefore
      ? (() => { try { return readFileSync(filePath, "utf-8"); } catch { return null; } })()
      : null;

    if (existsBefore) this.#backup(filePath);

    const change: FileChange = {
      id: this.#nextId,
      filePath,
      changeType: existsBefore ? ChangeType.EDIT : ChangeType.WRITE,
      oldContent,
      newContent: content,
      timestamp: new Date().toISOString(),
      dryRun: this.#dryRun,
    };

    if (this.#dryRun) this.#writeDryBackup(filePath, content);

    this.#changes.push(change);
    this.#nextId++;
    this.#lastActive = -1;
    return change;
  }

  captureEdit(filePath: string, oldContent: string, newContent: string): FileChange | null {
    if (oldContent === newContent) return null;

    this.#backup(filePath);

    const change: FileChange = {
      id: this.#nextId,
      filePath,
      changeType: ChangeType.EDIT,
      oldContent,
      newContent,
      timestamp: new Date().toISOString(),
      dryRun: this.#dryRun,
    };

    if (this.#dryRun) this.#writeDryBackup(filePath, newContent);

    this.#changes.push(change);
    this.#nextId++;
    this.#lastActive = -1;
    return change;
  }

  // ── Undo / Redo ──────────────────────────────────────────────────────────

  undo(count = 1): FileChange[] {
    if (this.#dryRun) return [];
    const undone: FileChange[] = [];

    for (let i = 0; i < count; i++) {
      if (this.#lastActive < 0) this.#lastActive = this.#changes.length - 1;
      if (this.#lastActive < 0) break;

      const change = this.#changes[this.#lastActive];
      if (!change || change.dryRun) {
        this.#lastActive--;
        continue;
      }

      this.#applyRevert(change);
      undone.push(change);
      this.#lastActive--;
    }

    return undone;
  }

  redo(count = 1): FileChange[] {
    if (this.#dryRun) return [];
    const redone: FileChange[] = [];

    for (let i = 0; i < count; i++) {
      const idx = this.#lastActive + 1;
      if (idx >= this.#changes.length) break;

      const change = this.#changes[idx];
      if (!change || change.dryRun) {
        this.#lastActive = idx;
        continue;
      }

      this.#applyChange(change);
      redone.push(change);
      this.#lastActive = idx;
    }

    return redone;
  }

  // ── Internals ────────────────────────────────────────────────────────────

  #backup(filePath: string): void {
    if (!existsSync(filePath)) return;
    try {
      const backupName = `${filePath.replace(/[/\\:]/g, "_")}.${Date.now()}.bak`;
      const backupPath = join(this.#backupDir, backupName);
      copyFileSync(filePath, backupPath);
      this.#backups.set(filePath, backupPath);
    } catch { /* best-effort */ }
  }

  #writeDryBackup(filePath: string, content: string): void {
    try {
      const dp = join(this.#backupDir, `dry_${this.#nextId}_${filePath.replace(/[/\\:]/g, "_")}`);
      const parent = dirname(dp);
      if (!existsSync(parent)) mkdirSync(parent, { recursive: true });
      writeFileSync(dp, content, "utf-8");
    } catch { /* best-effort */ }
  }

  #applyRevert(change: FileChange): void {
    try {
      if (change.changeType === ChangeType.WRITE && change.oldContent === null) {
        // File was created by this change → delete it
        if (existsSync(change.filePath)) unlinkSync(change.filePath);
      } else if (change.oldContent !== null) {
        const parent = dirname(change.filePath);
        if (!existsSync(parent)) mkdirSync(parent, { recursive: true });
        writeFileSync(change.filePath, change.oldContent, "utf-8");
      }
    } catch {
      // Revert failure is non-fatal
    }
  }

  #applyChange(change: FileChange): void {
    try {
      const parent = dirname(change.filePath);
      if (!existsSync(parent)) mkdirSync(parent, { recursive: true });
      writeFileSync(change.filePath, change.newContent, "utf-8");
    } catch {
      // Apply failure is non-fatal
    }
  }

  // ── Housekeeping ─────────────────────────────────────────────────────────

  get changes(): readonly FileChange[] {
    return this.#changes;
  }

  get canUndo(): boolean {
    if (this.#dryRun) return false;
    return this.#changes.some((c) => !c.dryRun);
  }

  clear(): void {
    this.#changes.length = 0;
    this.#backups.clear();
    this.#lastActive = -1;
    this.#nextId = 1;
  }
}
