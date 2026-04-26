const scrapeBtn = document.getElementById("scrapeBtn");
const output = document.getElementById("output");
const statusEl = document.getElementById("status");

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.className = isError ? "status error" : "status";
}

async function queryActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs.length || !tabs[0].id) {
    throw new Error("No active tab found.");
  }
  return tabs[0];
}

function isSupportedYouTubeUrl(url) {
  if (!url) {
    return false;
  }
  try {
    const parsed = new URL(url);
    const isYouTubeHost =
      parsed.hostname === "www.youtube.com" ||
      parsed.hostname === "youtube.com" ||
      parsed.hostname === "m.youtube.com";
    if (!isYouTubeHost) {
      return false;
    }
    return parsed.pathname === "/watch" || parsed.pathname.startsWith("/shorts/");
  } catch {
    return false;
  }
}

async function injectAndScrape(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    files: ["youtube_player_response_helper.js"]
  });

  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: async () => {
      const shared = globalThis.__YT_SCRAPER_SHARED__;
      if (!shared?.resolveYouTubePlayerResponse || !shared?.resolveYouTubeVideoId) {
        throw new Error("YouTube scraper helper failed to load.");
      }

      function pickTrack(tracks, preferredLang) {
        if (!Array.isArray(tracks) || tracks.length === 0) {
          throw new Error("No caption tracks found for this video.");
        }
        return (
          tracks.find((track) => track.languageCode === preferredLang) ||
          tracks.find((track) => String(track.languageCode || "").startsWith(preferredLang)) ||
          tracks[0]
        );
      }

      function buildSummary(transcript, sentenceCount) {
        const sentences = transcript
          .replace(/\s+/g, " ")
          .split(/(?<=[.!?])\s+/)
          .map((sentence) => sentence.trim())
          .filter(Boolean);
        if (sentences.length <= sentenceCount) {
          return sentences.join(" ");
        }
        return sentences.slice(0, sentenceCount).join(" ");
      }

      const playerResponse = await shared.resolveYouTubePlayerResponse();
      const details = playerResponse.videoDetails || {};
      const videoId = shared.resolveYouTubeVideoId(details);
      if (!videoId) {
        throw new Error("Could not determine the current YouTube video id.");
      }

      const title = details.title || document.title.replace(/\s*-\s*YouTube$/, "");
      const tracks =
        playerResponse?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
      const chosen = pickTrack(tracks, "en");

      const res = await fetch(chosen.baseUrl, { credentials: "omit" });
      if (!res.ok) {
        throw new Error(`Transcript request failed: HTTP ${res.status}`);
      }

      const transcriptXml = await res.text();
      const xmlDoc = new DOMParser().parseFromString(transcriptXml, "text/xml");
      const transcript = Array.from(xmlDoc.querySelectorAll("text"))
        .map((node) => (node.textContent || "").replace(/\s+/g, " ").trim())
        .filter(Boolean)
        .join("\n");

      if (!transcript) {
        throw new Error("Transcript was empty.");
      }

      return {
        title,
        url: `https://www.youtube.com/watch?v=${videoId}`,
        videoId,
        language: chosen.languageCode || "unknown",
        summary: buildSummary(transcript, 5),
        transcript,
        scrapedAt: new Date().toISOString()
      };
    }
  });

  if (!result) {
    throw new Error("Injected scraper returned no result.");
  }
  return result;
}

async function runScrape() {
  setStatus("Scraping transcript...");
  output.textContent = "{}";
  const tab = await queryActiveTab();

  if (!isSupportedYouTubeUrl(tab.url)) {
    throw new Error("Open a YouTube watch page or Shorts page first.");
  }

  const payload = await injectAndScrape(tab.id);
  output.textContent = JSON.stringify(payload, null, 2);
  setStatus("Done. Copy JSON from the box.");
}

scrapeBtn.addEventListener("click", () => {
  runScrape().catch((err) => {
    setStatus(err.message || String(err), true);
  });
});
