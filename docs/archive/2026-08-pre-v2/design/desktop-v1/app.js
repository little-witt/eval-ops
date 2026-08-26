(() => {
  "use strict";

  const screens = new Set(["home", "new-task", "workbench", "decision", "settings", "resources"]);
  let wizardStep = 1;

  function switchScreen(name, updateUrl = true) {
    if (!screens.has(name)) name = "home";
    document.querySelectorAll("[data-screen]").forEach((screen) => {
      screen.classList.toggle("active", screen.dataset.screen === name);
    });
    document.querySelectorAll(".primary-nav .nav-item").forEach((item) => {
      const target = item.dataset.screenTarget;
      const taskScreen = ["home", "new-task", "workbench", "decision"].includes(name);
      item.classList.toggle("active", (target === "home" && taskScreen) || target === name);
    });
    if (updateUrl) {
      const url = new URL(window.location.href);
      url.searchParams.set("view", name);
      window.history.replaceState({}, "", url);
    }
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  function renderWizard() {
    document.querySelectorAll("[data-wizard-panel]").forEach((panel) => {
      panel.classList.toggle("active", Number(panel.dataset.wizardPanel) === wizardStep);
    });
    document.querySelectorAll("[data-wizard-step]").forEach((step) => {
      const value = Number(step.dataset.wizardStep);
      step.classList.toggle("active", value === wizardStep);
      step.classList.toggle("done", value < wizardStep);
      const badge = step.querySelector("b");
      badge.textContent = value < wizardStep ? "✓" : String(value);
    });
    document.getElementById("current-step").textContent = String(wizardStep);
    document.querySelector(".step-progress i").style.width = `${wizardStep * 25}%`;
    const previous = document.getElementById("wizard-prev");
    const next = document.getElementById("wizard-next");
    previous.disabled = wizardStep === 1;
    const labels = ["继续：Case 与预期 →", "继续：检查环境 →", "继续：确认预算 →", "启动首次评测"];
    next.textContent = labels[wizardStep - 1];
  }

  function showToast(message) {
    let toast = document.querySelector(".prototype-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.className = "prototype-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast.timeout);
    showToast.timeout = window.setTimeout(() => toast.classList.remove("show"), 2400);
  }

  document.querySelectorAll("[data-screen-target]").forEach((control) => {
    control.addEventListener("click", () => switchScreen(control.dataset.screenTarget));
    control.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") switchScreen(control.dataset.screenTarget);
    });
  });

  document.querySelectorAll("[data-wizard-step]").forEach((step) => {
    step.addEventListener("click", () => {
      wizardStep = Number(step.dataset.wizardStep);
      renderWizard();
    });
  });

  document.getElementById("wizard-prev").addEventListener("click", () => {
    wizardStep = Math.max(1, wizardStep - 1);
    renderWizard();
  });

  document.getElementById("wizard-next").addEventListener("click", () => {
    if (wizardStep < 4) {
      wizardStep += 1;
      renderWizard();
      return;
    }
    showToast("设计稿：真实版本会在这里创建任务并进入评测工作台");
  });

  document.querySelectorAll(".intent-card").forEach((card) => {
    card.addEventListener("click", () => {
      document.querySelectorAll(".intent-card").forEach((item) => item.classList.remove("selected"));
      card.classList.add("selected");
    });
  });

  document.querySelectorAll(".workbench-tabs button, .table-filters button, .settings-nav button").forEach((button) => {
    button.addEventListener("click", () => {
      const group = button.parentElement;
      group.querySelectorAll(":scope > button").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      if (!button.closest(".table-filters")) showToast("设计稿已切换视图；该模块将在确认后接入真实数据");
    });
  });

  document.querySelectorAll(".cause-item").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".cause-item").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      showToast("已切换根因簇，生产版本会同步定位对应 diff 与 Case");
    });
  });

  document.querySelectorAll(".diff-file-list > div").forEach((row) => {
    row.addEventListener("click", () => {
      document.querySelectorAll(".diff-file-list > div").forEach((item) => item.classList.remove("selected"));
      row.classList.add("selected");
      showToast(`已选择 ${row.querySelector("strong").textContent}`);
    });
  });

  document.querySelector(".approve-button").addEventListener("click", () => showToast("设计稿：批准后创建任务分支并执行最终复测"));
  document.querySelector(".request-change").addEventListener("click", () => showToast("可在备注中补充要求，并重新生成候选"));
  document.querySelector(".reject-button").addEventListener("click", () => showToast("本轮候选将被保留为审计记录，但不会发布"));
  document.getElementById("hide-note").addEventListener("click", () => document.querySelector(".prototype-note").classList.add("hidden"));

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      showToast("快捷搜索：任务、Skill、Case、会话或文件");
    }
  });

  const initial = new URL(window.location.href).searchParams.get("view") || "home";
  switchScreen(initial, false);
  renderWizard();
})();
