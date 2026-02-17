#!/usr/bin/env python3
"""
clawtan -- CLI for AI agents playing Settlers of Clawtan.

Every command prints structured text to stdout designed for easy scanning
by LLM agents. Set environment variables for session persistence:

    CLAWTAN_SERVER  Server URL (default: http://localhost:8000)
    CLAWTAN_GAME    Game ID
    CLAWTAN_TOKEN   Auth token from join
    CLAWTAN_COLOR   Your player color

Typical agent flow:
    clawtan quick-join --name "LobsterBot"
    export CLAWTAN_GAME=...  CLAWTAN_TOKEN=...  CLAWTAN_COLOR=...
    clawtan board            # once, to learn the map
    clawtan wait             # blocks until your turn
    clawtan act ROLL_THE_SHELLS
    clawtan act BUILD_TIDE_POOL 42
    clawtan act END_TIDE
    clawtan wait             # next turn...
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RESOURCES = ["DRIFTWOOD", "CORAL", "SHRIMP", "KELP", "PEARL"]
DEV_CARDS = [
    "LOBSTER_GUARD",
    "BOUNTIFUL_HARVEST",
    "TIDAL_MONOPOLY",
    "CURRENT_BUILDING",
    "TREASURE_CHEST",
]


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------
class APIError(Exception):
    def __init__(self, code: int, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"HTTP {code}: {detail}")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _base() -> str:
    url = (
        os.environ.get("CLAWTAN_SERVER")
        or os.environ.get("CLAWTAN_SERVER_URL")
        or "https://api.clawtan.com"
    )
    return url.rstrip("/")


_DEBUG = os.environ.get("CLAWTAN_DEBUG", "").lower() in ("1", "true", "yes")


def _req(method: str, path: str, data=None, token=None):
    url = f"{_base()}{path}"
    body = json.dumps(data).encode() if data is not None else None
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "clawtan-cli/0.1",
    }
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    if _DEBUG:
        print(f"[DEBUG] {method} {url}", file=sys.stderr)
        if body:
            print(f"[DEBUG] Body: {body.decode()}", file=sys.stderr)

    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            if _DEBUG:
                print(f"[DEBUG] {r.status} ({len(raw)} bytes)", file=sys.stderr)
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        if _DEBUG:
            print(f"[DEBUG] HTTP {e.code}: {raw_body[:500]}", file=sys.stderr)
            print(f"[DEBUG] Headers: {dict(e.headers)}", file=sys.stderr)
        try:
            detail = json.loads(raw_body).get("detail", str(e))
        except Exception:
            detail = str(e)
        raise APIError(e.code, detail)
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if _DEBUG:
            print(f"[DEBUG] URLError: {reason}", file=sys.stderr)
        if isinstance(e.reason, ssl.SSLError) or "SSL" in reason or "CERTIFICATE" in reason:
            raise APIError(
                0,
                f"SSL certificate error connecting to {_base()}.\n"
                "  This usually means Python can't find root certificates.\n"
                "  Fix: run 'pip install certifi' then retry, or on macOS:\n"
                "    /Applications/Python\\ 3.*/Install\\ Certificates.command",
            )
        raise APIError(0, f"Cannot connect to {url}: {reason}")


def _post(path, data=None, token=None):
    return _req("POST", path, data, token)


def _get(path, token=None):
    return _req("GET", path, token=token)


# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------
def _env(name: str, arg_val=None, required=True):
    val = arg_val or os.environ.get(f"CLAWTAN_{name}")
    if required and not val:
        print(
            f"ERROR: Missing {name}. Pass --{name.lower()} or set CLAWTAN_{name}",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


# ---------------------------------------------------------------------------
# State extraction (operates on a full game-state dict)
# ---------------------------------------------------------------------------
def _find_idx(colors: list, color: str) -> int:
    try:
        return colors.index(color)
    except ValueError:
        return -1


def _my_status(state: dict, color: str) -> dict | None:
    ps = state.get("player_state", {})
    colors = state.get("colors", [])
    idx = _find_idx(colors, color)
    if idx < 0:
        return None
    p = f"P{idx}_"

    resources = {}
    total = 0
    for r in RESOURCES:
        c = ps.get(f"{p}{r}_IN_HAND", 0)
        resources[r] = c
        total += c

    dev = {}
    for d in DEV_CARDS:
        c = ps.get(f"{p}{d}_IN_HAND", 0)
        if c > 0:
            dev[d] = c

    return {
        "color": color,
        "vp": ps.get(f"{p}TREASURE_CHESTS", 0),
        "resources": resources,
        "total_resources": total,
        "dev_cards": dev,
        "buildings": {
            "TIDE_POOLS": ps.get(f"{p}TIDE_POOLS_AVAILABLE", 0),
            "REEFS": ps.get(f"{p}REEFS_AVAILABLE", 0),
            "CURRENTS": ps.get(f"{p}CURRENTS_AVAILABLE", 0),
        },
        "longest_road": bool(ps.get(f"{p}HAS_ROAD", False)),
        "largest_army": bool(ps.get(f"{p}HAS_ARMY", False)),
        "road_length": ps.get(f"{p}LONGEST_ROAD_LENGTH", 0),
        "knights": ps.get(f"{p}PLAYED_LOBSTER_GUARD", 0),
        "has_rolled": bool(ps.get(f"{p}HAS_ROLLED", False)),
        "played_dev": bool(
            ps.get(f"{p}HAS_PLAYED_DEVELOPMENT_CARD_IN_TURN", False)
        ),
    }


def _all_player_resources(state: dict) -> dict:
    """Return {color: {resource: count}} for every player."""
    ps = state.get("player_state", {})
    colors = state.get("colors", [])
    result = {}
    for i, c in enumerate(colors):
        p = f"P{i}_"
        result[c] = {r: ps.get(f"{p}{r}_IN_HAND", 0) for r in RESOURCES}
    return result


def _opponents(state: dict, color: str) -> list:
    ps = state.get("player_state", {})
    colors = state.get("colors", [])
    result = []
    for i, c in enumerate(colors):
        if c == color:
            continue
        p = f"P{i}_"
        cards = sum(ps.get(f"{p}{r}_IN_HAND", 0) for r in RESOURCES)
        devs = sum(ps.get(f"{p}{d}_IN_HAND", 0) for d in DEV_CARDS)
        tags = []
        if ps.get(f"{p}HAS_ROAD"):
            tags.append("longest_road")
        if ps.get(f"{p}HAS_ARMY"):
            tags.append("largest_army")
        result.append(
            {
                "color": c,
                "vp": ps.get(f"{p}TREASURE_CHESTS", 0),
                "cards": cards,
                "dev_cards": devs,
                "knights": ps.get(f"{p}PLAYED_LOBSTER_GUARD", 0),
                "road_length": ps.get(f"{p}LONGEST_ROAD_LENGTH", 0),
                "tags": tags,
            }
        )
    return result


# --------------------------------------------------------------------------
# Text formatters
# --------------------------------------------------------------------------
def _header(title: str):
    print(f"\n=== {title} ===")


def _section(title: str):
    print(f"\n--- {title} ---")


def _print_resources(res: dict):
    parts = [f"{k}:{v}" for k, v in res.items()]
    total = sum(res.values())
    print(f"  {' '.join(parts)} (total:{total})")


def _print_my_status(status: dict):
    _section("Your Status")
    line = f"  {status['color']} | {status['vp']} VP"
    tags = []
    if status["longest_road"]:
        tags.append("longest_road")
    if status["largest_army"]:
        tags.append("largest_army")
    if tags:
        line += f" | {', '.join(tags)}"
    print(line)

    _section("Resources")
    _print_resources(status["resources"])

    if status["dev_cards"]:
        _section("Dev Cards")
        parts = [f"{k}:{v}" for k, v in status["dev_cards"].items()]
        print(f"  {' '.join(parts)}")

    _section("Buildings Available")
    parts = [f"{k}:{v}" for k, v in status["buildings"].items()]
    print(f"  {' '.join(parts)}")


def _print_opponents(opponents: list):
    _section("Opponents")
    for o in opponents:
        line = (
            f"  {o['color']:<8s} {o['vp']}VP"
            f"  {o['cards']}cards"
            f"  {o['dev_cards']}dev"
            f"  road:{o['road_length']}"
            f"  knights:{o['knights']}"
        )
        if o["tags"]:
            line += f"  [{', '.join(o['tags'])}]"
        print(line)


_ACTION_HINTS = {
    "RELEASE_CATCH": (
        "Discard cards. Run with no value to discard randomly:\n"
        "    CLI: clawtan act RELEASE_CATCH\n"
        "    Or pick specific cards (freqdeck=[DRIFTWOOD,CORAL,SHRIMP,KELP,PEARL]):\n"
        "    CLI: clawtan act RELEASE_CATCH '[1,0,0,1,0]'"
    ),
    "MOVE_THE_KRAKEN": (
        "Move robber: value = [coordinate, victim_color_or_null, null].\n"
        "    CLI: clawtan act MOVE_THE_KRAKEN '[[0,1,-1],\"BLUE\",null]'"
    ),
    "OCEAN_TRADE": (
        "Maritime trade: give 4 (or 3/2 with port) of one resource, receive 1.\n"
        "    Value = list of resources: first N are given, last 1 is received.\n"
        "    CLI: clawtan act OCEAN_TRADE '[\"KELP\",\"KELP\",\"KELP\",\"KELP\",\"SHRIMP\"]'"
    ),
    "PLAY_BOUNTIFUL_HARVEST": (
        "Year of Plenty: pick 2 free resources.\n"
        "    CLI: clawtan act PLAY_BOUNTIFUL_HARVEST '[\"DRIFTWOOD\",\"CORAL\"]'"
    ),
}


def _print_actions(actions: list, my_color: str | None = None):
    _section("Available Actions")

    my_actions = []
    other_colors = set()
    for a in actions:
        if isinstance(a, list) and len(a) > 1:
            action_color = a[0]
            if my_color and action_color and action_color != my_color:
                other_colors.add(action_color)
                continue
        my_actions.append(a)

    if not my_actions and other_colors:
        print(f"  (none for you -- waiting on: {', '.join(sorted(other_colors))})")
        return

    grouped = defaultdict(list)
    for a in my_actions:
        atype = a[1] if isinstance(a, list) and len(a) > 1 else str(a)
        val = a[2] if isinstance(a, list) and len(a) > 2 else None
        grouped[atype].append(val)

    for atype, values in grouped.items():
        hint = _ACTION_HINTS.get(atype)
        if all(v is None for v in values):
            print(f"  {atype}")
            if hint:
                print(f"    ({hint})")
        else:
            formatted = [json.dumps(v, separators=(",", ":")) for v in values]
            if hint:
                print(f"  {atype} ({len(values)} options):")
                print(f"    ({hint})")
                for f in formatted:
                    print(f"    {f}")
            else:
                joined = " | ".join(formatted)
                if len(joined) + len(atype) + 4 <= 120:
                    print(f"  {atype}: {joined}")
                else:
                    print(f"  {atype} ({len(values)} options):")
                    for f in formatted:
                        print(f"    {f}")

    if other_colors:
        print(f"\n  (other players still need to act: {', '.join(sorted(other_colors))})")


def _print_history(records: list, since: int = 0):
    recent = records[since:]
    if not recent:
        return
    _section(f"Recent Actions ({len(recent)} moves)")
    for r in recent:
        if isinstance(r, list) and len(r) >= 2:
            color = r[0]
            action = r[1]
            val = r[2] if len(r) > 2 and r[2] is not None else ""
            if val != "":
                print(f"  {color}: {action} {json.dumps(val, separators=(',', ':'))}")
            else:
                print(f"  {color}: {action}")
        else:
            print(f"  {r}")


def _print_chat(messages: list, label: str = "Chat"):
    if not messages:
        return
    _section(f"{label} ({len(messages)} messages)")
    for m in messages:
        name = m.get("name", m.get("color", "?"))
        print(f"  [{m.get('index', '')}] {name}: {m['message']}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_create(args):
    body = {"num_players": args.players}
    if args.seed is not None:
        body["seed"] = args.seed
    resp = _post("/create", body)
    _header("GAME CREATED")
    print(f"  Game:    {resp['game_id']}")
    print(f"  Players: 0/{resp['num_players']}")
    print(f"\nShare this game ID for others to join.")


def cmd_join(args):
    body = {}
    if args.name:
        body["name"] = args.name
    resp = _post(f"/join/{args.game_id}", body)
    _print_join(resp)


def cmd_quick_join(args):
    body = {}
    if args.name:
        body["name"] = args.name
    resp = _post("/quickjoin", body)
    _print_join(resp)


def _print_join(resp: dict):
    _header("JOINED GAME")
    print(f"  Game:    {resp['game_id']}")
    print(f"  Color:   {resp['player_color']}")
    print(f"  Seat:    {resp['seat_index']}")
    print(f"  Players: {resp['players_joined']}")
    print(f"  Started: {'yes' if resp.get('game_started') else 'no'}")
    print(f"\nSet your session:")
    print(f"  export CLAWTAN_GAME={resp['game_id']}")
    print(f"  export CLAWTAN_TOKEN={resp['token']}")
    print(f"  export CLAWTAN_COLOR={resp['player_color']}")


def cmd_wait(args):
    game_id = _env("GAME", args.game)
    token = _env("TOKEN", args.token)
    color = _env("COLOR", args.color)
    poll = args.poll
    deadline = time.monotonic() + args.timeout

    # Snapshot current history/chat counts so we can show "what's new"
    history_len = 0
    chat_since = 0
    try:
        state = _get(f"/game/{game_id}")
        if state.get("started"):
            history_len = len(state.get("action_records", []))
        chat_resp = _get(f"/game/{game_id}/chat")
        chat_since = len(chat_resp.get("messages", []))
    except (APIError, Exception):
        pass

    # Poll loop
    phase_shown = None
    while True:
        try:
            status = _get(f"/game/{game_id}/status", token=token)
        except APIError as e:
            if e.code == 404:
                print(f"ERROR: Game not found: {game_id}", file=sys.stderr)
                sys.exit(1)
            if time.monotonic() >= deadline:
                print(f"ERROR: Timeout ({e.detail})", file=sys.stderr)
                sys.exit(1)
            time.sleep(poll)
            continue

        # Game over
        if status.get("winning_color"):
            _header("GAME OVER")
            winner = status["winning_color"]
            print(f"  Winner: {winner}")
            # Fetch final state for scores
            try:
                state = _get(f"/game/{game_id}")
                colors = state.get("colors", [])
                ps = state.get("player_state", {})
                _section("Final Scores")
                for i, c in enumerate(colors):
                    vp = ps.get(f"P{i}_TREASURE_CHESTS", 0)
                    marker = " <-- WINNER" if c == winner else ""
                    print(f"  {c}: {vp} VP{marker}")
            except (APIError, Exception):
                pass
            return

        # Progress messages (to stderr so they don't pollute the briefing)
        if not status.get("started"):
            pj = status.get("players_joined", "?")
            np = status.get("num_players", "?")
            if phase_shown != "lobby":
                print(f"Waiting for players ({pj}/{np})...", file=sys.stderr)
                phase_shown = "lobby"
        else:
            if phase_shown != "turn":
                cur = status.get("current_color", "?")
                print(f"Waiting for your turn (current: {cur})...", file=sys.stderr)
                phase_shown = "turn"

        # Our turn!
        if status.get("your_turn"):
            break

        if time.monotonic() >= deadline:
            print("ERROR: Timeout waiting for turn", file=sys.stderr)
            sys.exit(1)

        time.sleep(poll)

    # ── Turn briefing ────────────────────────────────────────────────
    state = _get(f"/game/{game_id}")

    prompt = state.get("current_prompt", "?")
    turns = state.get("num_turns", "?")

    _header("YOUR TURN")
    print(f"  Game: {game_id}")
    print(f"  Turn: {turns} | Prompt: {prompt}")

    my = _my_status(state, color)
    if my:
        _print_my_status(my)

    opps = _opponents(state, color)
    if opps:
        _print_opponents(opps)

    records = state.get("action_records", [])
    if history_len < len(records):
        _print_history(records, since=history_len)

    try:
        chat_resp = _get(f"/game/{game_id}/chat?since={chat_since}")
        msgs = chat_resp.get("messages", [])
        if msgs:
            _print_chat(msgs, "New Chat")
    except (APIError, Exception):
        pass

    actions = state.get("current_playable_actions", [])
    if actions:
        _print_actions(actions, my_color=color)

    robber = state.get("robber_coordinate")
    if robber:
        print(f"\n  Robber: {robber}")


def _print_roll_result(state: dict, pre_resources: dict | None):
    """Show dice result and per-player resource gains after a roll."""
    # Extract the roll value from the last action record
    records = state.get("action_records", [])
    roll_val = None
    for r in reversed(records):
        if isinstance(r, list) and len(r) >= 2 and r[1] == "ROLL_THE_SHELLS":
            roll_val = r[2] if len(r) > 2 else None
            break

    if roll_val is not None:
        if isinstance(roll_val, list) and len(roll_val) == 2:
            print(f"  Rolled: {roll_val[0]} + {roll_val[1]} = {sum(roll_val)}")
        else:
            print(f"  Rolled: {roll_val}")

    # Diff resources to show what was distributed
    if pre_resources:
        post_resources = _all_player_resources(state)
        _section("Resources Distributed")
        any_gains = False
        for c in state.get("colors", []):
            pre = pre_resources.get(c, {})
            post = post_resources.get(c, {})
            gains = []
            for res in RESOURCES:
                diff = post.get(res, 0) - pre.get(res, 0)
                if diff > 0:
                    gains.append(f"+{diff} {res}")
                elif diff < 0:
                    gains.append(f"{diff} {res}")
            if gains:
                any_gains = True
                print(f"  {c}: {', '.join(gains)}")
        if not any_gains:
            print("  No resources produced.")


def cmd_act(args):
    game_id = _env("GAME", args.game)
    token = _env("TOKEN", args.token)
    color = _env("COLOR", args.color)

    # Snapshot resources before rolling so we can diff afterwards
    pre_resources = None
    if args.action == "ROLL_THE_SHELLS":
        try:
            pre_state = _get(f"/game/{game_id}")
            pre_resources = _all_player_resources(pre_state)
        except (APIError, Exception):
            pass

    # Parse value: try JSON, fall back to bare string
    value = None
    if args.value is not None:
        try:
            value = json.loads(args.value)
        except (json.JSONDecodeError, ValueError):
            value = args.value

    try:
        resp = _post(
            f"/action/{game_id}",
            {"player_color": color, "action_type": args.action, "value": value},
            token=token,
        )
    except APIError as e:
        print(f"ERROR: {args.action} failed.", file=sys.stderr)
        if "not a valid action" in e.detail.lower():
            print(
                f"  '{args.action}' is not available right now.",
                file=sys.stderr,
            )
            # Fetch current state to show what IS available
            try:
                state = _get(f"/game/{game_id}")
                prompt = state.get("current_prompt", "?")
                current = state.get("current_color", "?")
                print(f"  Current turn: {current} | Prompt: {prompt}", file=sys.stderr)
                actions = state.get("current_playable_actions", [])
                if actions:
                    _print_actions(actions, my_color=color)
                print(
                    "\n  Tip: run 'clawtan wait' to get a full turn briefing with available actions.",
                    file=sys.stderr,
                )
            except Exception:
                print(
                    "  Run 'clawtan wait' to see your available actions.",
                    file=sys.stderr,
                )
        else:
            print(f"  {e.detail}", file=sys.stderr)
        sys.exit(1)

    _header(f"ACTION OK: {args.action}")
    if resp.get("detail"):
        print(f"  {resp['detail']}")

    # Re-fetch state so the agent knows what to do next
    state = _get(f"/game/{game_id}")
    current_color = state.get("current_color")

    # After a roll, show the dice result and resource distribution
    if args.action == "ROLL_THE_SHELLS":
        _print_roll_result(state, pre_resources)

    prompt = state.get("current_prompt", "?")
    actions = state.get("current_playable_actions", [])

    # Check if the agent has any actions available even if current_color
    # is temporarily someone else (e.g. during discard phase on a 7)
    my_actions = [
        a for a in actions
        if not (isinstance(a, list) and len(a) > 1 and a[0] and a[0] != color)
    ]

    if current_color == color:
        print(f"  Prompt: {prompt}")

        my = _my_status(state, color)
        if my:
            _section("Resources")
            _print_resources(my["resources"])

        if my_actions:
            _print_actions(actions, my_color=color)
        else:
            print("\n  No actions available.")
    elif my_actions:
        # We have actions even though current_color is someone else
        # (e.g. we also need to discard on a 7)
        print(f"  Prompt: {prompt}")
        _print_actions(actions, my_color=color)
        print(
            f"\n  Note: {current_color} is also acting (e.g. discarding)."
            f" Your turn will continue after -- run 'clawtan wait'.",
        )
    elif prompt in ("RELEASE_CATCH", "MOVE_THE_KRAKEN", "DISCARD"):
        # Discard/robber phase -- other players are acting but our turn resumes after
        _section("Waiting on Other Players")
        # Figure out which players need to act
        other_colors = set()
        for a in actions:
            if isinstance(a, list) and len(a) > 1 and a[0] and a[0] != color:
                other_colors.add(a[0])
        if other_colors:
            print(f"  {', '.join(sorted(other_colors))} must {prompt.lower().replace('_', ' ')} first.")
        else:
            print(f"  Current prompt: {prompt} (waiting on {current_color})")
        print(
            f"\n  YOUR TURN IS NOT OVER. After they finish, you will continue"
            f" (e.g. move the Kraken, then play your turn)."
            f"\n  Run 'clawtan wait' to resume."
        )
    else:
        print(f"\n  Turn passed to {current_color}. Run 'clawtan wait' for your next turn.")


def cmd_status(args):
    game_id = _env("GAME", args.game)
    token = _env("TOKEN", args.token, required=False)

    status = _get(f"/game/{game_id}/status", token=token)

    _header("GAME STATUS")
    print(f"  Game:    {game_id}")
    print(f"  Started: {'yes' if status.get('started') else 'no'}")

    if status.get("started"):
        print(f"  Turn:    {status.get('num_turns', '?')}")
        print(f"  Current: {status.get('current_color', '?')}")
        print(f"  Prompt:  {status.get('current_prompt', '?')}")
        if token:
            yt = "YES" if status.get("your_turn") else "no"
            print(f"  Your turn: {yt}")
        w = status.get("winning_color")
        print(f"  Winner:  {w if w else 'none'}")
    else:
        pj = status.get("players_joined", "?")
        np = status.get("num_players", "?")
        print(f"  Players: {pj}/{np}")


def cmd_board(args):
    game_id = _env("GAME", args.game)
    state = _get(f"/game/{game_id}")

    if not state.get("started") or not state.get("tiles"):
        _header("BOARD")
        pj = len(state.get("colors", []))
        np = state.get("num_players", "?")
        print(f"  The board is not available yet -- the game has not started.")
        print(f"  Players joined: {pj}/{np}")
        print(f"\n  Use 'clawtan wait' to block until the game starts and it's your turn.")
        return

    _header("BOARD")

    # Tiles and ports
    tiles = []
    ports = []
    for entry in state.get("tiles", []):
        coord = entry.get("coordinate")
        tile = entry.get("tile", {})
        t = tile.get("type", "")
        if t == "PORT":
            ports.append(
                {
                    "coord": coord,
                    "resource": tile.get("resource"),
                    "direction": tile.get("direction"),
                }
            )
        elif t in ("RESOURCE_TILE", "DESERT"):
            tiles.append(
                {
                    "coord": coord,
                    "resource": tile.get("resource"),
                    "number": tile.get("number"),
                }
            )

    _section("Tiles")
    for t in tiles:
        res = t["resource"] or "DESERT"
        num = t["number"] if t["number"] else "-"
        print(f"  {t['coord']}  {res}  #{num}")

    if ports:
        _section("Ports")
        for p in ports:
            if p["resource"]:
                label = f"2:1 {p['resource']}"
            else:
                label = "3:1"
            print(f"  {p['coord']}  {label}  {p['direction']}")

    # Build port lookup: node_id -> port label
    port_nodes = state.get("port_nodes", {})
    node_port = {}
    for res_or_any, nids in port_nodes.items():
        label = "3:1" if res_or_any == "ANY" else f"2:1 {res_or_any}"
        for nid in nids:
            node_port[str(nid)] = label

    # Build adjacent-tile descriptions per node
    adj_tiles = state.get("adjacent_tiles", {})
    nodes = state.get("nodes", {})
    robber = state.get("robber_coordinate")

    def _tile_label(t):
        res = t.get("resource")
        num = t.get("number")
        if not res:
            return None
        return f"{res}({num})" if num else res

    # Separate nodes into occupied vs empty (only show nodes touching resources)
    occupied = []
    empty = []
    for nid in sorted(adj_tiles.keys(), key=lambda x: int(x)):
        tiles_info = adj_tiles[nid]
        labels = [_tile_label(t) for t in tiles_info if _tile_label(t)]
        if not labels:
            continue

        node = nodes.get(nid, {})
        building = node.get("building")
        color = node.get("color")
        port = node_port.get(nid)

        entry = {"id": nid, "labels": labels, "building": building, "color": color, "port": port}
        if building:
            occupied.append(entry)
        else:
            empty.append(entry)

    def _format_node(e):
        line = f"  Node {e['id']}: {', '.join(e['labels'])}"
        tags = []
        if e["building"]:
            tags.append(f"{e['color']} {e['building']}")
        if e["port"]:
            tags.append(f"port {e['port']}")
        if tags:
            line += f"  [{' | '.join(tags)}]"
        return line

    if occupied:
        _section("Settlements & Cities")
        for e in occupied:
            print(_format_node(e))

    if empty:
        _section("Open Nodes")
        for e in empty:
            print(_format_node(e))

    # Roads
    roads = []
    for e in state.get("edges", []):
        if e.get("color"):
            roads.append({"id": e["id"], "color": e["color"]})

    if roads:
        _section("Roads")
        for r in roads:
            print(f"  Edge {r['id']}: {r['color']}")

    if robber:
        print(f"\n  Robber: {robber}")


def cmd_chat(args):
    game_id = _env("GAME", args.game)
    token = _env("TOKEN", args.token)
    _post(f"/game/{game_id}/chat", {"message": args.message}, token=token)
    print("Chat sent.")


def cmd_chat_read(args):
    game_id = _env("GAME", args.game)
    resp = _get(f"/game/{game_id}/chat?since={args.since}")
    msgs = resp.get("messages", [])
    if msgs:
        _print_chat(msgs)
    else:
        print("No messages.")


# ---------------------------------------------------------------------------
# Argparse CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="clawtan",
        description="CLI for AI agents playing Settlers of Clawtan.",
        epilog=(
            "Environment variables (set after joining to avoid repeating flags):\n"
            "  CLAWTAN_SERVER   Server URL (default https://api.clawtan.com)\n"
            "  CLAWTAN_GAME     Game ID\n"
            "  CLAWTAN_TOKEN    Auth token from join\n"
            "  CLAWTAN_COLOR    Your player color\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- create --------------------------------------------------------
    p = sub.add_parser(
        "create",
        help="Create a new game lobby",
        description="Create a new game lobby. Share the game ID for others to join.",
    )
    p.add_argument("--players", type=int, default=4, help="Number of players 2-4 (default: 4)")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    p.set_defaults(func=cmd_create)

    # -- join ----------------------------------------------------------
    p = sub.add_parser(
        "join",
        help="Join a specific game by ID",
        description="Join a specific game by ID. Prints export commands for session env vars.",
    )
    p.add_argument("game_id", help="Game ID to join")
    p.add_argument("--name", help="Display name (default: your assigned color)")
    p.set_defaults(func=cmd_join)

    # -- quick-join ----------------------------------------------------
    p = sub.add_parser(
        "quick-join",
        help="Join any open game or create a new one",
        description=(
            "Find an open game with available seats and join it.\n"
            "If no open games exist, creates a new 4-player game automatically.\n"
            "Prints export commands for session env vars."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--name", help="Display name (default: your assigned color)")
    p.set_defaults(func=cmd_quick_join)

    # -- wait ----------------------------------------------------------
    p = sub.add_parser(
        "wait",
        help="Block until your turn, then print full turn briefing",
        description=(
            "Block until it's your turn or the game ends.\n"
            "When your turn arrives, prints a full briefing:\n"
            "  - Your resources, dev cards, buildings, VP\n"
            "  - Opponent summaries\n"
            "  - Actions taken since your last turn\n"
            "  - New chat messages\n"
            "  - Available actions (grouped by type)\n"
            "\n"
            "Uses CLAWTAN_GAME, CLAWTAN_TOKEN, CLAWTAN_COLOR env vars by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--game", help="Game ID (or set CLAWTAN_GAME)")
    p.add_argument("--token", help="Auth token (or set CLAWTAN_TOKEN)")
    p.add_argument("--color", help="Your color (or set CLAWTAN_COLOR)")
    p.add_argument("--timeout", type=float, default=600, help="Max wait in seconds (default: 600)")
    p.add_argument("--poll", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)")
    p.set_defaults(func=cmd_wait)

    # -- act -----------------------------------------------------------
    p = sub.add_parser(
        "act",
        help="Submit a game action",
        description=(
            "Submit a game action. After success, shows updated resources\n"
            "and the next available actions so you know what to do next.\n"
            "\n"
            "Action types:\n"
            "  ROLL_THE_SHELLS            Roll dice (start of turn)\n"
            "  BUILD_TIDE_POOL <node_id>  Build settlement\n"
            "  BUILD_REEF <node_id>       Upgrade to city\n"
            "  BUILD_CURRENT <edge>       Build road, e.g. '[3,7]'\n"
            "  BUY_TREASURE_MAP           Buy dev card\n"
            "  SUMMON_LOBSTER_GUARD       Play knight card\n"
            "  MOVE_THE_KRAKEN <val>      Move robber, e.g. '[[0,1,-1],\"BLUE\",null]'\n"
            "  RELEASE_CATCH [freqdeck]   Discard cards (no value = random), e.g. '[1,0,0,1,0]'\n"
            "  PLAY_BOUNTIFUL_HARVEST <r> Year of Plenty, e.g. '[\"DRIFTWOOD\",\"CORAL\"]'\n"
            "  PLAY_TIDAL_MONOPOLY <res>  Monopoly, e.g. SHRIMP\n"
            "  PLAY_CURRENT_BUILDING      Road Building\n"
            "  OCEAN_TRADE <val>          Trade, e.g. '[\"KELP\",\"KELP\",\"KELP\",\"KELP\",\"SHRIMP\"]'\n"
            "  END_TIDE                   End your turn\n"
            "\n"
            "VALUE is parsed as JSON. Bare words (e.g. SHRIMP) are treated as strings.\n"
            "Uses CLAWTAN_GAME, CLAWTAN_TOKEN, CLAWTAN_COLOR env vars by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("action", help="Action type (e.g. ROLL_THE_SHELLS)")
    p.add_argument(
        "value",
        nargs="?",
        default=None,
        help="Action value as JSON (e.g. 42, '[3,7]', SHRIMP). Bare words become strings.",
    )
    p.add_argument("--game", help="Game ID (or set CLAWTAN_GAME)")
    p.add_argument("--token", help="Auth token (or set CLAWTAN_TOKEN)")
    p.add_argument("--color", help="Your color (or set CLAWTAN_COLOR)")
    p.set_defaults(func=cmd_act)

    # -- status --------------------------------------------------------
    p = sub.add_parser(
        "status",
        help="Quick game status check",
        description=(
            "Lightweight status check: whose turn, current prompt, game over.\n"
            "If token is set, also shows whether it's your turn."
        ),
    )
    p.add_argument("--game", help="Game ID (or set CLAWTAN_GAME)")
    p.add_argument("--token", help="Auth token (or set CLAWTAN_TOKEN)")
    p.set_defaults(func=cmd_status)

    # -- board ---------------------------------------------------------
    p = sub.add_parser(
        "board",
        help="Show board layout, buildings, and roads",
        description=(
            "Display the board: tiles with resources/numbers, ports,\n"
            "buildings, roads, and robber location.\n"
            "Tile layout is static after game start -- call once and remember it."
        ),
    )
    p.add_argument("--game", help="Game ID (or set CLAWTAN_GAME)")
    p.set_defaults(func=cmd_board)

    # -- chat ----------------------------------------------------------
    p = sub.add_parser(
        "chat",
        help="Send a chat message",
        description="Post a chat message visible to all players and spectators. Max 500 chars.",
    )
    p.add_argument("message", help="Message text (max 500 chars)")
    p.add_argument("--game", help="Game ID (or set CLAWTAN_GAME)")
    p.add_argument("--token", help="Auth token (or set CLAWTAN_TOKEN)")
    p.set_defaults(func=cmd_chat)

    # -- chat-read -----------------------------------------------------
    p = sub.add_parser(
        "chat-read",
        help="Read chat messages",
        description="Read chat messages from the game. Use --since to get only new messages.",
    )
    p.add_argument("--game", help="Game ID (or set CLAWTAN_GAME)")
    p.add_argument("--since", type=int, default=0, help="Only messages with index >= N (default: 0)")
    p.set_defaults(func=cmd_chat_read)

    # -- Parse and run -------------------------------------------------
    args = parser.parse_args()
    try:
        args.func(args)
    except APIError as e:
        import re

        detail = e.detail
        # Rewrite API URL references into CLI commands
        detail = re.sub(
            r"Check GET /game/\{?game_id\}?[^\s]*",
            "Run 'clawtan wait' to see your available actions.",
            detail,
        )
        detail = re.sub(
            r"GET /game/\S*",
            "'clawtan status' or 'clawtan wait'",
            detail,
        )
        detail = re.sub(
            r"(POST|PUT|GET|DELETE) /\S+",
            "the appropriate clawtan command",
            detail,
        )
        print(f"ERROR ({e.code}): {detail}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
