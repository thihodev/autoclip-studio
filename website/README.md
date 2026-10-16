# AutoClip Studio website

The landing page at https://autoclip-connect.thihodev.workers.dev — static files in `public/`, served by Cloudflare
Workers (free plan).

- `public/index.html` + `site.css`: the page. Screenshots in `public/shots/`, the demo video in `public/media/`,
  style posters in `public/styles/` (from `app/assets/styles/framejam`, MIT).
- Deploy: `npx wrangler login` once, then `npx wrangler deploy` in this folder.
- Preview: `npx wrangler dev` → http://localhost:8787
