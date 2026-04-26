(() => {
  const namespace = "__YT_SCRAPER_SHARED__";
  if (globalThis[namespace]?.resolveYouTubePlayerResponse) {
    return;
  }

  function resolveYouTubeVideoId(details, url = location.href) {
    if (details?.videoId) {
      return details.videoId;
    }

    const currentUrl = new URL(url);
    const watchId = currentUrl.searchParams.get("v");
    if (watchId) {
      return watchId;
    }

    const shortsMatch = currentUrl.pathname.match(/^\/shorts\/([^/?]+)/);
    return shortsMatch?.[1] || null;
  }

  function extractJsonObject(text, startIndex) {
    let depth = 0;
    let inString = false;
    let escaped = false;

    for (let index = startIndex; index < text.length; index += 1) {
      const char = text[index];

      if (inString) {
        if (escaped) {
          escaped = false;
        } else if (char === "\\") {
          escaped = true;
        } else if (char === "\"") {
          inString = false;
        }
        continue;
      }

      if (char === "\"") {
        inString = true;
        continue;
      }

      if (char === "{") {
        depth += 1;
      } else if (char === "}") {
        depth -= 1;
        if (depth === 0) {
          return text.slice(startIndex, index + 1);
        }
      }
    }

    return null;
  }

  function extractPlayerResponseFromScripts() {
    const scripts = document.querySelectorAll("script");
    const markerPattern = /(?:var\s+)?ytInitialPlayerResponse\s*=\s*/;

    for (const script of scripts) {
      const text = script.textContent || "";
      const match = markerPattern.exec(text);
      if (!match) {
        continue;
      }

      let jsonStart = match.index + match[0].length;
      while (jsonStart < text.length && /\s/.test(text[jsonStart])) {
        jsonStart += 1;
      }

      if (text[jsonStart] !== "{") {
        continue;
      }

      const payload = extractJsonObject(text, jsonStart);
      if (!payload) {
        continue;
      }

      try {
        return JSON.parse(payload);
      } catch {
        // Try the next script tag if this payload is malformed.
      }
    }

    return null;
  }

  async function fetchPlayerResponseFromInnertube(videoId) {
    const apiKey = window.ytcfg?.get?.("INNERTUBE_API_KEY");
    const clientInfo = window.ytcfg?.get?.("INNERTUBE_CONTEXT");

    if (!apiKey || !clientInfo) {
      throw new Error("YouTube page data not available. Try refreshing the page.");
    }

    const response = await fetch(`/youtubei/v1/player?key=${apiKey}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ videoId, context: clientInfo }),
    });

    if (!response.ok) {
      throw new Error(`Innertube API failed: HTTP ${response.status}`);
    }

    return response.json();
  }

  async function resolveYouTubePlayerResponse() {
    let playerResponse = window.ytInitialPlayerResponse;

    if (!playerResponse || !playerResponse.videoDetails) {
      playerResponse = extractPlayerResponseFromScripts();
    }

    if (!playerResponse || !playerResponse.videoDetails) {
      const videoId = resolveYouTubeVideoId(playerResponse?.videoDetails);
      if (!videoId) {
        throw new Error("Could not determine video ID.");
      }
      playerResponse = await fetchPlayerResponseFromInnertube(videoId);
    }

    if (!playerResponse) {
      throw new Error("Could not read YouTube player data. Try reloading the page (Ctrl+R).");
    }

    return playerResponse;
  }

  globalThis[namespace] = {
    resolveYouTubePlayerResponse,
    resolveYouTubeVideoId,
  };
})();
