(() => {
  const BUTTON_GROUP_ID = "yt-obsidian-transcript-save-group";
  const STATUS_ID = "yt-obsidian-transcript-status";
  const SERVER_URL = "http://localhost:8765/transcript";

  function createUi() {
    if (document.getElementById(BUTTON_GROUP_ID)) {
      return;
    }

    const container = document.createElement("div");
    container.style.display = "inline-flex";
    container.style.alignItems = "center";
    container.style.gap = "8px";
    container.style.marginLeft = "12px";

    const buttonGroup = document.createElement("div");
    buttonGroup.id = BUTTON_GROUP_ID;
    buttonGroup.style.display = "inline-flex";
    buttonGroup.style.alignItems = "stretch";

    const buttons = [
      createButton("📝 Transcript.lol", "full", {
        borderRadius: "18px 0 0 18px",
        borderRight: "none"
      }),
      createButton("▶ YouTube Only", "youtube", {
        borderRadius: "0 18px 18px 0"
      })
    ];

    const status = document.createElement("span");
    status.id = STATUS_ID;
    status.style.fontSize = "12px";
    status.style.color = "#606060";

    async function handleSave(mode, clickedButton) {
      setStatus("Saving...", "#606060");
      setButtonsDisabled(buttons, true);
      try {
        const expandButton =
          document.querySelector("ytd-text-inline-expander tp-yt-paper-button#expand") ||
          document.querySelector("#description-inline-expander tp-yt-paper-button#expand") ||
          document.querySelector("tp-yt-paper-button#expand");
        if (expandButton instanceof HTMLElement) {
          expandButton.click();
          await new Promise((r) => setTimeout(r, 1000));
        }

        const description =
          document.querySelector("#description-inner")?.innerText?.trim() ||
          document.querySelector("#description")?.innerText?.trim() ||
          document.querySelector('meta[name="description"]')?.content?.trim() ||
          "";

        const aiParas = [...document.querySelectorAll("p.videoSummaryContentViewModelParagraph")]
          .map((p) => p.textContent?.trim())
          .filter(Boolean);
        const ai_summary = aiParas.join("\n\n");

        const response = await fetch(SERVER_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            mode,
            url: window.location.href,
            title: document.title.replace(/\s*-\s*YouTube\s*$/, "").trim(),
            description,
            ai_summary
          })
        });

        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.status !== "ok") {
          const message = payload.message || `HTTP ${response.status}`;
          setStatus(`✗ Error: ${message}`, "#c00");
        } else {
          setStatus(`✓ Saved (${payload.source || "transcript"})`, "#0a7a0a");
        }
      } catch (error) {
        setStatus("✗ Start transcript_server.py first", "#c00");
      } finally {
        clickedButton.blur();
        setButtonsDisabled(buttons, false);
      }
    }

    for (const button of buttons) {
      button.addEventListener("click", () => handleSave(button.dataset.mode, button));
      buttonGroup.appendChild(button);
    }

    container.appendChild(buttonGroup);
    container.appendChild(status);

    const host =
      document.querySelector("#above-the-fold #top-row") ||
      document.querySelector("ytd-watch-metadata #top-row");
    if (host) {
      host.appendChild(container);
    }
  }

  function createButton(label, mode, styleOverrides) {
    const button = document.createElement("button");
    button.dataset.mode = mode;
    button.textContent = label;
    button.style.padding = "8px 12px";
    button.style.border = "1px solid #0f0f0f33";
    button.style.cursor = "pointer";
    button.style.fontSize = "13px";
    button.style.background = "#f2f2f2";
    button.style.color = "#0f0f0f";
    button.style.lineHeight = "1.2";
    button.style.whiteSpace = "nowrap";
    Object.assign(button.style, styleOverrides);
    return button;
  }

  function setStatus(message, color) {
    const status = document.getElementById(STATUS_ID);
    if (!status) return;
    status.textContent = message;
    status.style.color = color;
  }

  function setButtonsDisabled(buttons, disabled) {
    for (const button of buttons) {
      button.disabled = disabled;
      button.style.opacity = disabled ? "0.7" : "1";
      button.style.cursor = disabled ? "default" : "pointer";
    }
  }

  const observer = new MutationObserver(() => {
    createUi();
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true
  });

  createUi();
})();
