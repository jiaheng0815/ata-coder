/**
 * Project auto-detection — replaces project.py.
 *
 * Detects project type, language, framework, and provides
 * workspace context for the agent.
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

// ── Types ───────────────────────────────────────────────────────────────────

export interface ProjectInfo {
  name: string;
  root: string;
  language: string;
  framework: string;
  buildTool: string;
  hasGit: boolean;
  files: string[];
}

// ── Detection ───────────────────────────────────────────────────────────────

const MARKERS: Array<{
  files: string[];
  language: string;
  framework: string;
  buildTool: string;
}> = [
  // Python
  { files: ["pyproject.toml", "setup.py", "setup.cfg"], language: "Python", framework: "", buildTool: "pip/setuptools" },
  { files: ["Pipfile"], language: "Python", framework: "", buildTool: "pipenv" },
  { files: ["poetry.lock"], language: "Python", framework: "", buildTool: "poetry" },
  { files: ["requirements.txt"], language: "Python", framework: "", buildTool: "pip" },
  // Node.js / TypeScript
  { files: ["package.json"], language: "TypeScript/JavaScript", framework: "", buildTool: "npm" },
  { files: ["next.config.js", "next.config.ts"], language: "TypeScript", framework: "Next.js", buildTool: "npm" },
  { files: ["nuxt.config.ts", "nuxt.config.js"], language: "TypeScript", framework: "Nuxt", buildTool: "npm" },
  { files: ["svelte.config.js"], language: "TypeScript", framework: "SvelteKit", buildTool: "npm" },
  { files: ["astro.config.mjs"], language: "TypeScript", framework: "Astro", buildTool: "npm" },
  // Rust
  { files: ["Cargo.toml"], language: "Rust", framework: "", buildTool: "cargo" },
  // Go
  { files: ["go.mod"], language: "Go", framework: "", buildTool: "go" },
  // Java / Kotlin
  { files: ["build.gradle", "build.gradle.kts"], language: "Java/Kotlin", framework: "", buildTool: "gradle" },
  { files: ["pom.xml"], language: "Java", framework: "", buildTool: "maven" },
  // C / C++
  { files: ["CMakeLists.txt"], language: "C/C++", framework: "", buildTool: "cmake" },
  { files: ["Makefile"], language: "C/C++", framework: "", buildTool: "make" },
  // C# / .NET
  { files: ["*.csproj", "*.sln"], language: "C#", framework: ".NET", buildTool: "dotnet" },
  // Ruby
  { files: ["Gemfile"], language: "Ruby", framework: "", buildTool: "bundler" },
  // PHP
  { files: ["composer.json"], language: "PHP", framework: "", buildTool: "composer" },
  // Swift
  { files: ["Package.swift"], language: "Swift", framework: "", buildTool: "swift" },
  // Dart / Flutter
  { files: ["pubspec.yaml"], language: "Dart", framework: "Flutter", buildTool: "pub" },
];

function findFile(dir: string, patterns: string[]): string | null {
  for (const pattern of patterns) {
    if (pattern.includes("*")) {
      // Glob pattern — check directory
      try {
        const entries = readdirSync(dir);
        const regex = new RegExp("^" + pattern.replace(/\*/g, ".*").replace(/\./g, "\\.") + "$");
        const match = entries.find((e) => regex.test(e));
        if (match) return join(dir, match);
      } catch { /* permission denied */ }
    } else {
      const full = join(dir, pattern);
      if (existsSync(full)) return full;
    }
  }
  return null;
}

// ── Main detection ──────────────────────────────────────────────────────────

export function detectProject(startDir?: string): ProjectInfo {
  const dir = resolve(startDir ?? process.cwd());
  const name = dir.split(/[/\\]/).pop() ?? "unknown";

  const info: ProjectInfo = {
    name,
    root: dir,
    language: "unknown",
    framework: "",
    buildTool: "",
    hasGit: existsSync(join(dir, ".git")),
    files: [],
  };

  // Walk up to find project root
  let current = dir;
  let found: typeof MARKERS[number] | null = null;

  for (let i = 0; i < 5; i++) {
    for (const marker of MARKERS) {
      const file = findFile(current, marker.files);
      if (file) {
        found = marker;
        info.root = current;
        info.files.push(file);
      }
    }
    if (found) break;

    // Also check for .git (project boundary)
    if (existsSync(join(current, ".git")) && i > 0) break;

    const parent = resolve(current, "..");
    if (parent === current) break;
    current = parent;
  }

  if (found) {
    info.language = found.language;
    info.framework = found.framework;
    info.buildTool = found.buildTool;
  }

  // Detect framework from package.json if present
  const pkgJson = join(info.root, "package.json");
  if (existsSync(pkgJson)) {
    try {
      const pkg = JSON.parse(readFileSync(pkgJson, "utf-8"));
      const deps = { ...pkg.dependencies, ...pkg.devDependencies };
      if (deps.react && !info.framework) info.framework = "React";
      if (deps.vue && !info.framework) info.framework = "Vue";
      if (deps.angular && !info.framework) info.framework = "Angular";
      if (deps.express && !info.framework) info.framework = "Express";
      if (deps.fastify && !info.framework) info.framework = "Fastify";
      if (deps.electron && !info.framework) info.framework = "Electron";
    } catch { /* ignore parse errors */ }
  }

  // Language-specific file extension heuristics
  if (info.language === "unknown") {
    try {
      const entries = readdirSync(dir).filter((e) => !e.startsWith("."));
      const exts = new Set(entries.map((e) => e.split(".").pop()?.toLowerCase()));
      if (exts.has("py")) { info.language = "Python"; info.buildTool = "pip"; }
      else if (exts.has("ts") || exts.has("tsx")) { info.language = "TypeScript"; info.buildTool = "npm"; }
      else if (exts.has("js") || exts.has("jsx")) { info.language = "JavaScript"; info.buildTool = "npm"; }
      else if (exts.has("rs")) { info.language = "Rust"; info.buildTool = "cargo"; }
      else if (exts.has("go")) { info.language = "Go"; info.buildTool = "go"; }
      else if (exts.has("java")) { info.language = "Java"; }
    } catch { /* ignore */ }
  }

  return info;
}
