# Mutations — selection room

A ready-to-use interactive demo of **Mutations of My Word**. The prompt catalog and three starting questions are included; every candidate is generated locally.

## Start

On macOS, double-click **Start Mutations.command**. Or run:

```sh
python3 run.py
```

The application opens your browser automatically. On first launch it creates a private virtual environment and downloads the pinned Stable Diffusion 1.5 checkpoint plus LCM adapter (about 3 GB); no API key or ComfyUI is required. The model selector also offers DreamShaper 8 and Realistic Vision 5.1; those optional checkpoints are downloaded only when selected. Subsequent launches use the local cache. Requires Python 3.10 or newer and an internet connection only for a model’s first download. Your workspace is saved automatically in `.mutations/` beside the application. Starting again preserves it; launching twice reuses the running workspace.

## Try the demo

1. **Keep** one or more of the nine images that you would develop. **Reject** images you have assessed and ruled out; **undecided** stays unlabeled.
2. Optionally open an image to add a reason, score its relationship to the work, productive departure or specificity, or choose a favourite. Scores and reviewer name are optional.
3. Click **Save & next nine**. Your choices are recorded and guide the next batch automatically.

Each round is a real local inference job: nine candidates are rendered at 512×512. You can Keep, Reject, score or annotate each candidate as soon as it appears; **Save & next nine** unlocks after the full round is ready. Initial seeds are random; descendant seeds participate in crossover and mutation. An exact parent recipe, including its seed, may survive across generations, while a diversity guard prevents two siblings in the same generation from having completely identical executable genomes. **Keep** is positive parent evidence, **undecided** is neutral baseline evidence, and **reject** explicitly excludes a candidate. Crossover and prompt/model/seed mutations produce the next recipes. Every image records its seed, exact recipe, execution device and parent IDs. Use **Pause** and **Retry** to stop/resume a job; completed images are retained. **Start new session** begins another population and retains earlier history.

Each individual has a broad state representation: an executable `genome` containing generator, conditioning, references, ordered program operations, temporal state and constraints; a `phenotype` containing rendered assets and execution metadata; ancestry and variation operators; and an `evaluation` containing the latest human fitness observation. The current worker implements the `generate → image` operation, while the representation is ready for multi-stage image/video programs.

**Source & context** contains the three starting questions and the prompt library. **Reveal identities** shows model, recipe and lineage. **Shuffle** preserves candidate identity and records display order. Keys **1–9** toggle keep; **Shift + 1–9** rejects, outside text fields and buttons.

**Export session** downloads all local generated rounds and their feedback as JSON, with explicit provenance. This is automated generation with human selection, not a trained artistic evaluator: the fitness signal is the review (keep/reject, favourite, optional 0–4 rubrics and notes). Reported seeds are preserved and selection history is recorded separately from generated ancestry.

## Your own experiments (optional)

**Import your own outputs** accepts exactly nine images or clips. PNG, JPEG, WebP, GIF, AVIF, MP4, WebM and MOV are supported, up to 128 MB each. Playback depends on your browser's codecs. Candidate metadata is optional. Imported generations retain the original **Submit feedback** action; their next outputs can be supplied by a model controller.

For a local controller, use a JSON manifest with nine candidates:

```json
{
  "title": "Generation 02",
  "experiment": "MyWord_02A / departures",
  "prompt": "a medical doctor talking to the camera …",
  "candidates": [
    {
      "id": "g02-c01",
      "file": "outputs/c01.png",
      "genome": {"model": "your-model", "prompt": "your evolved prompt", "seed": 49},
      "parents": ["g01-c03", "g01-c07"]
    }
  ]
}
```

The excerpt shows one candidate; supply nine with distinct IDs. Files resolve relative to the manifest. The upload form also accepts this metadata, matching each `file` basename to an uploaded filename.

```sh
python3 -m mutations_ui import path/to/generation.json
python3 -m mutations_ui export > feedback.json
python3 -m mutations_ui export --include-demo > demo-feedback.json
```

The default CLI/API export excludes demo data. The UI deliberately includes it when you choose **Export demo session**. Media files are referenced rather than embedded in JSON; back up `.mutations/` for the complete dataset.

## Feedback and persistence

Drafts are saved in your browser. Submitted feedback is stored in SQLite with append-only revisions. A stale submission from another tab returns a conflict instead of overwriting a newer review. Repeated requests for the next batch reuse the same continuation for that review revision.

Exports derive keep-over-explicit-reject pairs, plus favourite-over-other-keep pairs. Undecided candidates are unlabeled. An unrated rubric dimension is `null`; zero is an actual rating. Pairwise rows from one review share context and are **not independent votes**. Only the latest review supplies training comparisons; earlier revisions remain in the history. Preserve review and lineage grouping when fitting or validating an evaluator.

The service uses the Python standard library, plain HTML/CSS/JavaScript and SQLite around a direct Diffusers worker. It listens on loopback only. The initial page is delivered as one self-contained response; generation updates and grid previews then arrive over one reconnecting event stream. Full-resolution media is fetched only when opened. There is no ComfyUI dependency or remote hosting requirement. The demo generates still images from a selectable local model registry; it does not claim automatic video generation, identity-LoRA training or a learned fitness model.

## Advanced options

```sh
python3 -m mutations_ui serve --no-browser
python3 -m mutations_ui serve --port 8766
python3 -m mutations_ui --data-dir /path/to/experiment serve
```

The terminal logs each HTTP request with status, response size and elapsed time, plus generation milestones. This makes it possible to distinguish a request that reached the application from one that failed before it arrived.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/bootstrap` | Compact startup snapshot: navigation summaries, selected generation, catalog and models. |
| `GET /api/generations/{id}/events` | Reconnecting live stream of generation snapshots and progress. |
| `GET /api/catalog` | Included prompts and ready-made starting points. |
| `POST /api/demo` | Start a fresh demo; optional `preset`: `overview`, `style`, or `prompts`. |
| `POST /api/generations/{id}/next` | Continue a submitted demo review with `expected_revision`. |
| `POST /api/assets` | Upload raw media; percent-encoded filename in `X-Filename`. Returns `asset_id`. |
| `POST /api/generations` | Import nine `{id, asset_id, genome, parents}` candidates. |
| `GET /api/generations` | All generations and their latest submitted reviews, newest first. |
| `GET /api/generations/{id}` | Read one generation and its latest review. |
| `POST /api/generations/{id}/reviews` | Submit all nine observations with `expected_revision` (0 initially). |
| `GET /api/export` | Export real experiments; `?include_demo=1` includes demo rounds. |

JSON requests require `Content-Type: application/json`. Review candidates have `candidate_id`, `decision` (`keep`, `reject`, `undecided`), `scores` (`relationship`, `departure`, `specificity`, each `null` or an integer 0–4), and `note`. An optional `favourite_id` must identify a kept candidate. `display_order` lists all nine IDs; `reviewer` and generation `note` are optional.

## Verify

```sh
python3 -m unittest discover -s tests -v
node --check mutations_ui/web/app.js
```
