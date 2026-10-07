const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const desktopRoot = path.resolve(__dirname, "..");
const projectRoot = path.resolve(desktopRoot, "..");
const python = process.env.ACEVAL_BUILD_PYTHON || "python3";
const targetArch = process.arch === "arm64" ? "arm64" : process.arch === "x64" ? "x86_64" : process.arch;
const data = `${path.join(projectRoot, "src/aceval/d2c_driver")}${path.delimiter}aceval/d2c_driver`;
const configRoot = path.join(desktopRoot, ".pyinstaller-cache");

fs.mkdirSync(configRoot, { recursive: true });
const result = spawnSync(
  python,
  [
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
    "--name", "forge-kernel",
    "--target-arch", targetArch,
    "--distpath", path.join(desktopRoot, "build"),
    "--workpath", path.join(desktopRoot, ".pyinstaller-work"),
    "--specpath", path.join(desktopRoot, ".pyinstaller-spec"),
    "--paths", path.join(projectRoot, "src"),
    "--add-data", data,
    path.join(desktopRoot, "kernel_entry.py"),
  ],
  {
    cwd: desktopRoot,
    env: { ...process.env, PYINSTALLER_CONFIG_DIR: configRoot },
    stdio: "inherit",
    shell: false,
  },
);

if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status || 1);

const executable = path.join(desktopRoot, "build/forge-kernel", process.platform === "win32" ? "forge-kernel.exe" : "forge-kernel");
if (!fs.existsSync(executable)) throw new Error("PyInstaller completed without a Kernel executable");
