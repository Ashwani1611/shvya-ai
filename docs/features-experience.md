# Public Features experience

`/features/` renders `templates/features.html` inside the same `marketing/dark_base.html`, header and footer as the current public landing page. Keep this interactive experience when changing the marketing theme; route tests assert its assets and controls so a static catalogue cannot silently replace it again.

The page-specific CSS and JavaScript are scoped to `.premium-features`. The existing sidebar product catalogue is represented by accessible tabs. The customer journey is an illustrative office-space enquiry, not a customer testimonial or a live account.

## Mascot and accessibility

The mascot body derives from the user-supplied orb artwork, with independently drawn eyes. Pointer tracking, hover listening, greeting, wink, nod, thinking and celebration are presentation states only. Intersection observation limits pointer updates to visible mascots. Motion pauses when requested, follows the system reduced-motion preference, and stops while the page is hidden. All click interactions also support keyboard activation; both tab groups support arrow keys, Home and End.

## Film

`shvya-cinematic-film.mp4` is a 30-second original motion-design film. Its fictional office establishing frame and laptop close-up were generated for Shvya; they are not footage of actual staff or customers. Cinematic camera movement and product overlays are composed around these still scenes. The page labels them as illustrative AI-generated scenes. It uses the original Shvya narration and matching English VTT captions, and provides a text transcript. No x.ai footage, script or brand assets are used.

The film loads only after a visitor opens the native dialog. Closing it pauses playback and restores keyboard focus to the play button. New filenames prevent caches from retaining the old film.

## Release verification

Check the public URL with and without the trailing slash, shared header links, all feature tabs, the four journey steps, motion pause, mobile overflow, film play/close/Escape and voice/captions. After deployment verify the page includes `premium-features.js` and `shvya-cinematic-film.mp4`, rather than relying only on the service health endpoint.
