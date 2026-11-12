/**
 * AuditFlow static site Worker.
 *
 * Serves the prebuilt assets in ./public and exposes /health for uptime checks.
 * All behaviour lives in the static files; there is no server-side state.
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok", {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    return env.ASSETS.fetch(request);
  },
};
