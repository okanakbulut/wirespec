"""The failure artefact: one HTML file, and nothing else.

§16.4. Not a video and not a viewer application -- **a single
self-contained document** with the frames embedded as data URIs, a scrubber,
and the network and console on the same axis. The reasons are all in the spec
and all of them are about the artefact being useful at the moment somebody
needs it: no encoder means no second dependency, exact seeking beats a scrub bar
approximating one, and one file opens on a machine with no viewer installed and
no network.

**On the JavaScript in the generated page.** §3.1 forbids wirespec
from shipping, injecting or evaluating JavaScript *of its own in the page under
test*, and this does none of those things: the artefact is output, read by a
person afterwards, and it never touches the browser being driven. The boundary
is worth stating rather than leaving to be re-derived, because the file plainly
contains a ``<script>`` block. Everything that can be rendered in Python is --
the waterfall is laid out server-side, and the script only seeks, plays and
draws the playhead.
"""

import html
import json
from datetime import UTC, datetime
from pathlib import Path

from wirespec.recorder import REDACTED, SECRET_HEADERS, Timeline

__all__ = ["render", "write"]

#: Requests below this share of the span would render as a bar too thin to see,
#: and a request that took no measurable time is exactly the one somebody is
#: scanning for the absence of.
MIN_BAR = 0.4


def write(timeline: Timeline, path: str | Path, *, title: str = "") -> Path:
    """Render and save. Creates the directory if it is not there."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(timeline, title=title), encoding="utf-8")
    return destination


def render(timeline: Timeline, *, title: str = "") -> str:
    """The whole document, as a string.

    Deterministic: the same timeline renders the same bytes. Nothing here reads
    the current time -- the header shows when the *recording* started, so two
    runs of the writer over one recording produce one file.
    """
    span = timeline.span
    # Three fields, and the middle one is the point: the **label is formatted
    # here**, not by the script. Handing the script a number and asking it to
    # print it makes the artefact depend on Python's `%.3f` and JavaScript's
    # `toFixed(3)` agreeing about a half-way case, and they do not -- caught by
    # a test that read `0.029s` where Python had computed `0.030s`. The number
    # stays for arithmetic the script genuinely has to do: seeking and the
    # playhead.
    # A fourth field only when there is a second tab to name. A recording of
    # one page renders exactly what it always did, down to the bytes.
    many = len(timeline.tabs) > 1
    frames = [
        [round(timeline.at(frame.at), 4), f"{timeline.at(frame.at):.3f}s", frame.data]
        + ([_tab_name(frame.tab, timeline)] if many else [])
        for frame in timeline.frames
    ]
    return _DOCUMENT.format(
        title=html.escape(title or "recording"),
        started=_when(timeline),
        summary=html.escape(_summary(timeline)),
        note=_note(timeline),
        stage=_stage(timeline),
        ruler=_ruler(timeline),
        tabs=_tabs(timeline),
        acts="\n".join(_act(action, timeline) for action in timeline.actions) or _EMPTY_ACTS,
        rows="\n".join(_row(entry, index, timeline) for index, entry in enumerate(timeline.traffic)) or _EMPTY_ROWS,
        details="\n".join(_detail(entry, index, timeline) for index, entry in enumerate(timeline.traffic)),
        logs="\n".join(_log(message, timeline) for message in timeline.messages) or "",
        span=f"{span:.3f}",
        frames=_embed(frames),
    )


def _when(timeline: Timeline) -> str:
    if not timeline.start:
        return "an empty recording"
    return datetime.fromtimestamp(timeline.start, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _summary(timeline: Timeline) -> str:
    parts = [
        f"{timeline.span:.2f}s",
        f"{len(timeline.frames)} frames",
        f"{len(timeline.actions)} actions",
        f"{len(timeline.traffic)} requests",
        f"{len(timeline.messages)} console",
    ]
    return " · ".join(parts)


def _tab_name(tab: int, timeline: Timeline) -> str:
    """``tab 2``, or ``tab 2: /popup.html`` where the URL is known.

    ASCII on purpose: this one goes into the embedded JSON, where a ``·`` comes
    back out as ``\\u00b7`` and is unreadable to anyone looking at the file
    rather than at the page.
    """
    where = timeline.tabs[tab - 1] if 0 < tab <= len(timeline.tabs) else ""
    return f"tab {tab}: {_short(where, 40)}" if where else f"tab {tab}"


def _tabs(timeline: Timeline) -> str:
    """The legend, and only when there is something to be confused about.

    One tab needs no label anywhere: every row came from it, and a column
    repeating that on every line is a column of noise. More than one, and every
    row has to say which -- otherwise a request the popup made reads as one the
    page under test made.
    """
    if len(timeline.tabs) <= 1:
        return ""
    named = " · ".join(f"tab {number}: {html.escape(_short(url, 48))}" for number, url in enumerate(timeline.tabs, 1))
    return f'<div class="meta tabs">{named}</div>'


def _mark(tab: int, timeline: Timeline) -> str:
    """The per-row tab badge, empty for a single-page recording."""
    if len(timeline.tabs) <= 1:
        return ""
    return f'<span class="tab">{tab}</span>'


def _note(timeline: Timeline) -> str:
    """What the recording knows it lost.

    Nothing fails silently (§1, goal 4). A filmstrip that quietly
    skips reads as "nothing happened there", and a waterfall quietly missing a
    request answers the wrong question.
    """
    notes = []
    if timeline.thinned:
        notes.append(f"{timeline.thinned} frames thinned to fit the buffer — the filmstrip is coarser near the start")
    if timeline.unplaced:
        notes.append(f"{timeline.unplaced} requests could not be placed on the axis and are not shown")
    if timeline.dropped:
        notes.append(f"{timeline.dropped} frames arrived with no timestamp and are not shown")
    if not notes:
        return ""
    return '<p class="note">' + html.escape("; ".join(notes)) + "</p>"


def _stage(timeline: Timeline) -> str:
    """The screen, or an honest statement that there is not one.

    A test that failed before the first paint recorded nothing, and that is
    exactly when somebody opens the artefact. A broken scrubber over a blank
    box would send them looking for a bug in the recorder.
    """
    if not timeline.frames:
        return '<div class="stage empty">no frames — the recording ended before the page painted</div>'
    return (
        '<div class="stage"><img id="shot" alt="the page as recorded"></div>'
        '<div class="which" id="which"></div>'
        '<div class="controls">'
        '<button id="play" type="button">play</button>'
        f'<input id="scrub" type="range" min="0" max="{len(timeline.frames) - 1}" '
        f'value="{len(timeline.frames) - 1}" step="1">'
        '<output id="clock">0.000s</output>'
        "</div>"
    )


def _ruler(timeline: Timeline) -> str:
    """Five ticks across the axis, so a bar's position means something."""
    ticks = "".join(
        f'<b style="left:{fraction * 100:.0f}%">{timeline.span * fraction:.2f}s</b>'
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    return (
        f'<div class="row ruler"><span></span><span></span><span></span><span></span><span></span>'
        f'<span class="track">{ticks}</span></div>'
    )


def _row(entry, index: int, timeline: Timeline) -> str:
    """One request, laid out in Python rather than by the script.

    Two-tone: the pale part is the wait for the first byte and the solid part is
    the body arriving. The split is the difference between a slow server and a
    large response, and reading it off a single bar is guesswork.
    """
    started = entry.started
    if started is None:  # pragma: no cover -- Timeline filters these out
        return ""
    end = entry.finished or entry.responded or started
    responded = entry.responded or end
    left = timeline.at(started) / timeline.span * 100
    waiting = max((responded - started) / timeline.span * 100, 0.0)
    body = max((end - responded) / timeline.span * 100, 0.0)
    if waiting + body < MIN_BAR:
        waiting = MIN_BAR
    outcome = "failed" if entry.failed else _band(entry.status)
    detail = entry.failed or (f"{entry.status} {entry.status_text}".strip() if entry.status else "in flight")
    took = f"{(end - started) * 1000:.0f} ms"
    return (
        f'<div class="row {outcome}" data-at="{timeline.at(started):.4f}" '
        f'data-pane="req-{index}" tabindex="0">'
        f"{_mark(entry.tab, timeline)}"
        f'<span class="method">{html.escape(entry.method)}</span>'
        f'<span class="url" title="{html.escape(entry.url)}">{html.escape(_short(entry.url))}</span>'
        f'<span class="kind">{html.escape(entry.kind)}</span>'
        f'<span class="detail">{html.escape(detail)}</span>'
        f'<span class="took">{took}</span>'
        f'<span class="track"><i class="wait" style="left:{left:.3f}%;width:{waiting:.3f}%"></i>'
        f'<i class="body" style="left:{left + waiting:.3f}%;width:{body:.3f}%"></i></span>'
        "</div>"
    )


def _headers(pairs: dict[str, str], redact: bool) -> str:
    """A header block, sorted, with credentials starred out.

    Sorted because Chrome's order is neither the wire order nor stable, and a
    reader comparing two artefacts should not have to diff a shuffle.
    """
    if not pairs:
        return '<p class="absent">none</p>'
    cells = []
    for name in sorted(pairs, key=str.lower):
        value = REDACTED if redact and name.lower() in SECRET_HEADERS else pairs[name]
        cells.append(f"<dt>{html.escape(name)}</dt><dd>{html.escape(value)}</dd>")
    return "<dl>" + "".join(cells) + "</dl>"


def _body_block(entry) -> str:
    """The response body, or the reason it is not here.

    Never silently empty. §16.3's rule is that whatever a cap threw
    away is printed, and it matters more here than for frames: a blank pane
    reads as "the server sent nothing", which is a different fact from "we did
    not ask" and sends a reader somewhere else entirely.
    """
    parts = []
    if entry.body:
        parts.append(f"<pre>{html.escape(entry.body)}</pre>")
    if entry.body_note:
        parts.append(f'<p class="absent">{html.escape(entry.body_note)}</p>')
    elif entry.body is None and not entry.completed:
        # Not "empty". The body is read when the load finishes, and this one
        # never did -- which for a `fetch()` nobody drains is Chrome waiting on
        # the page rather than anything being wrong.
        parts.append('<p class="absent">not captured: the response had not completed when recording stopped</p>')
    elif not entry.body:
        parts.append('<p class="absent">empty</p>')
    return "".join(parts)


def _detail(entry, index: int, timeline: Timeline) -> str:
    """Everything known about one request, laid out in Python.

    §16.4 keeps the script to seeking, playing and the playhead, so
    the panes are all rendered here and the script only decides which one is
    shown. That also means the detail survives with JavaScript off -- the panes
    are `:target`-addressable, so the drawer still works from a link.
    """
    outcome = entry.failed or (f"{entry.status} {entry.status_text}".strip() if entry.status else "in flight")
    facts = {
        "URL": entry.url,
        "Method": entry.method,
        "Type": entry.kind,
        "Status": outcome,
        "MIME": entry.mime,
        "Protocol": entry.protocol,
        "Remote": entry.remote,
        "Size": f"{entry.size:,.0f} bytes" if entry.size else "",
        "Cached": "from disk cache" if entry.cached else "",
        # The number alone: the legend at the top already carries the URL, and
        # repeating it here spends the drawer's narrow width on it twice.
        "Tab": f"tab {entry.tab}" if len(timeline.tabs) > 1 else "",
    }
    general = (
        "<dl>"
        + "".join(
            f"<dt>{html.escape(name)}</dt><dd>{html.escape(str(value))}</dd>" for name, value in facts.items() if value
        )
        + "</dl>"
    )

    payload = ""
    if entry.post_data is not None:
        shown = (
            f"<pre>{html.escape(entry.post_data)}</pre>"
            if entry.post_data
            else '<p class="absent">larger than Chrome kept — raise Network.enable maxPostDataSize to see it</p>'
        )
        payload = f"<details open><summary>payload</summary>{shown}</details>"

    return (
        f'<div class="pane" id="req-{index}">'
        f"<h3>{html.escape(entry.method)} {html.escape(_short(entry.url, 80))}"
        f' <span class="status">{html.escape(outcome)}</span></h3>'
        f"<details open><summary>general</summary>{general}</details>"
        f"<details><summary>request headers</summary>{_headers(entry.request_headers, timeline.redact)}</details>"
        f"{payload}"
        f"<details><summary>response headers</summary>{_headers(entry.response_headers, timeline.redact)}</details>"
        f"<details open><summary>response body</summary>{_body_block(entry)}</details>"
        "</div>"
    )


def _act(action, timeline: Timeline) -> str:
    """One call the driver made.

    A bar rather than a mark, because the duration is the whole point: an
    action that took four seconds spent them waiting, and the row says so
    without anybody having to reason from the frames either side of it.
    """
    left = timeline.at(action.at) / timeline.span * 100
    width = max(action.took / timeline.span * 100, MIN_BAR)
    outcome = "failed" if action.failure else "ok"
    # The exception's first line only. The rest is a Python traceback's worth of
    # detail in a column 200 pixels wide, and the full text is on the title.
    said = action.failure.splitlines()[0] if action.failure else ""
    text = f"{action.target} — {said}" if said else action.target
    return (
        f'<div class="row act {outcome}" data-at="{timeline.at(action.at):.4f}" tabindex="0">'
        f"{_mark(action.tab, timeline)}"
        f'<span class="method">{html.escape(action.name)}</span>'
        f'<span class="text" title="{html.escape(action.failure or action.target)}">{html.escape(text)}</span>'
        f'<span class="took">{action.took * 1000:.0f} ms</span>'
        f'<span class="track"><i class="body" style="left:{left:.3f}%;width:{width:.3f}%"></i></span>'
        "</div>"
    )


def _log(message, timeline: Timeline) -> str:
    where = f"{message.url}:{message.line}" if message.url and message.line else message.url
    return (
        f'<div class="row log {html.escape(message.level)}" data-at="{timeline.at(message.at):.4f}" tabindex="0">'
        f"{_mark(message.tab, timeline)}"
        f'<span class="method">{html.escape(message.level)}</span>'
        f'<span class="text">{html.escape(message.text)}</span>'
        f'<span class="took">{html.escape(_short(where))}</span>'
        f'<span class="track"><i class="mark" style="left:{timeline.at(message.at) / timeline.span * 100:.3f}%"></i>'
        "</span>"
        "</div>"
    )


def _band(status: int | None) -> str:
    if status is None:
        return "pending"
    if status >= 500:
        return "server-error"
    if status >= 400:
        return "client-error"
    if status >= 300:
        return "redirect"
    return "ok"


def _short(url: str, limit: int = 60) -> str:
    """A URL as a column of them is worth reading.

    The scheme goes, because every row has the same one and it costs eight
    characters of the only column that distinguishes the rows; a long URL then
    keeps its **tail**, which is the identifying part. The full one is on the
    row's ``title``, so nothing is lost. Measured against the fixture site, the
    difference is between ``http://127.0.0.1:37171/…`` on every row and
    ``127.0.0.1:37171/api``.
    """
    bare = url.removeprefix("https://").removeprefix("http://")
    if len(bare) <= limit:
        return bare
    return "…" + bare[-(limit - 1) :]


def _embed(value: object) -> str:
    """JSON for a ``<script>`` block.

    ``</script>`` inside a JavaScript string literal still ends the tag: the
    HTML tokenizer does not know it is in a string. A recorded URL is the one
    piece of this document that comes from outside the test, and the failure
    mode is a blank page rather than an error, so the escape is not optional.
    ``<!--`` gets the same treatment for the same reason.
    """
    return json.dumps(value, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")


_EMPTY_ROWS = '<div class="row empty"><span class="text">no requests were recorded</span></div>'
_EMPTY_ACTS = '<div class="row empty"><span class="text">no actions were recorded</span></div>'

_DOCUMENT = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>wirespec — {title}</title>
<style>
  :root {{
    --bg: #14161a; --panel: #1b1e24; --line: #2a2f38; --ink: #e6e8ec; --dim: #949aa6;
    --ok: #3fb950; --redirect: #d29922; --client: #db6d28; --server: #f85149; --failed: #f85149;
    --wait: #3a4351; --mark: #58a6ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
         font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  header {{ padding: 12px 16px; border-bottom: 1px solid var(--line); }}
  h1 {{ font-size: 14px; margin: 0 0 2px; font-weight: 600; }}
  .meta {{ color: var(--dim); font-size: 12px; }}
  .tabs {{ margin-top: 2px; }}
  /* The badge sits in the row's own padding rather than taking a column: a
     column would cost every row eighteen pixels of the only part that
     distinguishes them, on the recordings that have one tab and do not need
     it at all. */
  .row {{ position: relative; }}
  .row > .tab {{ position: absolute; left: -14px; top: 3px; width: 12px; text-align: right;
                 color: var(--dim); font-size: 10px; }}
  .rows {{ padding-left: 16px; }}
  .which {{ color: var(--dim); font-size: 11px; margin-top: 6px; min-height: 1.4em; }}
  .note {{ margin: 8px 0 0; padding: 6px 10px; border-left: 3px solid var(--redirect);
           background: #241f14; color: #e3c07b; font-size: 12px; }}
  /* The stage gets the whole width and the timeline sits under it. A 1280x720
     frame in a 36% column is 460px wide, which is where this started and is too
     small to read the page under test -- and the waterfall was no better off
     for the space it took. */
  main {{ display: block; }}
  .screen {{ padding: 16px 16px 0; }}
  .stage {{ background: #000; border: 1px solid var(--line); display: grid; place-items: center;
            min-height: 180px; overflow: hidden; }}
  .stage.empty {{ color: var(--dim); padding: 40px 16px; text-align: center; }}
  /* Capped by height rather than width: the frame should get every pixel the
     window is wide, and still leave the waterfall on the same screen. */
  .stage img {{ display: block; max-width: 100%; max-height: 68vh; height: auto; }}
  .controls {{ display: flex; gap: 10px; align-items: center; margin-top: 10px; }}
  .controls input {{ flex: 1; }}
  button {{ background: var(--panel); color: var(--ink); border: 1px solid var(--line);
            padding: 3px 12px; cursor: pointer; font: inherit; }}
  output {{ color: var(--dim); min-width: 70px; text-align: right; }}
  .timeline {{ padding: 16px 16px 40px; min-width: 0; }}
  h2 {{ font-size: 12px; color: var(--dim); margin: 0 0 8px; font-weight: 600;
        text-transform: uppercase; letter-spacing: .06em; }}
  h2:not(:first-child) {{ margin-top: 22px; }}
  .rows {{ position: relative; }}
  /* Rows on the left, the selected request's detail on the right. The drawer
     is a column rather than a panel under the rows so that clicking around the
     waterfall does not move the row under the pointer. */
  .wire {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(300px, 34%); gap: 16px;
           align-items: start; }}
  @media (max-width: 1100px) {{ .wire {{ grid-template-columns: 1fr; }} }}
  .drawer {{ position: sticky; top: 8px; border: 1px solid var(--line); background: var(--panel);
             max-height: 78vh; overflow: auto; padding: 10px 12px; }}
  .drawer .hint {{ color: var(--dim); }}
  .pane {{ display: none; }}
  .pane:target, .pane.on {{ display: block; }}
  .pane h3 {{ font-size: 12px; margin: 0 0 6px; font-weight: 600; word-break: break-all; }}
  .pane .status {{ color: var(--dim); font-weight: 400; }}
  .pane details {{ border-top: 1px solid var(--line); padding: 6px 0; }}
  .pane details > summary {{ cursor: pointer; color: var(--dim); font-size: 11px;
                             text-transform: uppercase; letter-spacing: .06em; }}
  .pane dl {{ margin: 6px 0 0; display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 2px 10px; }}
  .pane dt {{ color: var(--dim); word-break: break-all; }}
  .pane dd {{ margin: 0; word-break: break-all; }}
  .pane pre {{ margin: 6px 0 0; padding: 8px; background: var(--bg); border: 1px solid var(--line);
               max-height: 320px; overflow: auto; white-space: pre-wrap; word-break: break-word; }}
  .pane .absent {{ color: var(--dim); margin: 6px 0 0; }}
  .row.picked {{ background: #22303f; }}
  .row {{ display: grid; grid-template-columns: 62px minmax(0, 1fr) 58px 104px 56px 38%;
          gap: 8px; align-items: center; padding: 3px 4px; border-radius: 3px; cursor: pointer; }}
  /* The console rows carry the same grid so their marks line up with the bars
     above them. Two axes drawn a few pixels apart is worse than one. */
  .row.log .text, .row.act .text {{ grid-column: 2 / 5; }}
  .row.act .method {{ color: var(--mark); }}
  .row.act.failed .method, .row.act.failed .text {{ color: var(--server); }}
  .row:hover {{ background: var(--panel); }}
  .row:focus {{ outline: 1px solid var(--mark); }}
  .row.empty {{ color: var(--dim); cursor: default; }}
  .method {{ color: var(--dim); }}
  .url, .text {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .kind, .took {{ color: var(--dim); font-size: 11px; }}
  .detail {{ font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .ok .detail {{ color: var(--ok); }}
  .redirect .detail {{ color: var(--redirect); }}
  .client-error .detail {{ color: var(--client); }}
  .server-error .detail, .failed .detail {{ color: var(--server); }}
  .track {{ position: relative; height: 12px; background: #12141880; border-radius: 2px; }}
  .track i {{ position: absolute; top: 2px; height: 8px; min-width: 1px; border-radius: 2px; }}
  .wait {{ background: var(--wait); }}
  .body {{ background: var(--ok); }}
  .redirect .body {{ background: var(--redirect); }}
  .client-error .body {{ background: var(--client); }}
  .server-error .body, .failed .body {{ background: var(--server); }}
  .failed .wait {{ background: #4a2222; }}
  .mark {{ width: 3px; top: 0; height: 12px; background: var(--mark); }}
  .log.error .mark, .log.error .method {{ background: none; color: var(--server); }}
  .log.error .mark {{ background: var(--server); }}
  .log.warning .mark {{ background: var(--redirect); }}
  .log.warning .method {{ color: var(--redirect); }}
  #playhead {{ position: absolute; top: 0; bottom: 0; width: 1px; background: var(--mark);
               pointer-events: none; opacity: .8; }}
  /* The axis needs numbers on it. A bar "a third of the way across" says
     nothing without them, and the whole claim of this page is that the frame
     and the request can be read together. */
  .ruler {{ cursor: default; color: var(--dim); font-size: 11px; }}
  .ruler:hover {{ background: none; }}
  .ruler .track {{ background: none; border-top: 1px solid var(--line); height: 14px; }}
  .ruler b {{ position: absolute; top: 1px; font-weight: normal; transform: translateX(-50%); white-space: nowrap; }}
  .ruler b:first-child {{ transform: none; }}
  .ruler b:last-child {{ transform: translateX(-100%); }}
</style>
<header>
  <h1>{title}</h1>
  <div class="meta">{started} · {summary}</div>
  {tabs}
  {note}
</header>
<main>
  <section class="screen">{stage}</section>
  <section class="timeline">
    <h2>actions</h2>
    <div class="rows" id="actions">
{acts}
    </div>
    <h2>network</h2>
    <div class="wire">
      <div class="rows" id="network"><div id="playhead" hidden></div>
{ruler}
{rows}
      </div>
      <aside class="drawer" id="drawer">
        <p class="hint" id="hint">select a request to see its headers and body</p>
{details}
      </aside>
    </div>
    <h2>console</h2>
    <div class="rows" id="console">
{logs}
    </div>
  </section>
</main>
<script>
// Seeking, playing, and the playhead. Everything that could be laid out in
// Python already was: this file is read by a person, and the less of it that
// only exists at runtime the better.
const FRAMES = {frames};
const SPAN = {span};
const shot = document.getElementById("shot");
const scrub = document.getElementById("scrub");
const clock = document.getElementById("clock");
const playhead = document.getElementById("playhead");
// Only present, and only filled, when the recording followed more than one tab.
const which = document.getElementById("which");
const play = document.getElementById("play");
let timer = null;

function show(index) {{
  if (!FRAMES.length) return;
  const i = Math.max(0, Math.min(FRAMES.length - 1, index));
  shot.src = "data:image/jpeg;base64," + FRAMES[i][2];
  scrub.value = i;
  clock.textContent = FRAMES[i][1];
  // Which tab this frame came from. A single-tab recording has no fourth
  // field, and the caption stays empty rather than saying "tab 1" on a page
  // where there is nothing else it could be.
  if (which) which.textContent = FRAMES[i][3] || "";
  const track = document.querySelector(".track");
  if (track && playhead) {{
    // The bars are positioned as a percentage of one track, so the playhead
    // has to be measured against a track rather than against the row.
    const box = track.getBoundingClientRect();
    const rows = document.getElementById("network").getBoundingClientRect();
    playhead.hidden = false;
    playhead.style.left = (box.left - rows.left + box.width * (FRAMES[i][0] / SPAN)) + "px";
  }}
}}

function seek(seconds) {{
  // The nearest frame, not the next one: a request that failed is followed by
  // whatever the page did about it, and rounding forward hides the moment.
  let best = 0;
  for (let i = 1; i < FRAMES.length; i++) {{
    if (Math.abs(FRAMES[i][0] - seconds) < Math.abs(FRAMES[best][0] - seconds)) best = i;
  }}
  show(best);
}}

function stop() {{ if (timer) {{ clearTimeout(timer); timer = null; }} if (play) play.textContent = "play"; }}

function step() {{
  const i = Number(scrub.value);
  if (i >= FRAMES.length - 1) {{ stop(); return; }}
  // Real time, from the frames' own timestamps, so playback runs at the speed
  // the page actually ran at rather than at whatever the frame count implies.
  const gap = Math.min(Math.max((FRAMES[i + 1][0] - FRAMES[i][0]) * 1000, 8), 2000);
  timer = setTimeout(() => {{ show(i + 1); step(); }}, gap);
}}

if (scrub) {{
  scrub.addEventListener("input", () => {{ stop(); show(Number(scrub.value)); }});
  play.addEventListener("click", () => {{
    if (timer) return stop();
    if (Number(scrub.value) >= FRAMES.length - 1) show(0);
    play.textContent = "stop";
    step();
  }});
  document.addEventListener("keydown", (event) => {{
    if (event.key === "ArrowRight") {{ stop(); show(Number(scrub.value) + 1); }}
    else if (event.key === "ArrowLeft") {{ stop(); show(Number(scrub.value) - 1); }}
    else return;
    event.preventDefault();
  }});
}}

// Which request's pane the drawer is showing. Rendered server-side, so this
// only ever toggles a class -- the panes are `:target`-addressable too, which
// is what makes the drawer work with the script disabled.
const hint = document.getElementById("hint");

function reveal(row) {{
  const id = row.getAttribute("data-pane");
  if (!id) return;
  for (const open of document.querySelectorAll(".pane.on")) open.classList.remove("on");
  for (const was of document.querySelectorAll(".row.picked")) was.classList.remove("picked");
  const pane = document.getElementById(id);
  if (pane) pane.classList.add("on");
  row.classList.add("picked");
  if (hint) hint.hidden = true;
}}

for (const row of document.querySelectorAll(".row[data-at]")) {{
  const at = Number(row.dataset.at);
  row.addEventListener("click", () => {{ stop(); seek(at); reveal(row); }});
  row.addEventListener("keydown", (event) => {{
    if (event.key === "Enter") {{ stop(); seek(at); reveal(row); }}
  }});
}}

show(FRAMES.length - 1);
</script>
</html>
"""
