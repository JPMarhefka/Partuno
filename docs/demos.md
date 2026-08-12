# Partuno demo media

The strongest public demos show a real deployed connection and a short,
focused workflow. Record from an operator-owned Render deployment if that is
the environment you want viewers to understand; there is no need to run the
local server just to produce screenshots or GIFs.

## Recommended clips

| Clip | What to show | What to hide |
| --- | --- | --- |
| MCP connection | ChatGPT adds Partuno, completes consent, and lists tools | OAuth codes, callback state, email, client IDs, private service URLs |
| Component research | A search, filters, product details, and source evidence | Provider account IDs, private pricing, API keys |
| Cross-provider comparison | The same exact MPN and quantity compared across DigiKey and Mouser | Customer, project, quote, or order identifiers |
| Requirements recommendation | Hard requirements, evidence states, and a qualified shortlist | Internal BOM names and project data |
| Safety boundary | A list/quote/cart preview and the confirmation boundary | Real write tokens, private list/cart IDs, or anything that could be reused |

Keep each clip to one idea. Eight to twenty seconds is usually enough for a
GIF. A longer workflow is better as an MP4 or linked video, with a short GIF
used as the README preview.

## Record on a Mac

1. Open the deployed Partuno connection in ChatGPT or the MCP client.
2. Use **Shift-Command-5**, choose **Record Selected Portion**, and select
   only the application area needed for the story.
3. Turn off the microphone unless narration is important. If narration is
   useful, record it separately so the GIF stays silent and small.
4. Perform one clean run with realistic but non-sensitive inputs.
5. Stop the recording and keep the original `.mov` or exported `.mp4` as the
   source file. Apple documents the built-in workflow in its [screen recording
   guide](https://support.apple.com/en-us/102618).

QuickTime Player is another built-in option: choose **File > New Screen
Recording**, select the desired area, and stop from the menu bar. See Apple's
[QuickTime recording guide](https://support.apple.com/guide/quicktime-player/record-your-screen-qtp97b08e666/mac)
for the current controls.

## Protect the recording

Before exporting a public asset, review the entire clip frame by frame. Do not
show API keys, bearer tokens, OAuth authorization codes, callback state,
private Render URLs, email addresses, client identifiers, customer data,
account numbers, order numbers, private BOMs, or reusable mutation tokens.
Prefer synthetic part searches and redacted account examples. Crop or blur
anything that appears briefly during navigation, not only the final result.

## Convert a screen video to a GIF

The exact editor does not matter. A browser-based editor can trim the clip,
crop the browser chrome, blur sensitive regions, and export a GIF. Use a short
loop, a width around 800–1200 pixels, and roughly 10–15 frames per second so
text remains readable without making the repository asset unnecessarily large.

If you use FFmpeg, a two-pass palette generally gives better text and color
than a one-line conversion:

```bash
ffmpeg -i partuno-demo.mov \
  -vf "fps=12,scale=1100:-1:flags=lanczos,palettegen" \
  /tmp/partuno-palette.png

ffmpeg -i partuno-demo.mov -i /tmp/partuno-palette.png \
  -lavfi "fps=12,scale=1100:-1:flags=lanczos[x];[x][1:v]paletteuse" \
  -loop 0 partuno-demo.gif
```

For a GUI-only workflow, trim and redact in the editor of your choice, export
the short MP4, then use its GIF export or an image-conversion utility. Keep
the MP4 for detailed review when a GIF makes small interface text hard to
read.

## Suggested repository assets

Use descriptive names if you add additional demos:

- `docs/assets/mcp-connection.gif`
- `docs/assets/component-research.gif`
- `docs/assets/cross-provider-comparison.gif`
- `docs/assets/recommendation-safety.gif`

The current `docs/assets/partuno-demo.gif` is intentionally a representative
preview rather than evidence of a particular user's account or a shared live
service. Replace or supplement it with sanitized deployed recordings when
ready.
