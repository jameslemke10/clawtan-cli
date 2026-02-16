#!/usr/bin/env node
const { execFileSync } = require("child_process");
const path = require("path");

const script = path.join(__dirname, "..", "clawtan", "cli.py");
try {
  execFileSync("python3", [script, ...process.argv.slice(2)], {
    stdio: "inherit",
  });
} catch (e) {
  process.exit(e.status || 1);
}
