import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const requireFromSkill = createRequire(
  "/Users/liuzhenni/.agents/skills/chrome-devtools/scripts/package.json"
);
const puppeteer = requireFromSkill("puppeteer");

const project = "/Users/liuzhenni/workspace/ai/eval-ops";
const source = `file://${project}/design/desktop-v2/index.html`;
const output = `${project}/docs/screenshots/forge-desktop-v3`;
await mkdir(output, { recursive: true });

const browser = await puppeteer.launch({
  headless: true,
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
  defaultViewport: { width: 1728, height: 1500, deviceScaleFactor: 1 },
});

const page = await browser.newPage();
const consoleProblems = [];
page.on("console", (message) => {
  if (["error", "warn"].includes(message.type())) {
    consoleProblems.push({ type: message.type(), text: message.text() });
  }
});
page.on("pageerror", (error) => consoleProblems.push({ type: "pageerror", text: error.message }));

const shots = [
  ["task&moment=cases", "01-case-generation.png"],
  ["task&moment=evaluation", "02-online-evaluation.png"],
  ["task&moment=decision", "03-analysis-decision.png"],
  ["create&moment=evaluation", "04-create-task.png"],
];

for (const [query, filename] of shots) {
  await page.goto(`${source}?theme=v3&view=${query}`, { waitUntil: "networkidle0" });
  await new Promise((resolve) => setTimeout(resolve, 350));
  await page.screenshot({ path: path.join(output, filename), fullPage: false });
}

await page.goto(`${source}?theme=v3&view=task&moment=evaluation`, { waitUntil: "networkidle0" });
await page.click('[data-moment="decision"]');
await page.waitForFunction(() => document.body.dataset.moment === "decision");
const interaction = await page.evaluate(() => ({
  moment: document.body.dataset.moment,
  inspectorTitle: document.querySelector(".inspector-head h3")?.textContent,
  selectedMoment: document.querySelector("[data-moment].is-active")?.dataset.moment,
  issueCount: document.querySelectorAll(".analysis-results .issue-summary button").length,
}));

await browser.close();
process.stdout.write(JSON.stringify({ success: true, screenshots: shots.map(([, name]) => path.join(output, name)), interaction, consoleProblems }, null, 2));
