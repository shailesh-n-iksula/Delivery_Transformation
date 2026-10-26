"""Manual mode: every order as something a person can do by hand, with no connector.

`dcp worksheet` turns what is due into a checklist: who does it, the exact message to post or text to paste, and the one
command that records it afterwards. Messages are plain templates, so this costs no tokens. When a connector (Slack,
Pulse, GitHub) is connected, the runner does the same step itself and logs the same events, so a project can mix
manual and automatic steps freely.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .mdparse import numbered_items, strip_tags, tag_values
from .models import ACTION_KIND, STEP_LABEL
from .packlint import _match_sections


def _latest_pack(flow_dir: Path):
    packs = sorted(flow_dir.glob("pack_v*.md"), key=lambda p: int(p.stem.split("_v")[1]))
    return (packs[-1], int(packs[-1].stem.split("_v")[1])) if packs else (None, 0)


def _person(cfg: dict, role: str) -> str:
    p = (cfg.get("people") or {}).get(role)
    return p["name"] if p else f"the {role.replace('_', ' ')}"


# ---------- message templates (no model needed) ----------

def pack_summary(pack_text: str) -> dict:
    s = _match_sections(pack_text)
    title = re.search(r"^#\s*Requirement Pack:\s*(.+)$", pack_text, re.M)
    quote = re.search(r'["“](.+?)["”]', s.get("summary", ""))
    return {"title": title.group(1).strip() if title else "(untitled)", "summary": quote.group(1) if quote else "",
            "scope": [strip_tags(i) for i in numbered_items(s.get("scope_in", ""))[0]],
            "criteria": len(numbered_items(s.get("acceptance", ""))[0]),
            "questions": [(strip_tags(q), (tag_values(q, "Owner") or ["unassigned"])[0]) for q in numbered_items(s.get("questions", ""))[0]],
            "risks": [strip_tags(r) for r in numbered_items(s.get("risks", ""))[0]]}


def msg_gate1(flow: str, pack_text: str, version: int, cfg: dict) -> str:
    p = pack_summary(pack_text)
    lines = [f"*Gate 1: please confirm the requirement for {p['title']}* ({flow}, pack v{version})", ""]
    if p["summary"]:
        lines.append(f"The client's words: \"{p['summary']}\"")
    lines += [f"{len(p['scope'])} things in scope, {p['criteria']} acceptance criteria, {len(p['risks'])} risks and dependencies."]
    lines += [f"  {i}. {s}" for i, s in enumerate(p["scope"], 1)]
    if p["questions"]:
        lines += ["", "Still to confirm with the client:"] + [f"  {i}. {q} ({o})" for i, (q, o) in enumerate(p["questions"], 1)]
    lines += ["", f"{_person(cfg, 'delivery_manager')}: read the pack, confirm it with the client, then approve here. "
              "Every scope item and criterion carries the client's own words from the call."]
    return "\n".join(lines)


def msg_gate2(flow: str, tickets_md: str, cfg: dict, required_roles: List[str]) -> str:
    from .ticketlint import parse_tickets
    ts = parse_tickets(tickets_md)
    stories = [t for t in ts if t.kind == "STORY"]
    lines = [f"*Gate 2: please validate the tickets before development* ({flow})", "",
             f"{len(stories)} stories, {sum(1 for t in ts if t.kind in ('DEP', 'RISK', 'DECISION'))} risks, dependencies and decisions:"]
    lines += [f"  S{i}. {t.title}" for i, t in enumerate(stories, 1)]
    who = _person(cfg, "delivery_manager") + (" and " + _person(cfg, "tech_lead") if "tech_lead" in required_roles else "")
    lines += ["", f"{who}: validate here.",
              f"{_person(cfg, 'tech_lead')}: sizing is optional and separate. Reply with sizes like 'S1=5d S2=3d' any time before development starts."]
    return "\n".join(lines)


def msg_followup(flow: str, missing: List[str], needed: int, cfg: dict) -> str:
    from .packlint import COVERAGE_LABELS
    return "\n".join([f"*{flow}: this call cannot support a requirement yet.*", "",
                      "The checks found no clear answer on:"] + [f"  - {COVERAGE_LABELS.get(m, m)}" for m in missing] +
                     ["", f"{_person(cfg, 'delivery_manager')}: hold a follow-up call, or reply with a reason to draft anyway. "
                      "Paste the follow-up transcript into the inbox with a first line 'Flow: " + flow + "' and it joins this requirement."])


def msg_nudge(flow: str, check: str, owner: str, detail: str, cfg: dict) -> str:
    return f"Reminder for {_person(cfg, owner)} on {flow}: {detail or check} is waiting on you."


def msg_escalate(flow: str, reason: str, cfg: dict) -> str:
    return f"{_person(cfg, 'delivery_manager')}: {flow} needs a person. Reason: {reason.replace('_', ' ')}."


def msg_change(flow: str, version: int, reason: str, cfg: dict) -> str:
    return (f"Requirement change on {flow} after the build started: pack v{version}. Reason: {reason}. "
            f"{_person(cfg, 'tech_lead')}: please check the branch against the updated tickets.")


def msg_launch(flow: str, branch: str, tier: str, unsized: bool, brief_path: str) -> str:
    return "\n".join([f"*{flow}: ready to build.* Branch `{branch}`, tier {tier}.", f"Build brief: {brief_path}",
                      "Tech lead: run the brief in Claude Code on that branch." + (" Note: the stories are not sized." if unsized else "")])


def email_questions(flow: str, pack_text: str) -> str:
    p = pack_summary(pack_text)
    if not p["questions"]:
        return "(the pack has no open questions)"
    return "\n".join([f"Hi, to lock the scope for {p['title']} we need these answered:", ""] +
                     [f"{i}. {q} ({o})" for i, (q, o) in enumerate(p["questions"], 1)] +
                     ["", "Please reply with a number next to each answer."])


# ---------- the worksheet ----------

def _item(order: dict, who: str, steps: List[str], message: Optional[str] = None, commands: Optional[List[str]] = None) -> dict:
    return {"order": f"{order['flow']}: {order['action']}", "what": STEP_LABEL.get(order["action"], order["action"]),
            "why": order.get("why", ""), "who": who, "steps": steps, "message": message, "commands": commands or []}


def build_worksheet(cfg: dict, orders: List[dict], waiting: List[dict], data_dir: Path, store_file: str, rules: dict) -> List[dict]:
    key = cfg["key"]
    ch = cfg["slack"]["channel"]
    out: List[dict] = []

    def log(type_, flow, data):
        return f"python3 -m dcp log {type_} --store {store_file} --flow {flow} --project {key} --actor <your name> --data '{json.dumps(data)}'"

    def base(flow):
        return data_dir / key / flow

    for o in orders:
        a, flow = o["action"], o["flow"]
        fd = base(flow) if flow != "_poll" else None
        inp = o.get("inputs") or {}
        if a == "poll_granola":
            out.append(_item(o, "delivery manager", ["Paste mode is on: paste the transcript into data/%s/inbox/ and run `python3 -m dcp tick --project %s`." % (key, key)]))
        elif a == "poll_slack":
            cmds = []
            for w in waiting:
                if w["waiting_for"] == "gate1":
                    cmds.append(f"python3 -m dcp gate-approve --project {key} --flow {w['flow']} --gate 1 --approver-id <Slack ID of who approved>")
                elif w["waiting_for"].startswith("gate2"):
                    cmds.append(f"python3 -m dcp gate-approve --project {key} --flow {w['flow']} --gate 2 --approver-id <Slack ID of who validated>")
                elif w["waiting_for"] == "tier":
                    cmds.append(f"python3 -m dcp tier-set --project {key} --flow {w['flow']} --tier <T1|T2|T3> --setter-id <tech lead Slack ID>")
                elif w["waiting_for"] == "branch":
                    cmds.append(f"python3 -m dcp branch-share --project {key} --flow {w['flow']} --branch <branch> --actor <name>   (or: dcp no-code --flow {w['flow']} --reason ...)")
                elif w["waiting_for"] == "sizes":
                    cmds.append(f"python3 -m dcp tickets-size --project {key} --flow {w['flow']} --tickets {base(w['flow'])}/tickets.md --sizes 'S1=5d S2=3d' --by-id <tech lead Slack ID>")
            out.append(_item(o, "delivery manager", [f"Read {ch} for replies on each waiting item below. If you cannot tell who replied, ask for their "
                                                     "Slack member ID (profile, three dots, Copy member ID) and register them with `dcp people`.",
                                                     "Record each answer with the matching command."] + [f"Waiting: {w['flow']} {w['waiting_for']} ({w['owner']})" for w in waiting], None, cmds))
        elif a == "sweep_pulse":
            out.append(_item(o, "delivery manager", ["Export the board's tickets from Pulse as JSON: a list of {key, title, description}.",
                                                     "Run the check; it lists what is missing on each ticket."],
                             commands=[f"python3 -m dcp lint-pulse --project {key} --export board.json"]))
        elif a == "refresh_governance":
            out.append(_item(o, "delivery manager", ["Generate the dashboard and share it with the team."],
                             commands=[f"python3 -m dcp dashboard --project {key} --out dashboard.html", f"python3 -m dcp score {store_file}"]))
        elif a == "open_gate":
            pack, v = _latest_pack(fd)
            if inp.get("gate") == 1 and pack:
                msg = msg_gate1(flow, pack.read_text(encoding="utf-8"), v, cfg)
            elif inp.get("gate") == 2 and (fd / "tickets.md").exists():
                msg = msg_gate2(flow, (fd / "tickets.md").read_text(encoding="utf-8"), cfg, inp.get("required_roles", ["delivery_manager"]))
            else:
                msg = f"Gate {inp.get('gate')} for {flow}: please review. (The pack or tickets file was not found in {fd}.)"
            out.append(_item(o, "delivery manager", [f"Post this in {ch}.", "Then record that the gate is open."], msg,
                             [f"python3 -m dcp gate-open --project {key} --flow {flow} --gate {inp.get('gate')}"]))
        elif a == "nudge":
            out.append(_item(o, "delivery manager", [f"Send this to the {inp.get('to', 'owner')} in {ch}.", "Then record it so they are not nudged again for a few hours."],
                             msg_nudge(flow, inp.get("check", ""), inp.get("owner", ""), inp.get("detail", ""), cfg),
                             [log("nudge.sent", flow, {"check": inp.get("check", "")})]))
        elif a == "escalate":
            out.append(_item(o, "delivery manager", [f"Post this in {ch}.", "Then record it."], msg_escalate(flow, inp.get("reason", ""), cfg),
                             [log("escalated", flow, {"reason": inp.get("reason", "")})]))
        elif a == "request_followup":
            out.append(_item(o, "delivery manager", [f"Post this in {ch}, then arrange the follow-up call or give a reason to proceed.",
                                                     "To proceed anyway, the delivery manager runs the second command."],
                             msg_followup(flow, inp.get("missing", []), inp.get("needed", 5), cfg),
                             [log("nudge.sent", flow, {"check": "followup"}),
                              f"python3 -m dcp call-proceed --project {key} --flow {flow} --reason '<why>' --by-id <delivery manager Slack ID>"]))
        elif a == "notify_change":
            out.append(_item(o, "delivery manager", [f"Post this in {ch}, then record it."], msg_change(flow, inp.get("pack_version", 0), inp.get("reason", ""), cfg),
                             [log("nudge.sent", flow, {"check": "cr_notify"})]))
        elif a == "launch_build":
            out.append(_item(o, "tech lead", ["Build the brief from the confirmed pack and tickets.", f"Post this in {ch}.",
                                              "Run the brief in Claude Code yourself, on the shared branch, then record that the build started."],
                             msg_launch(flow, inp.get("branch", "<branch>"), inp.get("tier", "?"), bool(inp.get("unsized")), f"briefs/{flow}.md"),
                             [f"python3 -m dcp build-brief --project {key} --flow {flow} --pack {_latest_pack(fd)[0] or '<pack>'} --tickets {fd}/tickets.md --out briefs/{flow}.md",
                              log("build.started", flow, {"ticket": "<Pulse key of the story>"})]))
        elif a == "create_pulse_tickets":
            out.append(_item(o, "delivery manager", ["Print the plan and create each ticket in Pulse by hand, in order (create first, then the link comments).",
                                                     "After each one, record the Pulse key it got. Run the plan again until it says complete."],
                             commands=[f"python3 -m dcp tickets-plan --project {key} --flow {flow} --tickets {fd}/tickets.md",
                                       f"python3 -m dcp tickets-record --project {key} --flow {flow} --local-id <id from the plan> --key <Pulse key> --hash <hash> --title '<title>'"]))
        elif a in ("draft_pack", "fix_pack", "revise_pack"):
            v = inp.get("version", 1)
            out.append(_item(o, "delivery manager", [f"Open an AI assistant approved for client data. Give it {inp.get('prompt', 'prompts/stage2_requirement_pack.md')} and {fd}/transcript.txt"
                                                     + ("; for a revision also give it the current pack and the reason." if a != "draft_pack" else "."),
                                                     f"Save its reply as {fd}/pack_v{v}.md. Read it before saving: it is a draft.", "Record it; the check runs by itself."],
                             commands=[log("pack.drafted", flow, {"version": v}), f"python3 -m dcp tick --project {key}"]))
        elif a in ("draft_tickets", "fix_tickets", "revise_tickets"):
            out.append(_item(o, "delivery manager", [f"Give the AI assistant {inp.get('prompt', 'prompts/stage3_ticket_set.md')} and the confirmed pack.",
                                                     f"Save its reply as {fd}/tickets.md and read it.", "Record it; the checks run by themselves."],
                             commands=[log("tickets.drafted", flow, {"count": "<number of tickets>"}), f"python3 -m dcp tick --project {key}"]))
        elif a == "prepare_questions":
            pack, v = _latest_pack(fd)
            out.append(_item(o, "delivery manager", ["Send this to the client by email, numbered, in the body (not as an attachment).", "Then record it."],
                             email_questions(flow, pack.read_text(encoding="utf-8")) if pack else "(pack not found)",
                             [log("questions.sent", flow, {"count": inp.get("count", 0)})]))
        elif a == "create_support_ticket":
            out.append(_item(o, "delivery manager", ["Write the fields from the Slack thread into a JSON file (what_happened, impact, environment, evidence, reported_by, slack_thread, tier, next_step).",
                                                     "Run the check, create the ticket in Pulse from its output, and record the key."],
                             commands=[f"python3 -m dcp support-ticket --project {key} --flow {flow} --fields fields.json --title '<short title>'",
                                       log("ticket.created", flow, {"ticket": "<Pulse key>"})]))
        elif ACTION_KIND.get(a) == "code":
            out.append(_item(o, "nobody", ["This is plain code. It runs by itself when you run the tick."], commands=[f"python3 -m dcp tick --project {key}"]))
        else:
            out.append(_item(o, "delivery manager", ["No manual steps are written for this yet."]))
    return out


def render(items: List[dict], waiting: List[dict]) -> str:
    if not items and not waiting:
        return "Nothing to do right now."
    out: List[str] = []
    for n, it in enumerate(items, 1):
        out += [f"{n}. {it['what'].upper()}   [{it['order']}]   who: {it['who']}", f"   why: {it['why']}"]
        out += [f"   - {s}" for s in it["steps"]]
        if it["message"]:
            out += ["   message to use:", *[f"   | {l}" for l in it["message"].splitlines()]]
        if it["commands"]:
            out += ["   then run:", *[f"   $ {c}" for c in it["commands"]]]
        out.append("")
    people = [w for w in waiting if not w.get("optional")]
    if people:
        out += ["WAITING ON PEOPLE (nothing for you to run until they act):"]
        out += [f"   {w['flow']}: {w['waiting_for']} ({w['owner']}). {w['detail']}" for w in people]
    return "\n".join(out)
