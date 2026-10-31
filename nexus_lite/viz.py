"""
nexus_lite.viz — Live HTML5 and Colab visualization dashboard for nexus-lite.

Features:
- Self-contained HTML5 dashboard (zero external JS/CSS CDN dependencies).
- Inline SVG multi-agent topology graph with nodes (orchestrator, PTY sessions, workers)
  and directed edges for task delegations and agent-bus messages.
- Live PTY terminal grid with dark-themed terminal screens (<pre>).
- Task execution board with RUNNING/COMPLETED/FAILED badges, durations, caller->worker,
  and collapsible <details> with output.md artifacts.
- Agent-bus message timeline with sender -> target, kind badges, and pointer traffic.
- Colab & Jupyter interactive display (dashboard, watch) with auto-refresh via clear_output.
- CLI subcommand: nexus-lite dashboard [--out FILE].
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional, Union

from . import core, pty


def render_dashboard_html() -> str:
    """
    Query SQLite and live PTY sessions to build a rich, self-contained HTML5 dashboard.
    Zero external JS/CSS dependencies: renders cleanly inside Google Colab output cells.
    """
    core.init()
    ws = core.get_workspace()

    # Query SQLite database
    try:
        core.reap_stale_tasks()
    except Exception:
        pass
    with core.get_db() as con:
        try:
            tasks_rows = con.execute(
                """
                SELECT id, caller, agent_cmd, status, rc, created_at, completed_at, artifact_dir
                FROM tasks
                ORDER BY created_at DESC
                LIMIT 50;
                """
            ).fetchall()
            tasks = [dict(r) for r in tasks_rows]
        except sqlite3.OperationalError:
            tasks = []

        try:
            messages_rows = con.execute(
                """
                SELECT id, sender, target, kind, summary, path, content, timestamp
                FROM messages
                ORDER BY timestamp DESC
                LIMIT 50;
                """
            ).fetchall()
            messages = [dict(r) for r in messages_rows]
        except sqlite3.OperationalError:
            messages = []

    # Live PTY sessions
    try:
        live_sessions = pty.list_sessions()
    except Exception:
        live_sessions = []

    # Wiki documents
    try:
        wiki_docs = core.wiki_list()
    except Exception:
        wiki_docs = []

    # KPI counts
    live_ptys_count = len(live_sessions)
    tasks_running = sum(1 for t in tasks if t.get("status") == "running")
    tasks_completed = sum(1 for t in tasks if t.get("status") == "completed")
    tasks_failed = sum(1 for t in tasks if t.get("status") == "failed")
    messages_count = len(messages)
    wiki_count = len(wiki_docs)

    # Read live PTY screens
    pty_screens: list[dict] = []
    for s in live_sessions:
        name = s["name"]
        sock = s.get("sock", "")
        try:
            screen_text = pty.read(name, cols=100, rows=25, settle=0.0)
        except Exception as e:
            screen_text = f"Error reading screen: {e}"
        pty_screens.append({
            "name": name,
            "sock": sock,
            "screen": screen_text or "(screen empty / waiting for input)",
        })

    # Prepare SVG topology graph (Agents/Peers + Tasks/Workers + A2A DM/Handoff/Escalation edges)
    left_nodes: list[dict] = []
    seen_left: set[str] = set()

    for s in live_sessions:
        s_name = s["name"]
        if s_name not in seen_left:
            seen_left.add(s_name)
            left_nodes.append({
                "id": s_name,
                "label": s_name,
                "type": "pty",
                "badge": "PTY ACTIVE",
            })

    for t in tasks:
        caller = t.get("caller") or "main"
        if caller not in seen_left:
            seen_left.add(caller)
            badge = "PEER AGENT" if any(k in caller.lower() for k in ("agy", "opencode", "claude", "coder", "reviewer", "architect", "driver", "navigator", "verifier")) else "ORCHESTRATOR"
            left_nodes.append({
                "id": caller,
                "label": caller,
                "type": "agent" if badge == "PEER AGENT" else "caller",
                "badge": badge,
            })

    recent_tasks = tasks[:12]
    task_id_set = {t["id"] for t in recent_tasks}

    # Also discover peer agents from message bus (DM / handoff senders & targets)
    recent_msgs = messages[:20]
    for m in recent_msgs:
        for party in (m.get("sender") or "", m.get("target") or ""):
            if not party or party.upper() == "ALL" or party in task_id_set:
                continue
            if party not in seen_left:
                seen_left.add(party)
                left_nodes.append({
                    "id": party,
                    "label": party,
                    "type": "agent",
                    "badge": "PEER AGENT",
                })

    if not left_nodes:
        left_nodes.append({
            "id": "orchestrator",
            "label": "orchestrator",
            "type": "caller",
            "badge": "STANDBY",
        })

    right_nodes: list[dict] = []
    for t in recent_tasks:
        right_nodes.append({
            "id": t["id"],
            "label": t["id"],
            "cmd": t.get("agent_cmd", ""),
            "status": t.get("status", "running"),
        })

    node_w = 175
    node_h = 42
    spacing_y = 64
    margin_top = 60
    x_left = 175
    x_right = 665

    left_y = {n["id"]: margin_top + i * spacing_y for i, n in enumerate(left_nodes)}
    right_y = {n["id"]: margin_top + i * spacing_y for i, n in enumerate(right_nodes)}
    svg_height = max(280, margin_top + max(len(left_nodes), len(right_nodes), 1) * spacing_y + 40)
    svg_width = 860

    edges_svg: list[str] = []

    # 1. Delegation edges (solid, status-colored: Agent/Caller -> Task)
    for t in recent_tasks:
        caller = t.get("caller") or "main"
        tid = t["id"]
        status = t.get("status", "running")
        if caller in left_y and tid in right_y:
            y1 = left_y[caller]
            y2 = right_y[tid]
            x1 = x_left + (node_w // 2)
            x2 = x_right - (node_w // 2)
            cx1 = (x1 + x2) // 2
            if status == "completed":
                color = "#3fb950"
                marker = "url(#arrow-completed)"
            elif status == "escalated":
                color = "#bc8cff"
                marker = "url(#arrow-handoff)"
            elif status == "failed":
                color = "#f85149"
                marker = "url(#arrow-failed)"
            else:
                color = "#d29922"
                marker = "url(#arrow-running)"
            edges_svg.append(
                f'<path d="M {x1} {y1} C {cx1} {y1}, {cx1} {y2}, {x2} {y2}" '
                f'fill="none" stroke="{color}" stroke-width="1.8" marker-end="{marker}" opacity="0.85"/>'
            )

    # 2. Agent-to-Agent (A2A) & Task-to-Task Bus Message Edges (dashed, labeled)
    a2a_pair_counts: dict[tuple[str, str], int] = {}
    for m in recent_msgs:
        sender = m.get("sender", "")
        target = m.get("target", "")
        kind = m.get("kind", "dm")
        mid = m.get("id", "")
        summary_txt = m.get("summary", "")
        is_escalate = "TASK_ESCALATE" in summary_txt
        color = "#f0883e" if is_escalate else ("#bc8cff" if kind in ("handoff", "broadcast") else "#58a6ff")
        marker = "url(#arrow-handoff)" if (kind in ("handoff", "broadcast") or is_escalate) else "url(#arrow-bus)"

        # Case A: Peer-to-Peer Agent interaction (left column <-> left column)
        if sender in left_y and target in left_y and sender != target:
            pair_idx = a2a_pair_counts.get((sender, target), 0)
            a2a_pair_counts[(sender, target)] = pair_idx + 1
            y1 = left_y[sender]
            y2 = left_y[target]
            x_arc = x_left - (node_w // 2)
            bulge = 55 + (pair_idx * 18)
            cx = max(12, x_arc - bulge)
            mid_y = (y1 + y2) // 2
            lbl_tag = html.escape(f"{kind}:{mid}"[:14])
            edges_svg.append(
                f'<path d="M {x_arc} {y1} C {cx} {y1}, {cx} {y2}, {x_arc} {y2}" '
                f'fill="none" stroke="{color}" stroke-width="2.0" stroke-dasharray="5,3" marker-end="{marker}" opacity="0.95"/>'
                f'<text x="{cx + 6}" y="{mid_y - 4}" fill="{color}" font-size="8.5" font-family="monospace" font-weight="600">{lbl_tag}</text>'
            )
        # Case B: Stage-to-stage right column handoffs (task -> task)
        elif sender in right_y and target in right_y and sender != target:
            y1 = right_y[sender]
            y2 = right_y[target]
            x_edge = x_right + (node_w // 2)
            cx = x_edge + 50
            edges_svg.append(
                f'<path d="M {x_edge} {y1} C {cx} {y1}, {cx} {y2}, {x_edge} {y2}" '
                f'fill="none" stroke="{color}" stroke-width="1.6" stroke-dasharray="4,4" marker-end="{marker}" opacity="0.9"/>'
            )
        # Case C: Worker task -> Agent/Caller notification or escalation
        elif sender in right_y and target in left_y:
            y1 = right_y[sender]
            y2 = left_y[target]
            x1 = x_right - (node_w // 2)
            x2 = x_left + (node_w // 2)
            cx1 = (x1 + x2) // 2
            edges_svg.append(
                f'<path d="M {x1} {y1} C {cx1} {y1}, {cx1} {y2}, {x2} {y2}" '
                f'fill="none" stroke="{color}" stroke-width="1.5" stroke-dasharray="4,4" marker-end="{marker}" opacity="0.75"/>'
            )
        # Case D: Agent/Caller -> Worker task message
        elif sender in left_y and target in right_y:
            y1 = left_y[sender]
            y2 = right_y[target]
            x1 = x_left + (node_w // 2)
            x2 = x_right - (node_w // 2)
            cx1 = (x1 + x2) // 2
            edges_svg.append(
                f'<path d="M {x1} {y1} C {cx1} {y1}, {cx1} {y2}, {x2} {y2}" '
                f'fill="none" stroke="{color}" stroke-width="1.5" stroke-dasharray="4,4" marker-end="{marker}" opacity="0.75"/>'
            )

    nodes_svg: list[str] = []
    # Render left nodes (Peer Agents, Orchestrators, Live PTYs)
    for n in left_nodes:
        x = x_left
        y = left_y[n["id"]]
        if n["type"] == "pty":
            badge_color = "#58a6ff"
            border_color = "#388bfd"
        elif n["type"] == "agent":
            badge_color = "#bc8cff"
            border_color = "#8957e5"
        else:
            badge_color = "#8b949e"
            border_color = "#30363d"
        lbl = html.escape(n["label"][:20])
        bdg = html.escape(n["badge"])
        nodes_svg.append(
            f'<g class="node">'
            f'<rect x="{x - node_w // 2}" y="{y - node_h // 2}" width="{node_w}" height="{node_h}" rx="6" '
            f'fill="#161b22" stroke="{border_color}" stroke-width="1.6"/>'
            f'<text x="{x}" y="{y - 3}" fill="#f0f6fc" font-size="11" font-weight="600" text-anchor="middle" font-family="monospace">{lbl}</text>'
            f'<text x="{x}" y="{y + 12}" fill="{badge_color}" font-size="9" font-weight="600" text-anchor="middle" font-family="sans-serif">{bdg}</text>'
            f'</g>'
        )

    # Render right nodes (tasks/workers)
    for n in right_nodes:
        x = x_right
        y = right_y[n["id"]]
        status = n["status"]
        if status == "completed":
            st_color = "#3fb950"
            border_color = "#238636"
        elif status == "escalated":
            st_color = "#bc8cff"
            border_color = "#8957e5"
        elif status == "failed":
            st_color = "#f85149"
            border_color = "#da3633"
        else:
            st_color = "#d29922"
            border_color = "#9e6a03"

        lbl = html.escape(n["label"][:20])
        cmd_trunc = html.escape(n["cmd"][:22]) if n["cmd"] else status.upper()
        nodes_svg.append(
            f'<g class="node">'
            f'<rect x="{x - node_w // 2}" y="{y - node_h // 2}" width="{node_w}" height="{node_h}" rx="6" '
            f'fill="#161b22" stroke="{border_color}" stroke-width="1.6"/>'
            f'<text x="{x}" y="{y - 3}" fill="#f0f6fc" font-size="11" font-weight="600" text-anchor="middle" font-family="monospace">{lbl}</text>'
            f'<text x="{x}" y="{y + 12}" fill="{st_color}" font-size="9" font-weight="600" text-anchor="middle" font-family="monospace">{cmd_trunc}</text>'
            f'</g>'
        )

    svg_content = f"""<svg class="topology-svg" viewBox="0 0 {svg_width} {svg_height}" width="{svg_width}" height="{svg_height}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="arrow-completed" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#3fb950" />
    </marker>
    <marker id="arrow-running" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#d29922" />
    </marker>
    <marker id="arrow-failed" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#f85149" />
    </marker>
    <marker id="arrow-bus" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#58a6ff" />
    </marker>
    <marker id="arrow-handoff" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#bc8cff" />
    </marker>
  </defs>

  <!-- Legend -->
  <g font-size="10" font-family="sans-serif">
    <text x="20" y="24" fill="#8b949e" font-weight="600">ORCHESTRATORS &amp; PEER AGENTS</text>
    <text x="{x_right - 65}" y="24" fill="#8b949e" font-weight="600">WORKERS &amp; TASKS</text>
    <line x1="210" y1="20" x2="236" y2="20" stroke="#3fb950" stroke-width="2"/>
    <text x="241" y="24" fill="#8b949e">Done</text>
    <line x1="280" y1="20" x2="306" y2="20" stroke="#58a6ff" stroke-width="2" stroke-dasharray="4,2"/>
    <text x="311" y="24" fill="#58a6ff">A2A DM</text>
    <line x1="365" y1="20" x2="391" y2="20" stroke="#bc8cff" stroke-width="2" stroke-dasharray="3,3"/>
    <text x="396" y="24" fill="#bc8cff">Handoff / Escalate</text>
  </g>

  <!-- Edges -->
  {''.join(edges_svg)}

  <!-- Nodes -->
  {''.join(nodes_svg)}
</svg>"""

    # Format Tasks Execution Board HTML
    task_rows_html: list[str] = []
    for t in tasks:
        tid = html.escape(t["id"])
        caller = html.escape(t.get("caller") or "main")
        cmd = html.escape(t.get("agent_cmd") or "")
        st = t.get("status", "running")
        st_badge = f'<span class="badge badge-{st}">{st.upper()}</span>'

        t0 = t.get("created_at")
        t1 = t.get("completed_at")
        if t0 and t1:
            dur = f"{t1 - t0:.2f}s"
        elif t0:
            dur = f"{time.time() - t0:.1f}s (running)"
        else:
            dur = "-"

        out_content = ""
        art_dir = t.get("artifact_dir")
        if art_dir:
            out_file = Path(art_dir) / "output.md"
            if out_file.exists():
                try:
                    out_content = out_file.read_text(encoding="utf-8")
                except Exception:
                    pass

        details_html = ""
        if out_content:
            details_html = (
                f'<details><summary>output.md ({len(out_content)} bytes)</summary>'
                f'<pre>{html.escape(out_content)}</pre></details>'
            )
        else:
            details_html = f'<span style="color:#8b949e;font-size:11px;">rc={t.get("rc", "-")}</span>'

        task_rows_html.append(
            f"<tr>"
            f"<td><code>{tid}</code></td>"
            f"<td>{st_badge}</td>"
            f"<td><code>{cmd}</code></td>"
            f"<td>{caller}</td>"
            f"<td>{dur}</td>"
            f"<td>{details_html}</td>"
            f"</tr>"
        )

    tasks_table_body = (
        "".join(task_rows_html)
        if task_rows_html
        else '<tr><td colspan="6" class="empty-state">No tasks executed yet</td></tr>'
    )

    # Format Live PTY Terminal Grid
    pty_grid_html: list[str] = []
    for ps in pty_screens:
        p_name = html.escape(ps["name"])
        p_sock = html.escape(ps["sock"])
        p_screen = html.escape(ps["screen"])
        pty_grid_html.append(
            f'<div class="terminal-card">'
            f'<div class="terminal-header">'
            f'<span>session: <strong>{p_name}</strong></span>'
            f'<span class="badge badge-completed">ONLINE</span>'
            f'</div>'
            f'<pre class="terminal-body">{p_screen}</pre>'
            f'</div>'
        )

    pty_grid_content = (
        "".join(pty_grid_html)
        if pty_grid_html
        else '<div class="empty-state">No active dtach PTY sessions currently running.</div>'
    )

    # Format Agent-Bus Message Timeline
    msg_rows_html: list[str] = []
    for m in messages:
        mid = html.escape(m["id"])
        sender = html.escape(m.get("sender") or "")
        target = html.escape(m.get("target") or "")
        kind = m.get("kind") or "dm"
        badge_cls = f"badge-{kind}" if kind in ("dm", "handoff", "task", "broadcast") else "badge-dm"
        kind_badge = f'<span class="badge {badge_cls}">{kind.upper()}</span>'
        ts_val = m.get("timestamp")
        time_str = time.strftime("%H:%M:%S", time.localtime(ts_val)) if ts_val else "-"
        summary = html.escape(m.get("summary") or "")
        content = m.get("content") or ""

        content_details = ""
        if content and content.strip() != m.get("summary", "").strip():
            content_details = (
                f'<details><summary>details</summary>'
                f'<pre>{html.escape(content)}</pre></details>'
            )

        msg_rows_html.append(
            f"<tr>"
            f"<td><code>{time_str}</code></td>"
            f"<td>{kind_badge}</td>"
            f"<td><code>{sender}</code> &rarr; <code>{target}</code></td>"
            f"<td>{summary} {content_details}</td>"
            f"</tr>"
        )

    msg_table_body = (
        "".join(msg_rows_html)
        if msg_rows_html
        else '<tr><td colspan="4" class="empty-state">No bus messages sent yet</td></tr>'
    )

    # Read .nexus/bridge.json if present for header links
    bridge_json_path = ws / "bridge.json"
    dashboard_url: Optional[str] = None
    colab_dashboard_url: Optional[str] = None
    if bridge_json_path.exists():
        try:
            bdata = json.loads(bridge_json_path.read_text(encoding="utf-8"))
            dashboard_url = bdata.get("dashboard_url")
            colab_dashboard_url = bdata.get("colab_dashboard_url")
        except Exception:
            pass

    header_buttons: list[str] = [
        '<button id="btn-colab-poll" onclick="toggleColabAutoPoll()" class="header-btn poll-btn" title="Toggle 3s live polling in Colab Fullscreen Output">↻ Auto-Poll (Colab Fullscreen)</button>'
    ]
    if dashboard_url:
        tunnel_url = f"{dashboard_url}&refresh=3" if "?" in dashboard_url else f"{dashboard_url}?refresh=3"
        header_buttons.append(
            f'<a href="{tunnel_url}" target="_blank" class="header-btn tunnel-btn">Open Live Dashboard (Cloudflare Tunnel) ↗</a>'
        )
    if colab_dashboard_url:
        header_buttons.append(
            f'<a href="{colab_dashboard_url}" target="_blank" class="header-btn colab-btn">Open Live Dashboard (Colab Proxy) ↗</a>'
        )
    header_buttons_html = "\n".join(header_buttons)

    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nexus-Lite Multi-Agent Dashboard</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    background-color: #0d1117;
    color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
    padding: 20px;
}}
.header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #30363d;
    padding-bottom: 12px;
    margin-bottom: 20px;
    flex-wrap: wrap;
    gap: 12px;
}}
.header h1 {{
    font-size: 18px;
    font-weight: 600;
    color: #f0f6fc;
}}
.header .timestamp {{
    font-size: 12px;
    color: #8b949e;
    font-family: monospace;
}}
.header-actions {{
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
}}
.header-btn {{
    display: inline-block;
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    text-decoration: none;
    color: #ffffff;
    background-color: #238636;
    border: 1px solid #2ea043;
    transition: background-color 0.2s;
}}
.header-btn:hover {{
    background-color: #2ea043;
}}
.header-btn.colab-btn {{
    background-color: #1f6feb;
    border: 1px solid #388bfd;
}}
.header-btn.colab-btn:hover {{
    background-color: #388bfd;
}}
.header-btn.poll-btn {{
    background-color: #21262d;
    border: 1px solid #30363d;
    color: #c9d1d9;
    cursor: pointer;
    font-family: inherit;
}}
.header-btn.poll-btn:hover {{
    background-color: #30363d;
    color: #f0f6fc;
    border-color: #8b949e;
}}
.header-btn.poll-btn.active {{
    background-color: #1f6feb;
    border: 1px solid #388bfd;
    color: #ffffff;
}}
/* 2x2 Responsive Widescreen Grid */
.dashboard-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    gap: 12px;
    margin-bottom: 12px;
}}
.scrollable-panel {{
    max-height: 280px;
    overflow-y: auto;
}}
/* KPI Bar */
.kpi-bar {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
}}
.kpi-card {{
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 12px 14px;
}}
.kpi-title {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #8b949e;
    margin-bottom: 4px;
}}
.kpi-value {{
    font-size: 22px;
    font-weight: 600;
    color: #f0f6fc;
}}
.kpi-value.green {{ color: #3fb950; }}
.kpi-value.amber {{ color: #d29922; }}
.kpi-value.red {{ color: #f85149; }}
.kpi-value.blue {{ color: #58a6ff; }}
.kpi-value.purple {{ color: #bc8cff; }}

/* Section Containers */
.section {{
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    margin-bottom: 20px;
    overflow: hidden;
}}
.section-header {{
    padding: 10px 16px;
    border-bottom: 1px solid #30363d;
    font-size: 13px;
    font-weight: 600;
    color: #f0f6fc;
    display: flex;
    justify-content: space-between;
    align-items: center;
}}
.section-body {{
    padding: 14px;
}}

/* SVG Topology */
.topology-container {{
    width: 100%;
    overflow-x: auto;
    text-align: center;
}}
.topology-svg {{
    max-width: 100%;
    height: auto;
    background-color: #090d13;
    border-radius: 6px;
}}

/* Live PTY Terminal Grid */
.terminal-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 14px;
}}
.terminal-card {{
    background-color: #090d13;
    border: 1px solid #30363d;
    border-radius: 6px;
    overflow: hidden;
}}
.terminal-header {{
    background-color: #161b22;
    padding: 8px 12px;
    border-bottom: 1px solid #30363d;
    font-size: 11px;
    font-family: monospace;
    color: #8b949e;
    display: flex;
    justify-content: space-between;
    align-items: center;
}}
.terminal-body {{
    padding: 12px;
    font-family: monospace;
    font-size: 11px;
    color: #c9d1d9;
    max-height: 220px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
    line-height: 1.4;
}}

/* Data Table */
.data-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
}}
.data-table th, .data-table td {{
    padding: 8px 12px;
    text-align: left;
    border-bottom: 1px solid #21262d;
}}
.data-table th {{
    background-color: #0d1117;
    color: #8b949e;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
}}
.data-table tr:hover {{
    background-color: #0d1117;
}}

/* Badges */
.badge {{
    display: inline-block;
    padding: 2px 6px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
}}
.badge-completed {{ background-color: rgba(35, 134, 54, 0.2); color: #3fb950; border: 1px solid #238636; }}
.badge-running {{ background-color: rgba(158, 106, 3, 0.2); color: #d29922; border: 1px solid #9e6a03; }}
.badge-failed {{ background-color: rgba(218, 54, 51, 0.2); color: #f85149; border: 1px solid #da3633; }}
.badge-dm {{ background-color: rgba(56, 139, 253, 0.2); color: #58a6ff; border: 1px solid #1f6feb; }}
.badge-handoff {{ background-color: rgba(163, 113, 247, 0.2); color: #bc8cff; border: 1px solid #8957e5; }}
.badge-task {{ background-color: rgba(46, 160, 67, 0.2); color: #3fb950; border: 1px solid #2ea043; }}
.badge-broadcast {{ background-color: rgba(210, 153, 34, 0.2); color: #d29922; border: 1px solid #d29922; }}

details summary {{
    cursor: pointer;
    color: #58a6ff;
    font-size: 11px;
}}
details pre {{
    margin-top: 6px;
    background-color: #090d13;
    padding: 8px;
    border-radius: 4px;
    font-size: 11px;
    color: #8b949e;
    max-height: 160px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
}}
.empty-state {{
    color: #8b949e;
    font-size: 12px;
    font-style: italic;
    padding: 14px;
    text-align: center;
}}
</style>
</head>
<body>

<div class="header">
    <div>
        <h1>Nexus-Lite Multi-Agent Dashboard</h1>
        <div class="timestamp">Updated: {now_str}</div>
    </div>
    <div class="header-actions">
        {header_buttons_html}
    </div>
</div>

<!-- 1. KPI Summary Bar -->
<div class="kpi-bar">
    <div class="kpi-card">
        <div class="kpi-title">Live PTYs</div>
        <div class="kpi-value blue">{live_ptys_count}</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-title">Tasks Running</div>
        <div class="kpi-value amber">{tasks_running}</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-title">Tasks Completed</div>
        <div class="kpi-value green">{tasks_completed}</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-title">Tasks Failed</div>
        <div class="kpi-value red">{tasks_failed}</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-title">Bus Messages</div>
        <div class="kpi-value purple">{messages_count}</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-title">Wiki Docs</div>
        <div class="kpi-value">{wiki_count}</div>
    </div>
</div>

<!-- Row 1: Topology Graph + Live PTY Terminal Grid (Compact 2x2 responsive grid) -->
<div class="dashboard-grid">
    <div class="section" style="margin-bottom:0;">
        <div class="section-header">
            <span>Multi-Agent Topology Graph</span>
            <span style="font-size:11px;color:#8b949e;">Live directed delegations &amp; bus messages</span>
        </div>
        <div class="section-body topology-container scrollable-panel" style="max-height: 280px; overflow-y: auto;">
            {svg_content}
        </div>
    </div>

    <div class="section" style="margin-bottom:0;">
        <div class="section-header">
            <span>Live PTY Terminal Grid</span>
            <span style="font-size:11px;color:#8b949e;">{live_ptys_count} Active Session(s)</span>
        </div>
        <div class="section-body scrollable-panel" style="max-height: 280px; overflow-y: auto;">
            <div class="terminal-grid">
                {pty_grid_content}
            </div>
        </div>
    </div>
</div>

<!-- Row 2: Task Execution Board + Agent-Bus Message Timeline -->
<div class="dashboard-grid">
    <div class="section" style="margin-bottom:0;">
        <div class="section-header">
            <span>Task Execution Board</span>
            <span style="font-size:11px;color:#8b949e;">Recent sub-agent delegations</span>
        </div>
        <div class="section-body scrollable-panel" style="padding:0;max-height: 280px; overflow-y: auto; overflow-x:auto;">
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Task ID</th>
                        <th>Status</th>
                        <th>Command</th>
                        <th>Caller</th>
                        <th>Duration</th>
                        <th>Artifacts</th>
                    </tr>
                </thead>
                <tbody>
                    {tasks_table_body}
                </tbody>
            </table>
        </div>
    </div>

    <div class="section" style="margin-bottom:0;">
        <div class="section-header">
            <span>Agent-Bus Message Timeline</span>
            <span style="font-size:11px;color:#8b949e;">[nexus] Pointer traffic &amp; handoffs</span>
        </div>
        <div class="section-body scrollable-panel" style="padding:0;max-height: 280px; overflow-y: auto; overflow-x:auto;">
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Kind</th>
                        <th>Route</th>
                        <th>Summary</th>
                    </tr>
                </thead>
                <tbody>
                    {msg_table_body}
                </tbody>
            </table>
        </div>
    </div>
</div>

<script>
window._colabPollActive = window._colabPollActive || false;
window._colabPollTimer = window._colabPollTimer || null;

function updateColabButtonState() {{
    var btn = document.getElementById("btn-colab-poll");
    if (btn) {{
        if (window._colabPollActive) {{
            btn.classList.add("active");
            btn.innerText = "✓ Auto-Polling (3s)...";
        }} else {{
            btn.classList.remove("active");
            btn.innerText = "↻ Auto-Poll (Colab Fullscreen)";
        }}
    }}
}}

function pollColabOnce() {{
    if (window.google && window.google.colab && window.google.colab.kernel && window.google.colab.kernel.invokeFunction) {{
        window.google.colab.kernel.invokeFunction('nexus_lite.poll_dashboard', [], {{}})
            .then(function(res) {{
                var newHtml = '';
                if (res && res.data) {{
                    if (res.data['application/json'] && res.data['application/json'].html) {{
                        newHtml = res.data['application/json'].html;
                    }} else if (res.data['text/html']) {{
                        newHtml = res.data['text/html'];
                    }} else if (typeof res.data === 'string') {{
                        newHtml = res.data;
                    }}
                }} else if (res && res.html) {{
                    newHtml = res.html;
                }}
                if (newHtml) {{
                    var parser = new DOMParser();
                    var doc = parser.parseFromString(newHtml, 'text/html');
                    if (doc && doc.body) {{
                        document.body.innerHTML = doc.body.innerHTML;
                        updateColabButtonState();
                    }}
                }}
            }})
            .catch(function(err) {{
                console.error("Colab poll error:", err);
            }});
    }}
}}

function toggleColabAutoPoll() {{
    if (!window.google || !window.google.colab || !window.google.colab.kernel) {{
        alert("Colab Auto-Poll is available in Google Colab output cells and Fullscreen Output Sharing mode.");
        return;
    }}
    window._colabPollActive = !window._colabPollActive;
    if (window._colabPollActive) {{
        pollColabOnce();
        if (window._colabPollTimer) clearInterval(window._colabPollTimer);
        window._colabPollTimer = setInterval(pollColabOnce, 3000);
    }} else {{
        if (window._colabPollTimer) {{
            clearInterval(window._colabPollTimer);
            window._colabPollTimer = null;
        }}
    }}
    updateColabButtonState();
}}

if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', updateColabButtonState);
}} else {{
    updateColabButtonState();
}}
</script>

</body>
</html>
"""
    return html_doc


def dashboard(
    out_path: Optional[Union[str, Path]] = None,
    display_inline: bool = True,
    return_html: bool = False,
):
    """
    Render and write HTML dashboard to .nexus/dashboard.html (or custom path).
    If in Jupyter/Colab and display_inline=True, displays IPython.display.HTML.
    When return_html=False (default) and IPython.display is used, returns None
    so Jupyter/Colab does not render the HTML a second time as cell Out[N].
    """
    # Register google.colab callback for live in-place polling in Colab Fullscreen Output
    try:
        import google.colab.output

        def _poll_dashboard_callback():
            h = render_dashboard_html()
            if hasattr(google.colab.output, "JSON"):
                return google.colab.output.JSON({"html": h})
            return {"html": h}

        google.colab.output.register_callback("nexus_lite.poll_dashboard", _poll_dashboard_callback)
    except (ImportError, Exception):
        pass

    html_content = render_dashboard_html()

    if out_path is None:
        ws = core.get_workspace()
        out_p = ws / "dashboard.html"
    else:
        out_p = Path(out_path).resolve()

    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(html_content, encoding="utf-8")

    if display_inline:
        try:
            import IPython.display as ipd

            h = ipd.HTML(html_content)
            ipd.display(h)
            if not return_html:
                return None
            return html_content
        except (ImportError, Exception):
            pass

    return html_content


def watch(duration: float = 5.0, interval: float = 1.0):
    """
    Live-refreshes the dashboard in Colab/Jupyter using IPython.display.clear_output(wait=True).
    """
    t_end = time.time() + duration
    while time.time() < t_end:
        try:
            import IPython.display as ipd

            ipd.clear_output(wait=True)
            dashboard(display_inline=True)
        except (ImportError, Exception):
            dashboard(display_inline=False)
        time.sleep(interval)
