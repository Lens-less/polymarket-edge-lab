import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const serverUrl = "http://127.0.0.1:8010/api/v0.2/desk/snapshot";
let serverProcess: ChildProcess | null = null;
const currentDir = fileURLToPath(new URL(".", import.meta.url));
const repoRoot = resolve(currentDir, "../../..");
const pythonExecutable =
  process.platform === "win32"
    ? resolve(repoRoot, ".venv/Scripts/python.exe")
    : "python3";

async function waitForServer(timeoutMs = 15000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(serverUrl);
      if (response.ok) {
        return;
      }
    } catch {
      // keep polling until the backend is reachable
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
  }
  throw new Error("Trade desk backend did not start in time.");
}

export async function ensureDeskServer(): Promise<void> {
  try {
    const response = await fetch(serverUrl);
    if (response.ok) {
      return;
    }
  } catch {
    // no running server; start one below
  }

  serverProcess = spawn(
    pythonExecutable,
    [
      "-m",
      "uvicorn",
      "src.profit_system.web.app:create_demo_app",
      "--factory",
      "--host",
      "127.0.0.1",
      "--port",
      "8010",
      "--log-level",
      "warning"
    ],
    {
      cwd: repoRoot,
      stdio: "ignore",
      shell: false
    }
  );

  await waitForServer();
}

export async function stopDeskServer(): Promise<void> {
  if (!serverProcess?.pid) {
    return;
  }
  const proc = serverProcess;
  serverProcess = null;
  if (process.platform === "win32") {
    await new Promise<void>((resolveKill) => {
      const killer = spawn("taskkill", ["/pid", String(proc.pid), "/t", "/f"], {
        stdio: "ignore",
        shell: true
      });
      killer.on("exit", () => resolveKill());
      killer.on("error", () => resolveKill());
    });
    return;
  }
  proc.kill("SIGTERM");
}
