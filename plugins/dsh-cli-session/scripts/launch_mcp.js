#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn, spawnSync } = require("child_process");

const SCRIPT_DIR = __dirname;
const SERVER = path.join(SCRIPT_DIR, "dsh_mcp_server.py");
const REQUIREMENTS = path.join(SCRIPT_DIR, "..", "requirements.txt");
const MIN_PYTHON = [3, 11];

function fail(message, details = "") {
  process.stderr.write(`LeanCodex launcher: ${message}\n`);
  if (details) process.stderr.write(`${String(details).trim()}\n`);
  process.exit(1);
}

function pythonCandidates() {
  const explicit = process.env.LEANCODEX_PYTHON;
  if (explicit) return [{ command: explicit, prefix: [] }];

  if (process.platform === "win32") {
    return [
      { command: "py", prefix: ["-3"] },
      { command: "python3", prefix: [] },
      { command: "python", prefix: [] },
    ];
  }
  return [
    { command: "python3", prefix: [] },
    { command: "python", prefix: [] },
  ];
}

function probePython(candidate) {
  const code = [
    "import json, platform, sys",
    "print(json.dumps({",
    '  "major": sys.version_info.major,',
    '  "minor": sys.version_info.minor,',
    '  "implementation": sys.implementation.name,',
    '  "machine": platform.machine(),',
    "}))",
  ].join("\n");
  const result = spawnSync(
    candidate.command,
    [...candidate.prefix, "-c", code],
    { encoding: "utf8", windowsHide: true }
  );
  if (result.status !== 0) return null;
  try {
    const info = JSON.parse(result.stdout.trim());
    if (
      info.major < MIN_PYTHON[0] ||
      (info.major === MIN_PYTHON[0] && info.minor < MIN_PYTHON[1])
    ) {
      return null;
    }
    return { ...candidate, info };
  } catch {
    return null;
  }
}

function findPython() {
  for (const candidate of pythonCandidates()) {
    const found = probePython(candidate);
    if (found) return found;
  }
  const hint = process.env.LEANCODEX_PYTHON
    ? `LEANCODEX_PYTHON=${process.env.LEANCODEX_PYTHON} is not a usable Python 3.11+ executable.`
    : "Install Python 3.11+ with pip, or set LEANCODEX_PYTHON to its executable path.";
  fail("Python 3.11+ was not found.", hint);
}

function requiredMcpVersion() {
  const text = fs.readFileSync(REQUIREMENTS, "utf8");
  const match = text.match(/^\s*mcp==([^\s#]+)\s*$/m);
  if (!match) {
    fail("requirements.txt must contain an exact mcp==VERSION pin.");
  }
  return match[1];
}

function installedMcpVersion(python, siteDir = "") {
  const code = [
    "import importlib.metadata, site, sys",
    "target = sys.argv[1]",
    "if target: site.addsitedir(target)",
    "import mcp",
    'print(importlib.metadata.version("mcp"))',
  ].join("\n");
  const result = spawnSync(
    python.command,
    [...python.prefix, "-c", code, siteDir],
    { encoding: "utf8", windowsHide: true }
  );
  return result.status === 0 ? result.stdout.trim() : null;
}

function runtimeRoot() {
  if (process.env.LEANCODEX_RUNTIME_DIR) {
    return path.resolve(process.env.LEANCODEX_RUNTIME_DIR);
  }
  if (process.env.PLUGIN_DATA) {
    return path.join(process.env.PLUGIN_DATA, "runtime");
  }
  if (process.platform === "win32") {
    const local = process.env.LOCALAPPDATA ||
      path.join(os.homedir(), "AppData", "Local");
    return path.join(local, "LeanCodex", "runtime");
  }
  if (process.platform === "darwin") {
    return path.join(os.homedir(), "Library", "Caches", "LeanCodex", "runtime");
  }
  const cache = process.env.XDG_CACHE_HOME ||
    path.join(os.homedir(), ".cache");
  return path.join(cache, "leancodex", "runtime");
}

function cachedRuntimeDir(python) {
  const requirements = fs.readFileSync(REQUIREMENTS);
  const digest = crypto
    .createHash("sha256")
    .update(requirements)
    .digest("hex")
    .slice(0, 12);
  const machine = String(python.info.machine || "unknown")
    .replace(/[^A-Za-z0-9_.-]/g, "_");
  const key = [
    `py${python.info.major}.${python.info.minor}`,
    python.info.implementation,
    process.platform,
    machine,
    digest,
  ].join("-");
  return path.join(runtimeRoot(), key);
}

function ensureRuntime(python) {
  const required = requiredMcpVersion();
  if (installedMcpVersion(python) === required) {
    return { siteDir: null, source: "environment" };
  }

  const target = cachedRuntimeDir(python);
  if (installedMcpVersion(python, target) === required) {
    return { siteDir: target, source: target };
  }

  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temp = `${target}.install-${process.pid}-${Date.now()}`;
  fs.rmSync(temp, { recursive: true, force: true });
  fs.mkdirSync(temp, { recursive: true });

  const pipCheck = spawnSync(
    python.command,
    [...python.prefix, "-m", "pip", "--version"],
    { encoding: "utf8", windowsHide: true }
  );
  if (pipCheck.status !== 0) {
    fs.rmSync(temp, { recursive: true, force: true });
    fail(
      "Python was found, but pip is unavailable.",
      "Install pip for that Python or set LEANCODEX_PYTHON to a Python 3.11+ environment that includes pip."
    );
  }

  const install = spawnSync(
    python.command,
    [
      ...python.prefix,
      "-m",
      "pip",
      "install",
      "--disable-pip-version-check",
      "--no-input",
      "--no-warn-script-location",
      "--target",
      temp,
      "-r",
      REQUIREMENTS,
    ],
    {
      encoding: "utf8",
      windowsHide: true,
      timeout: 300000,
    }
  );
  if (install.error || install.status !== 0) {
    fs.rmSync(temp, { recursive: true, force: true });
    fail(
      "could not install the pinned MCP runtime into LeanCodex's private cache.",
      [install.error && install.error.message, install.stderr, install.stdout]
        .filter(Boolean)
        .join("\n")
    );
  }

  if (installedMcpVersion(python, temp) !== required) {
    fs.rmSync(temp, { recursive: true, force: true });
    fail("the freshly installed MCP runtime could not be imported.");
  }

  try {
    if (fs.existsSync(target)) {
      if (installedMcpVersion(python, target) === required) {
        fs.rmSync(temp, { recursive: true, force: true });
        return { siteDir: target, source: target };
      }
      fs.rmSync(target, { recursive: true, force: true });
    }
    fs.renameSync(temp, target);
  } catch (error) {
    if (fs.existsSync(target)) {
      if (installedMcpVersion(python, target) === required) {
        fs.rmSync(temp, { recursive: true, force: true });
        return { siteDir: target, source: target };
      }
    }
    fs.rmSync(temp, { recursive: true, force: true });
    fail("could not finalize the private MCP runtime cache.", String(error));
  }

  return { siteDir: target, source: target };
}

const python = findPython();
const runtime = ensureRuntime(python);

if (process.env.LEANCODEX_BOOTSTRAP_ONLY === "1") {
  process.stderr.write(
    `LeanCodex launcher: runtime ready (${runtime.source}).\n`
  );
  process.exit(0);
}

const serverArgs = runtime.siteDir
  ? [
      ...python.prefix,
      "-c",
      [
        "import runpy, site, sys",
        "site.addsitedir(sys.argv[1])",
        'runpy.run_path(sys.argv[2], run_name="__main__")',
      ].join("; "),
      runtime.siteDir,
      SERVER,
    ]
  : [...python.prefix, SERVER];

const child = spawn(
  python.command,
  serverArgs,
  {
    env: process.env,
    stdio: "inherit",
    windowsHide: true,
  }
);

child.on("error", (error) => {
  fail("could not start the MCP server.", String(error));
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    try {
      child.kill(signal);
    } catch {
      // The child may already have exited.
    }
  });
}

child.on("exit", (code) => {
  process.exit(code === null ? 1 : code);
});
