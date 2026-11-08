"""The page a signed approval link opens (FR-HUM-07).

Opening it decides nothing: it shows what is asked and a button, and only the button (a POST)
answers. The page loads nothing from anywhere, runs no script, can't be framed, and tells the
browser not to keep it or to say where it came from.
"""

from __future__ import annotations

import json
from html import escape

from fastapi.responses import HTMLResponse

from aios.core.human import Approval
from aios.human import ActionLink

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

STYLE = """
:root { --bg: #f7f7f5; --fg: #1d1d1b; --muted: #5f5f5a; --card: #fff; --line: #deded9;
  --yes: #1f6f43; --no: #a3312a; --warn: #8a5a00; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161615; --fg: #ececea; --muted: #a5a5a0; --card: #20201f; --line: #353533;
    --yes: #5bbf86; --no: #ef7d74; --warn: #e3b25a; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 640px; margin: 0 auto; padding: 32px 16px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 20px; }
h1 { font-size: 1.35rem; margin: 0 0 12px; overflow-wrap: anywhere; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 16px 0; }
dt { color: var(--muted); } dd { margin: 0; overflow-wrap: anywhere; }
pre { background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 10px;
  overflow-x: auto; font-size: .875rem; white-space: pre-wrap; overflow-wrap: anywhere; }
.warn { color: var(--warn); } .muted { color: var(--muted); font-size: .9rem; }
textarea { width: 100%; min-height: 72px; font: inherit; padding: 8px; color: var(--fg);
  background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
button { margin-top: 12px; font: inherit; font-weight: 600; color: #fff; border: 0;
  border-radius: 6px; padding: 10px 18px; cursor: pointer; }
.yes { background: var(--yes); } .no { background: var(--no); }
@media (prefers-color-scheme: dark) { button { color: #111; } }
"""


def page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head>"
        f'<body><main><div class="card">{body}</div></main></body></html>'
    )
    return HTMLResponse(html, status_code=status, headers=HEADERS)


def message(title: str, text: str, *, status: int = 200) -> HTMLResponse:
    return page(title, f"<h1>{escape(title)}</h1><p>{escape(text)}</p>", status=status)


def _shown(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)
    return f"<pre>{escape(text)}</pre>"


def confirmation(link: ActionLink, approval: Approval) -> HTMLResponse:
    """What is asked, and one button: approve or deny, as the link says."""
    details = approval.details
    verdict = "Approve" if link.approve else "Deny"
    action = details.action or "this action"
    title = f"{verdict} {action}?"
    if approval.status != "pending":
        return message(title, f"This approval is already {approval.status}. Nothing more to do.")
    if link.principal in approval.voted:
        left = approval.quorum - len(approval.votes)
        return message(title, f"You have approved this already. It waits for {left} more.")
    rows = [
        ("App", approval.app),
        ("Risk", details.risk),
        ("Expires", f"{details.expires_at:%Y-%m-%d %H:%M} UTC"),
        ("You answer as", link.principal),
    ]
    if details.quorum > 1:
        rows.insert(2, ("Approvers", f"{len(approval.votes)} of {details.quorum} so far"))
    parts = [
        f"<h1>{escape(title)}</h1>",
        f"<p>{escape(details.summary)}</p>",
        "<dl>",
        *(f"<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>" for k, v in rows),
        "</dl>",
    ]
    if details.tainted:
        parts.append(
            '<p class="warn">The agent had read content from outside before it asked. '
            "Check the details yourself.</p>"
        )
    if details.preview is not None:
        parts += ["<p><strong>What the tool says it would do</strong></p>", _shown(details.preview)]
    if details.payload is not None:
        parts += ["<p><strong>Exactly what is asked</strong></p>", _shown(details.payload)]
    if details.rationale:
        parts += ["<p><strong>The agent's reason, in its own words</strong></p>"]
        parts.append(_shown(details.rationale))
    css = "yes" if link.approve else "no"
    parts += [
        '<form method="post">',
        '<label for="comment" class="muted">A comment, if you like</label>',
        '<textarea id="comment" name="comment" maxlength="2000"></textarea>',
        f'<button type="submit" class="{css}">{verdict}</button>',
        "</form>",
        '<p class="muted">This link is for you alone, and works until the approval expires.</p>',
    ]
    return page(title, "".join(parts))


def answered(link: ActionLink, approval: Approval) -> HTMLResponse:
    if approval.status == "pending":
        left = approval.quorum - len(approval.votes)
        return message("Your yes is counted", f"It waits for {left} more approver(s).")
    if approval.status == "granted":
        return message("Approved", "Thank you. The run goes on. You can close this page.")
    return message("Denied", "The action won't happen. You can close this page.")
