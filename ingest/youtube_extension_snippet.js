/*
  Minimal Chrome extension content-script snippet to scrape YouTube transcript + summary.
  Inject into watch pages and send data to your background script or webhook.
*/

import {
  resolveYouTubePlayerResponse,
  resolveYouTubeVideoId,
} from "../extensions/youtube-scraper/youtube_player_response_helper_module.js";

export async function scrapeYouTubeSummaryAndTranscript() {
  const playerResponse = await resolveYouTubePlayerResponse();
  const details = playerResponse.videoDetails || {};
  const title = details.title || document.title.replace(/\s*-\s*YouTube$/, "");
  const videoId = resolveYouTubeVideoId(details);
  const watchUrl = videoId ? `https://www.youtube.com/watch?v=${videoId}` : location.href;

  const tracks =
    playerResponse?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
  if (!tracks.length) {
    throw new Error("No caption tracks found");
  }

  const preferred = tracks.find((t) => t.languageCode === "en") || tracks[0];
  const transcriptRes = await fetch(preferred.baseUrl, { credentials: "omit" });
  const transcriptXml = await transcriptRes.text();
  const xmlDoc = new DOMParser().parseFromString(transcriptXml, "text/xml");
  const transcript = [...xmlDoc.querySelectorAll("text")]
    .map((n) => n.textContent?.replace(/\s+/g, " ").trim() || "")
    .filter(Boolean)
    .join("\n");

  const summary = transcript
    .split(/(?<=[.!?])\s+/)
    .filter(Boolean)
    .slice(0, 5)
    .join(" ");

  return {
    title,
    url: watchUrl,
    videoId,
    language: preferred.languageCode || "unknown",
    summary,
    transcript,
    scrapedAt: new Date().toISOString(),
  };
}

globalThis.scrapeYouTubeSummaryAndTranscript = scrapeYouTubeSummaryAndTranscript;

// Example usage:
// scrapeYouTubeSummaryAndTranscript().then(console.log).catch(console.error);
