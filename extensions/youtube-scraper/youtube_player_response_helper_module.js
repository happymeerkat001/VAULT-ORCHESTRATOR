import "./youtube_player_response_helper.js";

const shared = globalThis.__YT_SCRAPER_SHARED__;

if (!shared?.resolveYouTubePlayerResponse || !shared?.resolveYouTubeVideoId) {
  throw new Error("YouTube shared helper failed to initialize.");
}

export const resolveYouTubePlayerResponse = (...args) =>
  shared.resolveYouTubePlayerResponse(...args);

export const resolveYouTubeVideoId = (...args) =>
  shared.resolveYouTubeVideoId(...args);
